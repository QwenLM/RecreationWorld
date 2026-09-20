#!/usr/bin/env python3
"""The shared five-platform screenshot assertion judge and CLI.

This file owns the actual judge: configuration, canonical prompt, image encoding, HTTP
transport, verdict parsing, directory traversal, aggregation, and the CLI. Platform code may
adapt its native assertion/result shape, but it must not implement another model call.

Two properties this CLI must preserve, both learned the hard way:

* ``--output`` exists. ax_eval.sh has passed it since the beginning; the parser did not
  accept it, argparse exited 2, `|| true` hid that, and the caller then produced a
  confident "0 of 9" out of the AUTHORED assertions — macOS's VLM dimension never once
  ran while reporting a plausible score.
* ``total`` counts assertions that actually got a verdict. ``authored`` and ``errors``
  are reported separately so a caller can tell "the model satisfied none of them" from
  "the judge never answered". An unjudged assertion must leave the denominator, not
  count as a failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

DEFAULT_MODEL = ""
DEFAULT_BASE_URL = ""
DEFAULT_MAX_TOKENS = 32768
KEY_ENVS = (
    "VLM_API_KEY",
    "VLM_JUDGE_API_KEY",
    "VLM_MODEL_API_KEY",
    "JUDGE_API_KEY",
)


# Full-suite runs can bring many independently scheduled jobs into evaluation at once.  The
# judge gateway then returns a short burst of 429s even though capacity becomes available a
# few seconds later.  Two retries at 2s/4s kept every caller in lockstep and was too short;
# even eight retries can expire while several five-platform groups share one RPM pool.  Use
# a bounded, jittered backoff long enough to drain that burst while retaining an explicit
# ``retries`` argument for tests and one-off callers.  The environment override is useful
# for emergency tuning without changing a frozen evaluation asset.
def _default_retries() -> int:
    # A reference audit is an offline validation job: a transiently saturated shared
    # judge must not turn a known-good frozen app into an infrastructure error.  Keep
    # ordinary candidate evaluation bounded at 16 retries, but give reference-only
    # checks enough headroom to outlive a long shared-RPM burst.  An explicit override
    # remains authoritative for both modes.
    fallback = (
        "64"
        if os.environ.get("RB_EVAL_TARGET", "").strip().lower() == "reference"
        else "16"
    )
    raw = os.environ.get("RB_VLM_JUDGE_RETRIES", fallback).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"RB_VLM_JUDGE_RETRIES must be an integer, got {raw!r}"
        ) from exc
    if not 0 <= value <= 64:
        raise ValueError(f"RB_VLM_JUDGE_RETRIES must be between 0 and 64, got {value}")
    return value


DEFAULT_RETRIES = _default_retries()
RETRY_BASE_SECONDS = 5.0
RETRY_MAX_SECONDS = 60.0


def _default_min_request_interval() -> float:
    raw = os.environ.get("RB_VLM_JUDGE_MIN_INTERVAL", "20").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"RB_VLM_JUDGE_MIN_INTERVAL must be numeric, got {raw!r}"
        ) from exc
    if not 0 <= value <= 300:
        raise ValueError(
            "RB_VLM_JUDGE_MIN_INTERVAL must be between 0 and 300 seconds, "
            f"got {value}"
        )
    return value


# A reference group runs 16 independent app jobs. Without a per-process floor, every
# successful response immediately triggers the next assertion and the aggregate request
# rate can exceed an endpoint's shared RPM limit. At 20s, one 16-job platform peaks
# around 48 requests/minute while preserving the requested runner concurrency.
DEFAULT_MIN_REQUEST_INTERVAL = _default_min_request_interval()
_REQUEST_SLOT_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _reserve_request_delay(interval: float | None = None) -> float:
    """Reserve one process-local request slot and return seconds to wait for it."""
    global _NEXT_REQUEST_AT
    if interval is None:
        interval = DEFAULT_MIN_REQUEST_INTERVAL
    if interval <= 0:
        return 0.0
    with _REQUEST_SLOT_LOCK:
        now = time.monotonic()
        slot = max(now, _NEXT_REQUEST_AT)
        _NEXT_REQUEST_AT = slot + interval
    return max(0.0, slot - now)


def _wait_for_request_slot() -> None:
    delay = _reserve_request_delay()
    if delay:
        time.sleep(delay)


async def _wait_for_request_slot_async() -> None:
    delay = _reserve_request_delay()
    if delay:
        import asyncio

        await asyncio.sleep(delay)


SYSTEM_PROMPT = (
    "You are a strict visual assertion judge. You will receive one or more screenshots "
    "and numbered assertions. In comparison mode, the original reference screenshot(s) "
    "come first and the candidate screenshot(s) come second. Judge each assertion only "
    "from visible evidence. Set pass=true only when the assertion is clearly and fully "
    "satisfied; any missing, incorrect, placeholder, or visibly mismatched element is a "
    "failure. Return ONLY a JSON array with exactly one object per assertion, in order: "
    '[{"pass": true, "reason": "brief evidence"}, '
    '{"pass": false, "reason": "brief mismatch"}].'
)

ASSERTION_USER_PREFIX = (
    "The screenshot(s) show the candidate application. Evaluate these assertions:\n"
)
COMPARISON_USER_PREFIX = (
    "The original reference screenshot(s) are followed by the candidate screenshot(s). "
    "Evaluate how the candidate matches the reference:\n"
)
PAIRED_USER_PREFIX = (
    "The screenshot images and assertions are paired in the same order: assertion 1 "
    "applies only to screenshot 1, assertion 2 only to screenshot 2, and so on. "
    "Evaluate each pair independently:\n"
)


def _default_batch_size() -> int:
    # Reference-only checks judge a trusted frozen app and can safely amortize the
    # shared endpoint RPM budget across the largest supported paired batch. Ordinary
    # candidate evaluation keeps the smaller batch so one malformed response has a
    # limited blast radius.  The explicit override remains authoritative.
    fallback = (
        "16"
        if os.environ.get("RB_EVAL_TARGET", "").strip().lower() == "reference"
        else "4"
    )
    raw = os.environ.get("RB_VLM_JUDGE_BATCH_SIZE", fallback).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"RB_VLM_JUDGE_BATCH_SIZE must be an integer, got {raw!r}"
        ) from exc
    if not 1 <= value <= 16:
        raise ValueError(
            f"RB_VLM_JUDGE_BATCH_SIZE must be between 1 and 16, got {value}"
        )
    return value


DEFAULT_BATCH_SIZE = _default_batch_size()


def resolve(model: str = "", base_url: str = "", api_key: str = "") -> dict:
    """Resolve judge settings from explicit arguments, environment, then defaults."""
    key = api_key or next((os.environ[e] for e in KEY_ENVS if os.environ.get(e)), "")
    cfg = {
        "model": model or os.environ.get("VLM_MODEL") or DEFAULT_MODEL,
        "base_url": (
            base_url or os.environ.get("VLM_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/"),
        "api_key": key,
    }
    if not cfg["model"]:
        raise ValueError("VLM_MODEL or an explicit model is required")
    if not cfg["base_url"]:
        raise ValueError("VLM_BASE_URL or an explicit base_url is required")
    return cfg


def _raw_data_url(image_path: str) -> str:
    ext = Path(image_path).suffix.lstrip(".").lower()
    mime = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else (ext or 'png')}"
    with open(image_path, "rb") as fh:
        return f"data:{mime};base64," + base64.b64encode(fh.read()).decode()


def _image_data_urls(image_path: str) -> list[str]:
    """Encode one image, tiling oversized web captures when Pillow is available."""
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"screenshot missing: {image_path}")
    try:
        from PIL import Image
    except ImportError:
        return [_raw_data_url(image_path)]

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    # Some OpenAI-compatible VLM endpoints reject an entire multi-image request when either
    # side of one image is 10px or smaller. A failed/cropped GUI capture can leave a 1x1 PNG;
    # pad that image instead of allowing it to poison the other assertions in its batch.
    # Padding (rather than stretching) keeps the visible evidence honest: the judge still
    # sees an effectively blank/invalid screenshot and can fail its assertion normally.
    min_dim = 16
    if width < min_dim or height < min_dim:
        padded = Image.new("RGB", (max(width, min_dim), max(height, min_dim)), "white")
        padded.paste(image, (0, 0))
        image = padded
        width, height = image.size
    max_dim, tile_height, max_tiles = 8000, 4000, 16
    if max(width, height) <= max_dim:
        crops = [image]
    else:
        budget = max_tiles * tile_height
        if height > budget:
            scale = budget / height
            image = image.resize(
                (max(1, round(width * scale)), budget), Image.Resampling.LANCZOS
            )
            width, height = image.size
        crops = []
        for top in range(0, height, tile_height):
            crop = image.crop((0, top, width, min(height, top + tile_height)))
            if crop.width > max_dim:
                scale = max_dim / crop.width
                crop = crop.resize(
                    (max_dim, max(1, round(crop.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            crops.append(crop)

    formats = (("PNG", None), ("JPEG", 90), ("JPEG", 80), ("JPEG", 65), ("JPEG", 50))
    for fmt, quality in formats:
        urls = []
        total = 0
        for crop in crops:
            buffer = BytesIO()
            kwargs = {"quality": quality} if quality is not None else {}
            crop.save(buffer, format=fmt, **kwargs)
            encoded = base64.b64encode(buffer.getvalue()).decode()
            total += len(encoded)
            urls.append(
                f"data:image/{'png' if fmt == 'PNG' else 'jpeg'};base64,{encoded}"
            )
        if total <= 5_000_000:
            return urls
    return urls


def _retry_delay(attempt: int, headers=None) -> float:
    """Return equal-jitter exponential backoff, honoring a bounded Retry-After."""
    ceiling = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2**attempt))
    delay = ceiling / 2 + random.uniform(0, ceiling / 2)
    try:
        retry_after = float((headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        retry_after = 0.0
    return min(RETRY_MAX_SECONDS, max(delay, retry_after))


def _is_retryable_http_error(status: int, detail: str) -> bool:
    """Return whether a gateway response is known to be transient.

    Some compatible gateways return ``InternalError.Algo.InvalidParameter`` with a
    "provided URL does not appear to be valid" message for a batch of perfectly
    valid base64 data URLs.  Replaying the identical body succeeds.  Treat only
    that narrow 400 signature as transient; ordinary 4xx responses (bad auth,
    unknown model, genuinely malformed requests) must still fail immediately.
    """
    if status in (418, 429) or status >= 500:
        return True
    lowered = (detail or "").lower()
    return (
        status == 400
        and "internalerror.algo.invalidparameter" in lowered
        and "provided url does not appear to be valid" in lowered
    )


def call(
    image_paths,
    prompt: str,
    *,
    cfg: dict | None = None,
    retries: int = DEFAULT_RETRIES,
    timeout: int = 180,
    system: str = SYSTEM_PROMPT,
) -> tuple[str | None, str | None]:
    """POST screenshots and a prompt, returning ``(text, error)``."""
    judge_cfg = cfg or resolve()
    body, error = _request_body(image_paths, prompt, judge_cfg, system)
    if error:
        return None, error

    last_error = "unknown error"
    for attempt in range(retries + 1):
        _wait_for_request_slot()
        request = urllib.request.Request(
            f"{judge_cfg['base_url']}/chat/completions",
            data=body,
            headers=_headers(judge_cfg),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _response_text(json.loads(response.read()))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode()[:200]
            except Exception:
                detail = ""
            last_error = f"HTTP {exc.code}: {detail}"
            if not _is_retryable_http_error(exc.code, detail) and 400 <= exc.code < 500:
                return None, last_error
            retry_headers = exc.headers
        except Exception as exc:  # transport, timeout, malformed JSON
            last_error = f"{type(exc).__name__}: {exc}"
            retry_headers = None
        if attempt < retries:
            delay = _retry_delay(attempt, retry_headers)
            print(
                f"[VLM] request failed ({last_error}); retry "
                f"{attempt + 1}/{retries} in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    return None, last_error


def _headers(cfg: dict) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }


def _request_body(
    image_paths, prompt: str, cfg: dict, system: str
) -> tuple[bytes | None, str | None]:
    if not cfg["api_key"]:
        return None, "no VLM api key (set VLM_API_KEY)"
    paths = [image_paths] if isinstance(image_paths, (str, Path)) else image_paths
    content: list[dict] = []
    try:
        for path in paths:
            for data_url in _image_data_urls(str(path)):
                content.append({"type": "image_url", "image_url": {"url": data_url}})
    except (OSError, ValueError) as exc:
        return None, str(exc)
    content.append({"type": "text", "text": prompt})
    return (
        json.dumps(
            {
                "model": cfg["model"],
                "temperature": 0,
                # Thinking-capable judges may spend several thousand tokens before
                # emitting the final JSON verdict.  A 4K cap truncated valid responses
                # before that JSON and surfaced as a misleading "unparseable reply".
                "max_tokens": DEFAULT_MAX_TOKENS,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
            }
        ).encode(),
        None,
    )


def _response_text(output: dict) -> tuple[str | None, str | None]:
    message = output["choices"][0]["message"]
    text = message.get("content") or message.get("reasoning_content") or ""
    return (text, None) if text.strip() else (None, "empty reply")


async def call_async(
    image_paths,
    prompt: str,
    *,
    cfg: dict | None = None,
    retries: int = DEFAULT_RETRIES,
    timeout: int = 180,
    system: str = SYSTEM_PROMPT,
) -> tuple[str | None, str | None]:
    """Async form of :func:`call`, used by Web's concurrent viewport jobs."""
    import asyncio

    import httpx

    judge_cfg = cfg or resolve()
    body, error = _request_body(image_paths, prompt, judge_cfg, system)
    if error:
        return None, error
    last_error = "unknown error"
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(retries + 1):
            await _wait_for_request_slot_async()
            try:
                response = await client.post(
                    f"{judge_cfg['base_url']}/chat/completions",
                    content=body,
                    headers=_headers(judge_cfg),
                )
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    if (
                        not _is_retryable_http_error(
                            response.status_code, response.text[:200]
                        )
                        and 400 <= response.status_code < 500
                    ):
                        return None, last_error
                    retry_headers = response.headers
                else:
                    return _response_text(response.json())
            except Exception as exc:  # transport, timeout, malformed JSON
                last_error = f"{type(exc).__name__}: {exc}"
                retry_headers = None
            if attempt < retries:
                delay = _retry_delay(attempt, retry_headers)
                print(
                    f"[VLM] request failed ({last_error}); retry "
                    f"{attempt + 1}/{retries} in {delay:.1f}s",
                    file=sys.stderr,
                )
                await asyncio.sleep(delay)
    return None, last_error


