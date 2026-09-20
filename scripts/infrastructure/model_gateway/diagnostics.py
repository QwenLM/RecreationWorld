#!/usr/bin/env python3
"""Extract the real upstream rejection from deployment model-gateway logs.

The problem this solves, from a real macOS run (2026-08-21): every one of five acceptance jobs
reported exactly one thing to the operator --

    supplier-proxy: upstream error: preheader timeout

-- which reads as a network fault and is not one. The Mac's loopback proxy has a pre-header
watchdog: when the pod-side proxy sends no response headers in time it writes a 504 and
DESTROYS the connection, so it never receives the upstream response and cannot report it. The
actual cause was only in the pod's own log (144 MB), and getting at it meant downloading an
81 MB job artifact by hand:

    POST .../protocol/anthropic/v1/messages "HTTP/1.1 400 Bad Request"
    'error_source': 'CLIENT_ERROR'
    'error_message': '内容安全检测不通过，请检查您的请求是否存在涉政、涉恐、涉黄、涉内部数据等信息'

The pod can read that file itself. One scan turns an opaque 40-minute hang into a named cause
in the job log.

Also reports the retry volume, because that is its own finding: a 400 CLIENT_ERROR is permanent
-- the flagged content sits in the conversation history, so every later request carries it -- and
PROXY_MAX_RETRIES=1000 turned a fast, diagnosable failure into 374 logged 400s over 40 minutes
and a 144 MB log. The Anthropic SDK logs "Not retrying"; the proxy's own loop retries anyway.

Streams the file line by line: these logs reach hundreds of MB and must never be read whole.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional

# `httpx - INFO - [anthropic] HTTP Request: POST <url> "HTTP/1.1 400 Bad Request"`
_STATUS_RE = re.compile(
    r'HTTP Request:\s+\w+\s+(\S+)\s+"HTTP/[\d.]+\s+(\d{3})\s+([^"]*)"'
)
# Some compatible gateways wrap rejection details; their logs double the escapes.
_ERRMSG_RE = re.compile(r"error_message\\*'?\s*:\s*\\*'([^'\\]{1,400})")
_MODEL_RE = re.compile(r"model_name\\*'?\s*:\s*\\*'([^'\\]{1,120})")
_SOURCE_RE = re.compile(r"error_source\\*'?\s*:\s*\\*'([^'\\]{1,80})")
_ATTEMPT_RE = re.compile(r"Attempt (\d+)/(\d+) failed")
# leading timestamp of either log shape: "2026-08-21 15:01:40,330 - ..." or {"time":"..."}
_TS_RE = re.compile(r'^\s*[\{"]*(?:"time":")?(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)')

# A non-2xx that is retryable says nothing about the run; a 4xx CLIENT_ERROR is the whole story.
PERMANENT = {400, 401, 403, 404, 413, 422}


def scan(path: str, *, max_bytes: int = 512 * 1024 * 1024) -> Optional[dict]:
    """Summarise the upstream failures in a proxy log, or None if there are none.

    Returns ``{status, reason, url, count, first_ts, last_ts, error_message, error_source,
    model_name, max_attempt, permanent}``. Bytes past ``max_bytes`` are not read.
    """
    if not path or not os.path.isfile(path):
        return None
    by_status: dict[int, dict] = {}
    err_msg = err_src = model = None
    max_attempt = 0
    retry_ceiling = 0
    read = 0
    with open(path, "rb") as fh:
        for raw in fh:
            read += len(raw)
            if read > max_bytes:
                break
            # latin1 never raises, and every pattern here is ASCII-anchored; the one non-ASCII
            # field (error_message) is re-decoded as UTF-8 below.
            line = raw.decode("latin1")
            m = _STATUS_RE.search(line)
            if m:
                code = int(m.group(2))
                if code >= 400:
                    e = by_status.setdefault(
                        code,
                        {
                            "count": 0,
                            "reason": m.group(3).strip(),
                            "url": m.group(1),
                            "first_ts": None,
                            "last_ts": None,
                        },
                    )
                    e["count"] += 1
                    ts = _TS_RE.match(line)
                    if ts:
                        e["first_ts"] = e["first_ts"] or ts.group(1)
                        e["last_ts"] = ts.group(1)
            if err_msg is None:
                em = _ERRMSG_RE.search(line)
                if em:
                    err_msg = em.group(1).encode("latin1").decode("utf-8", "replace")
                    sm = _SOURCE_RE.search(line)
                    if sm:
                        err_src = sm.group(1)
                    mm = _MODEL_RE.search(line)
                    if mm:
                        model = mm.group(1)
            am = _ATTEMPT_RE.search(line)
            if am:
                max_attempt = max(max_attempt, int(am.group(1)))
                retry_ceiling = max(retry_ceiling, int(am.group(2)))
    if not by_status:
        return None
    # The permanent error is the story even when a transient one is noisier.
    perm = [c for c in by_status if c in PERMANENT]
    status = (
        max(perm, key=lambda c: by_status[c]["count"])
        if perm
        else max(by_status, key=lambda c: by_status[c]["count"])
    )
    e = by_status[status]
    return {
        "status": status,
        "reason": e["reason"],
        "url": e["url"],
        "count": e["count"],
        "first_ts": e["first_ts"],
        "last_ts": e["last_ts"],
        "error_message": err_msg,
        "error_source": err_src,
        "model_name": model,
        "max_attempt": max_attempt,
        "retry_ceiling": retry_ceiling,
        "permanent": status in PERMANENT,
        "all_statuses": {str(k): v["count"] for k, v in sorted(by_status.items())},
    }


def render(d: Optional[dict]) -> str:
    """Operator-facing lines. Empty string when there is nothing to say."""
    if not d:
        return ""
    out = [
        "  UPSTREAM REJECTED THE REQUEST — this, not any local timeout, is the cause:",
        f"    HTTP {d['status']} {d['reason']} x{d['count']}"
        + (f"  (first {d['first_ts']}, last {d['last_ts']})" if d["first_ts"] else ""),
    ]
    if d.get("error_message"):
        src = f" [{d['error_source']}]" if d.get("error_source") else ""
        out.append(f"    upstream says{src}: {d['error_message']}")
    if d.get("model_name"):
        out.append(f"    model: {d['model_name']}")
    if d.get("url"):
        out.append(f"    endpoint: {d['url']}")
    if d["permanent"] and d.get("max_attempt", 0) > 1:
        out.append(
            f"    RETRIED {d['max_attempt']}x (ceiling {d['retry_ceiling']}) — a {d['status']} is "
            "PERMANENT here: the rejected content stays in the conversation history, so every "
            "retry carries it. Lower PROXY_MAX_RETRIES for this class."
        )
    if len(d.get("all_statuses", {})) > 1:
        out.append(f"    all non-2xx seen: {d['all_statuses']}")
    return "\n".join(out)


# The proxy sidecar's log is written by the deployment platform PLATFORM, not by rb, so its name is not ours to
# fix. Discovered by glob so this works for whatever the sidecar happens to be called on each
# platform instead of pinning one filename that only macOS uses.
LOG_GLOBS = ("logs/*proxy*.log", "logs/**/*proxy*.log", "*proxy*.log")


def find_logs(root: str, *, min_bytes: int = 1024) -> list[str]:
    """Proxy-sidecar logs under ``root``, largest first. Empty when there are none.

    Largest first because the log that grew is the one that was retrying.
    """
    import glob as _glob

    if not root or not os.path.isdir(root):
        return []
    hits: dict[str, int] = {}
    for pat in LOG_GLOBS:
        for f in _glob.glob(os.path.join(root, pat), recursive=True):
            try:
                if os.path.isfile(f) and os.path.getsize(f) >= min_bytes:
                    hits[os.path.realpath(f)] = os.path.getsize(f)
            except OSError:
                continue
    return [f for f, _ in sorted(hits.items(), key=lambda kv: -kv[1])]


def diagnose_dir(root: str) -> str:
    """Rendered diagnosis for the first proxy log under ``root`` that has an upstream failure.

    Best-effort by contract: this runs on a failure path, so it must never raise and never turn a
    diagnosis into a second failure.
    """
    try:
        for path in find_logs(root):
            d = scan(path)
            if d:
                return f"[proxy-diagnosis] {path}\n" + render(d)
    except Exception as exc:  # noqa: BLE001 - diagnosis must not mask the real failure
        return f"[proxy-diagnosis] unavailable ({type(exc).__name__}: {exc})"
    return ""


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            "usage: python3 -m infrastructure.model_gateway.diagnostics <log> [...]",
            file=sys.stderr,
        )
        return 2
    found = False
    for path in args:
        d = scan(path)
        if d:
            found = True
            print(f"[proxy-diagnosis] {path}")
            print(render(d))
    if not found:
        print("[proxy-diagnosis] no upstream non-2xx found in: " + ", ".join(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