def _normalized_verdict(value) -> dict | None:
    if not isinstance(value, dict) or ("pass" not in value and "score" not in value):
        return None
    raw = value.get("pass", value.get("score"))
    if isinstance(raw, bool):
        passed = raw
    elif isinstance(raw, (int, float)) and raw in (0, 1):
        passed = bool(raw)
    elif isinstance(raw, str) and raw.strip().lower() in {
        "true",
        "false",
        "yes",
        "no",
        "pass",
        "fail",
        "1",
        "0",
    }:
        passed = raw.strip().lower() in {"true", "yes", "pass", "1"}
    elif raw is None:
        passed = None
    else:
        return None
    return {"pass": passed, "reason": str(value.get("reason", ""))}


def parse_verdicts(text: str, expected_count: int) -> list[dict]:
    """Parse one ordered verdict per assertion, preserving unjudged entries."""
    if not text:
        return [{"pass": None, "error": "empty reply"}] * expected_count
    stripped = re.sub(r"```(?:json)?|```", "", text, flags=re.IGNORECASE).strip()
    candidates = []
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        values = value if isinstance(value, list) else [value]
        parsed = [item for item in (_normalized_verdict(v) for v in values) if item]
        if parsed:
            candidates = parsed
            break
    if not candidates and expected_count == 1:
        head = stripped[:24].upper()
        if head.startswith("YES"):
            candidates = [{"pass": True, "reason": stripped[:200]}]
        elif head.startswith("NO"):
            candidates = [{"pass": False, "reason": stripped[:200]}]
    if not candidates:
        return [
            {"pass": None, "error": "unparseable reply", "reason": stripped[:200]}
            for _ in range(expected_count)
        ]
    if len(candidates) < expected_count:
        candidates.extend(
            {
                "pass": None,
                "error": "missing verdict in judge reply",
                "reason": "",
            }
            for _ in range(expected_count - len(candidates))
        )
    return candidates[:expected_count]


def parse_verdict(text: str) -> dict:
    """Compatibility helper for a single assertion."""
    return parse_verdicts(text, 1)[0]


def judge_assertions(
    image_paths,
    assertions: list[str],
    *,
    comparison: bool = False,
    paired: bool = False,
    cfg: dict | None = None,
    retries: int = DEFAULT_RETRIES,
) -> list[dict]:
    """Judge a batch of assertions with the canonical five-platform prompt."""
    if comparison and paired:
        raise ValueError("comparison and paired modes are mutually exclusive")
    prefix = (
        COMPARISON_USER_PREFIX
        if comparison
        else PAIRED_USER_PREFIX if paired else ASSERTION_USER_PREFIX
    )
    numbered = "\n".join(f"{index}. {item}" for index, item in enumerate(assertions, 1))
    text, error = call(
        image_paths,
        f"{prefix}{numbered}",
        cfg=cfg,
        retries=retries,
    )
    if error:
        return [{"pass": None, "error": error} for _ in assertions]
    return parse_verdicts(text or "", len(assertions))


async def judge_assertions_async(
    image_paths,
    assertions: list[str],
    *,
    comparison: bool = False,
    paired: bool = False,
    cfg: dict | None = None,
    retries: int = DEFAULT_RETRIES,
) -> list[dict]:
    """Async Web adapter with the same canonical prompt and verdict parser."""
    if comparison and paired:
        raise ValueError("comparison and paired modes are mutually exclusive")
    prefix = (
        COMPARISON_USER_PREFIX
        if comparison
        else PAIRED_USER_PREFIX if paired else ASSERTION_USER_PREFIX
    )
    numbered = "\n".join(f"{index}. {item}" for index, item in enumerate(assertions, 1))
    text, error = await call_async(
        image_paths,
        f"{prefix}{numbered}",
        cfg=cfg,
        retries=retries,
    )
    if error:
        return [{"pass": None, "error": error} for _ in assertions]
    return parse_verdicts(text or "", len(assertions))


def judge_screenshot(
    image_path,
    assertion: str,
    *,
    cfg: dict | None = None,
    retries: int = DEFAULT_RETRIES,
) -> dict:
    """Judge one assertion against one screenshot. Never raises."""
    return judge_assertions(image_path, [assertion], cfg=cfg, retries=retries)[0]


def _load_assertions(results_dir: Path) -> dict[str, str]:
    """Load the one assertion map accepted by every desktop harness.

    Windows writes an append-only ``screenshot_asserts.jsonl`` as well as the final
    ``assertions.json``.  Prefer the jsonl entries so a late process crash cannot erase
    assertions that were already emitted, then fill any remaining names from the JSON map.
    Linux and macOS normally provide only the JSON map.
    """
    assertions: dict[str, str] = {}
    jsonl_path = results_dir / "screenshot_asserts.jsonl"
    if jsonl_path.is_file():
        try:
            for line in jsonl_path.read_text(encoding="utf-8-sig").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                assertions[str(item["screenshot"])] = str(item["description"])
        except (OSError, ValueError, KeyError, TypeError):
            assertions = {}

    assertions_file = results_dir / "assertions.json"
    if assertions_file.is_file():
        try:
            value = json.loads(assertions_file.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                for name, description in value.items():
                    assertions.setdefault(str(name), str(description))
        except (OSError, ValueError, TypeError):
            pass
    return assertions


def _assertion_set_digest(assertions: dict[str, str]) -> str:
    """Identify the exact frozen assertion set used by a partial checkpoint."""
    payload = json.dumps(
        assertions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Replace a JSON result without ever exposing a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _checkpoint_payload(
    *,
    assertions: dict[str, str],
    digest: str,
    results: dict,
    passed: int,
    total: int,
    errors: int,
    skipped: int,
    missing: int,
    complete: bool,
) -> dict:
    return {
        "schema_version": 1,
        "assertion_set_sha256": digest,
        "complete": complete,
        "passed": passed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total else 0,
        "authored": len(assertions),
        "errors": errors,
        "judge_errors": errors,
        "skipped": skipped,
        "missing_screenshots": missing,
        "results": results,
    }


def _load_checkpoint(path: Path | None, digest: str) -> dict:
    """Return only completed model verdicts from a matching checkpoint.

    Judge errors are deliberately retried. Missing screenshots are also rechecked because a
    restarted producer may have finished copying them after the prior process stopped.
    """
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    if payload.get("assertion_set_sha256") != digest:
        return {}
    reusable = {}
    for name, verdict in (payload.get("results") or {}).items():
        if (
            isinstance(verdict, dict)
            and isinstance(verdict.get("pass"), bool)
            and not verdict.get("missing")
        ):
            reusable[str(name)] = verdict
    return reusable


def evaluate_dir(
    results_dir: str,
    *,
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    cfg: dict | None = None,
    missing_as_fail: bool = False,
    max_error_rate: float = 0.10,
    partial_output: str | Path = "",
    batch_size: int | None = None,
):
    """Judge one screenshot directory and return the canonical VLM summary.

    A missing candidate screenshot is a benchmark failure only when the caller selected a
    frozen denominator. A transport/auth/parse error is always an infrastructure error and is
    excluded from ``total``.
    """
    d = Path(results_dir)
    assertions = _load_assertions(d)
    if not assertions:
        print(f"No assertions in {d}")
        return None

    judge_cfg = cfg or resolve(
        model=model or "", base_url=base_url or "", api_key=api_key or ""
    )
    partial_path = Path(partial_output) if partial_output else None
    effective_batch_size = DEFAULT_BATCH_SIZE if batch_size is None else batch_size
    if not 1 <= effective_batch_size <= 16:
        raise ValueError("batch_size must be between 1 and 16")
    assertion_digest = _assertion_set_digest(assertions)
    results: dict = _load_checkpoint(partial_path, assertion_digest)
    passed = total = errors = skipped = missing = 0

    def checkpoint(*, complete: bool = False) -> None:
        if partial_path is None:
            return
        _write_json_atomic(
            partial_path,
            _checkpoint_payload(
                assertions=assertions,
                digest=assertion_digest,
                results=results,
                passed=passed,
                total=total,
                errors=errors,
                skipped=skipped,
                missing=missing,
                complete=complete,
            ),
        )

    pending: list[tuple[str, str, Path]] = []
    for fname, assertion in assertions.items():
        previous = results.get(fname)
        if isinstance(previous, dict) and isinstance(previous.get("pass"), bool):
            total += 1
            if previous["pass"]:
                passed += 1
            print(f"  RESUME {fname}: completed verdict from partial checkpoint")
            continue
        img = d / fname
        if not img.exists():
            if missing_as_fail:
                total += 1
                missing += 1
                results[fname] = {
                    "pass": False,
                    "reason": "screenshot not produced by candidate",
                    "missing": True,
                }
                print(f"  MISS {fname} (counted as fail)")
            else:
                skipped += 1
                print(f"  SKIP {fname} (not found)")
            checkpoint()
            continue
        pending.append((fname, assertion, img))

    for offset in range(0, len(pending), effective_batch_size):
        batch = pending[offset : offset + effective_batch_size]
        if len(batch) == 1:
            verdicts = [judge_screenshot(str(batch[0][2]), batch[0][1], cfg=judge_cfg)]
        else:
            verdicts = judge_assertions(
                [str(item[2]) for item in batch],
                [item[1] for item in batch],
                paired=True,
                cfg=judge_cfg,
            )
        for (fname, _assertion, _img), verdict in zip(batch, verdicts):
            results[fname] = verdict
            if verdict.get("pass") is None:
                errors += 1
                print(
                    f"  ERR  {fname}: "
                    f"{verdict.get('error') or verdict.get('reason', '')}"
                )
                checkpoint()
                continue
            total += 1
            if verdict["pass"]:
                passed += 1
            print(
                f"  {'PASS' if verdict['pass'] else 'FAIL'} {fname}: "
                f"{verdict.get('reason', '')}"
            )
            checkpoint()

    checkpoint(complete=True)

    pct = f"{100 * passed / total:.0f}%" if total else "n/a"
    tail = (
        f" — {errors} unjudged, {skipped} skipped, {missing} missing-as-fail"
        if (errors or skipped or missing)
        else ""
    )
    print(f"\n  Score: {passed}/{total} ({pct}){tail}")
    out = d / "vlm_results.json"
    json.dump(results, open(out, "w"), indent=2)
    print(f"  Saved: {out}")
    summary = {
        "passed": passed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total else 0,
        "authored": len(assertions),
        "errors": errors,
        "judge_errors": errors,
        "skipped": skipped,
        "missing_screenshots": missing,
        "results": results,
    }
    if errors:
        attempted = total + errors
        error_rate = errors / attempted if attempted else 1.0
        summary["judge_error_rate"] = round(error_rate, 4)
        message = f"vlm_judge_errors:{errors}/{attempted}"
        summary["error" if error_rate > max_error_rate else "warning"] = message
    return summary


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument(
        "dirs", nargs="+", help="Directories with assertions.json + screenshots"
    )
    p.add_argument(
        "--model",
        default="",
        help="VLM model (required unless VLM_MODEL is set)",
    )
    p.add_argument("--base-url", default="", help="override VLM_BASE_URL")
    p.add_argument(
        "--missing-as-fail",
        action="store_true",
        help="count an authored assertion with no screenshot as a failed assertion",
    )
    p.add_argument(
        "--max-error-rate",
        type=float,
        default=float(os.environ.get("VLM_MAX_JUDGE_ERROR_RATE", "0.10")),
        help="judge-error rate above which the command fails (default: 0.10)",
    )
    p.add_argument(
        "--output",
        default="",
        help="Write an aggregate {passed,total,authored,errors,skipped} JSON here",
    )
    args = p.parse_args()

    cfg = resolve(model=args.model, base_url=args.base_url)
    print(f"judge: model={cfg['model']} base_url={cfg['base_url']}")
    if not cfg["api_key"]:
        print(f"  WARNING: no API key in {'/'.join(KEY_ENVS)} — every call will fail")

    agg = {
        "passed": 0,
        "total": 0,
        "authored": 0,
        "errors": 0,
        "skipped": 0,
        "missing_screenshots": 0,
        "dirs": {},
    }
    for d in args.dirs:
        print(f"\n=== {d} ===")
        r = evaluate_dir(
            d,
            model=args.model,
            cfg=cfg,
            missing_as_fail=args.missing_as_fail,
            max_error_rate=args.max_error_rate,
            partial_output=(
                str(Path(args.output).with_name("vlm_results.partial.json"))
                if args.output and len(args.dirs) == 1
                else ""
            ),
        )
        if r:
            for k in (
                "passed",
                "total",
                "authored",
                "errors",
                "skipped",
                "missing_screenshots",
            ):
                agg[k] += r[k]
            agg["dirs"][d] = {
                k: r[k]
                for k in (
                    "passed",
                    "total",
                    "authored",
                    "errors",
                    "skipped",
                    "missing_screenshots",
                )
            }
    agg["pass_rate"] = round(agg["passed"] / agg["total"], 4) if agg["total"] else 0
    agg["judge_errors"] = agg["errors"]
    if agg["errors"]:
        attempted = agg["total"] + agg["errors"]
        error_rate = agg["errors"] / attempted if attempted else 1.0
        agg["judge_error_rate"] = round(error_rate, 4)
        message = f"vlm_judge_errors:{agg['errors']}/{attempted}"
        agg["error" if error_rate > args.max_error_rate else "warning"] = message
    if args.output:
        _write_json_atomic(Path(args.output), agg)
        print(
            f"\nAggregate: {agg['passed']}/{agg['total']} of {agg['authored']} authored"
            f" ({agg['errors']} unjudged, {agg['skipped']} missing) -> {args.output}"
        )
    if agg.get("error"):
        raise SystemExit(1)
