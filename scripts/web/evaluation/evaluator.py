"""Main evaluation orchestrator for RecreationBench Web.

Coordinates the full evaluation pipeline:
1. Serve agent output
2. Take agent screenshots
3. Run anti-cheat checks
4. Run four-dimensional Playwright tests
5. Optionally run VLM judge
6. Aggregate scores
"""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import re
import signal
from pathlib import Path

from core import runtime_assets

from config import DEFAULT_EVAL_CONFIG
from serving.site_server import SiteServer

logger = logging.getLogger(__name__)

# Final-score ceiling applied when ANY integrity gate confirms a violation
# (screenshot-paste, read-the-answer leak, verbatim scrape, or verbatim DOM copy).
# Shared with the per-gate caps so the penalty is consistent and auditable.
INTEGRITY_CAP = 0.10

# Playwright's operation timeout is not a reliable upper bound for the complete
# capture fallback chain: a renderer can remain wedged after the first timed-out
# full-page paint and keep the retry/context teardown alive indefinitely.  Bound
# each page/viewport at the orchestration layer so one pathological reference
# page cannot occupy an deployment platform worker for hours.  A timed-out view is deliberately
# left missing and is scored as such by the existing evaluator contract.
WEB_CAPTURE_DEADLINE_SECONDS = 600
WEB_BROWSER_CLOSE_DEADLINE_SECONDS = 30
ANTI_CHEAT_SCRIPT = runtime_assets.load_text("web/runtime_assets/anti_cheat.js")

# Functional assertion tiers — content-DOMINANT (aggressive strictness): the score
# is driven by content reproduction; near-saturated gate/nav checks contribute little
# so they can't inflate it. On easel.ly this pulls 4.6 functional toward ~0.50.
FUNC_TIER_WEIGHTS = {"gate": 0.1, "struct": 0.15, "content": 1.0, "routing": 0.4,
                     "interaction": 0.4, "other": 0.3}


def _playwright_driver_pid(browser) -> int | None:
    """Best-effort lookup of the Node driver that owns launched browsers."""
    try:
        return int(browser._impl_obj._connection._transport._proc.pid)
    except (AttributeError, TypeError, ValueError):
        return None


def _reap_stranded_chromium(browser) -> list[int]:
    """Kill Chromium process groups left behind after ``browser.close()``.

    Playwright launches each browser as a direct child and process-group leader of
    its Node driver.  A renderer wedged by a very tall page can let the protocol
    close return while that process group keeps hundreds of MiB resident.  Capture
    is strictly sequential here, so any Chromium child still attached to this
    evaluator's private driver after close is stranded and safe to reap.
    """
    driver_pid = _playwright_driver_pid(browser)
    if driver_pid is None:
        return []
    try:
        children = Path(
            f"/proc/{driver_pid}/task/{driver_pid}/children"
        ).read_text(encoding="utf-8").split()
    except (OSError, ValueError):
        return []

    killed = []
    for raw_pid in children:
        try:
            pid = int(raw_pid)
            comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
            pgid = os.getpgid(pid)
            if "chrome" not in comm.lower() or pgid <= 1 or pgid == os.getpgrp():
                continue
            os.killpg(pgid, signal.SIGKILL)
            killed.append(pid)
        except (OSError, ValueError):
            continue
    return killed


def _consume_background_task(task: asyncio.Task) -> None:
    """Retrieve a detached task's result so late Playwright errors stay quiet."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _await_hard_deadline(awaitable, timeout: float, on_timeout=None):
    """Bound an awaitable without waiting indefinitely for cancellation.

    ``asyncio.wait_for`` cancels the child at its deadline and then waits for the
    cancellation to finish. Playwright teardown can ignore that cancellation
    while its Node driver is wedged, turning a nominal timeout into an unbounded
    wait. ``asyncio.wait`` lets the caller reclaim the browser process and move
    on immediately; the detached task is drained when the transport unwinds.
    """
    task = asyncio.create_task(awaitable)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()

    task.cancel()
    if on_timeout is not None:
        on_timeout()
    task.add_done_callback(_consume_background_task)
    raise asyncio.TimeoutError


@asynccontextmanager
async def _isolated_browser(browser_type, stage: str):
    """Give one evaluator stage exclusive ownership of a browser process.

    Screenshot capture may force-reap every Chromium child left under the shared
    Playwright driver.  Keeping anti-cheat or quality scoring browsers alive while
    capture runs therefore lets cleanup for one stage kill another stage's browser.
    Stage-scoped browsers make that ownership boundary explicit.
    """
    browser = await browser_type.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    try:
        yield browser
    finally:
        killed = []
        try:
            await _await_hard_deadline(
                browser.close(),
                WEB_BROWSER_CLOSE_DEADLINE_SECONDS,
                on_timeout=lambda: killed.extend(_reap_stranded_chromium(browser)),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "%s browser close exceeded %ss; forcing renderer cleanup",
                stage,
                WEB_BROWSER_CLOSE_DEADLINE_SECONDS,
            )
        except Exception as e:
            logger.warning("%s browser close failed: %s", stage, e)
        finally:
            killed.extend(_reap_stranded_chromium(browser))
            killed = list(dict.fromkeys(killed))
            if killed:
                logger.warning(
                    "Force-killed stranded Chromium after %s: %s",
                    stage,
                    ",".join(map(str, killed)),
                )


def _functional_tier(title: str) -> str:
    t = (title or "").lower()
    # depth>=1 interaction/reach tests, titled "[rb:d<N>] ..." by the generator. MUST be
    # checked first: the reach variants read '... via link "X"' and would otherwise fall
    # into "struct" (0.15), scoring an interaction assertion as a link-presence check.
    if t.startswith("[rb:d"):
        return "interaction"
    if "loads without error" in t or "title matches" in t:
        return "gate"
    if re.search(r": h[1-6] ", t):
        return "content"
    if " link " in t:
        return "struct"
    if "navigate to" in t:
        return "routing"
    return "other"


def _weighted_functional_score(details):
    """Tier-weighted functional pass-rate from test detail records (each
    {title, status}). Returns None if there are no details (caller then falls
    back to the raw pass-rate)."""
    if not details:
        return None
    wt = wp = 0.0
    for d in details:
        w = FUNC_TIER_WEIGHTS.get(_functional_tier(d.get("title", "")), 0.5)
        wt += w
        if d.get("status") == "passed":
            wp += w
    return (wp / wt) if wt > 0 else None


def _detail_group(d: dict) -> str:
    """Group key '<source>/<spec_stem>' for a functional test detail, parsed from
    its flattened file name '<source>__<spec>.spec.ts' (test_runner flattens
    scripted/ and agent_gen/ specs as '<parent>__<name>'). Falls back to
    'other/<stem>' when the file is missing or unflattened."""
    fn = d.get("file", "") or ""
    stem = fn[:-len(".spec.ts")] if fn.endswith(".spec.ts") else fn
    if "__" in stem:
        source, spec = stem.split("__", 1)
    else:
        source, spec = "other", stem
    return f"{source}/{spec}" if spec else ""


def _functional_subscores(details) -> dict:
    """Per-spec-file functional sub-scores, keyed '<source>/<spec_stem>'
    (e.g. 'agent_gen/functional', 'scripted/functional',
    'agent_gen/functional_interaction'). Each carries the binary pass-rate AND
    the tier-weighted score over just that file's tests.

    Additive/diagnostic — does NOT feed final_score or the pooled functional
    dimension. This is the field that makes an authorship group silently
    contributing zero visible checks: without it, a group that fails to compile is
    indistinguishable in the metrics from a group that simply passed everything.
    Returns {} when no detail carries file attribution."""
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for d in details or []:
        key = _detail_group(d)
        if key:
            groups[key].append(d)
    out = {}
    for key, ds in groups.items():
        total = len(ds)
        passed = sum(1 for d in ds if d.get("status") == "passed")
        out[key] = {
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "weighted": round(_weighted_functional_score(ds) or 0.0, 4),
            "total": total,
            "passed": passed,
            "failed": total - passed,
        }
    return out


def _collect_test_details(test_results: dict) -> dict:
    """Per-test pass/fail records by dimension for the standalone test_details.json
    artifact: {dim: [{title, status, file, [tier]}]}. Functional records also carry
    their assertion tier. Visual runs no specs, so it is absent."""
    out = {}
    for dim, r in (test_results or {}).items():
        ds = getattr(r, "details", None)
        if ds is None and isinstance(r, dict):
            ds = r.get("details")
        recs = []
        for d in (ds or []):
            rec = {
                "title": d.get("title", ""),
                "status": d.get("status"),
                "file": d.get("file", ""),
            }
            if dim == "functional":
                rec["tier"] = _functional_tier(d.get("title", ""))
            recs.append(rec)
        out[dim] = recs
    return out


async def _take_agent_screenshots(browser_type, agent_url: str, target_pages: list[dict],
                                   output_dir: Path, layout_dir: Path = None,
                                   region_dir: Path = None, dom_dir: Path = None):
    """Take screenshots of agent output at both viewports, plus the matching
    bbox-layout JSON (#7) and region screenshots (#8).

    Uses the SAME capture pipeline as GT (gt_generator.capture_screenshot) — fresh
    per-viewport context, real mobile device emulation, lazy-load scroll, fonts-
    ready, animation freeze, seeded RNG, full-page capture — so GT and agent are
    methodologically symmetric. Each viewport gets a fresh browser process: very
    tall mobile pages can leave Chromium renderers holding several GiB even after
    their context closes, which otherwise poisons every later page in the job.
    """
    from config import RESOLUTIONS
    from evaluation.gt_generator import (
        ScreencastFrameTimeout,
        capture_screenshot,
        extract_dom_snapshot,
    )

    for page_info in target_pages:
        page_id = page_info["id"] if isinstance(page_info, dict) else page_info
        page_path = page_info.get("path", "/") if isinstance(page_info, dict) else "/"
        url = agent_url.rstrip("/") + page_path

        for vp in RESOLUTIONS:
            output_path = str(output_dir / page_id / f"{vp['name']}.png")
            layout_path = (str(layout_dir / page_id / f"{vp['name']}.json")
                           if layout_dir is not None else None)
            rdir = (str(region_dir / page_id)
                    if (region_dir is not None and vp.get("name") == "desktop") else None)
            browser = None
            capture_timeout_killed = []
            try:
                browser = await browser_type.launch(
                    headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                await _await_hard_deadline(
                    capture_screenshot(
                        browser,
                        url,
                        output_path,
                        vp,
                        layout_path=layout_path,
                        region_dir=rdir,
                    ),
                    WEB_CAPTURE_DEADLINE_SECONDS,
                    on_timeout=lambda: capture_timeout_killed.extend(
                        _reap_stranded_chromium(browser)
                    ),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Agent screenshot exceeded %ss for %s/%s; marking this "
                    "view missing and continuing evaluation",
                    WEB_CAPTURE_DEADLINE_SECONDS,
                    page_id,
                    vp["name"],
                )
            except ScreencastFrameTimeout as e:
                logger.warning(
                    "Agent screenshot received no Chromium frame for %s/%s: %s; "
                    "marking this view missing and continuing evaluation",
                    page_id,
                    vp["name"],
                    e,
                )
            except Exception as e:
                logger.warning("Agent screenshot failed for %s/%s: %s",
                             page_id, vp["name"], e)
            finally:
                if browser is not None:
                    close_timeout_killed = []
                    try:
                        await _await_hard_deadline(
                            browser.close(),
                            WEB_BROWSER_CLOSE_DEADLINE_SECONDS,
                            on_timeout=lambda: close_timeout_killed.extend(
                                _reap_stranded_chromium(browser)
                            ),
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Browser close exceeded %ss after %s/%s; "
                            "forcing renderer cleanup",
                            WEB_BROWSER_CLOSE_DEADLINE_SECONDS,
                            page_id,
                            vp["name"],
                        )
                    except Exception as e:
                        logger.warning("Agent screenshot browser close failed for %s/%s: %s",
                                       page_id, vp["name"], e)
                    finally:
                        killed = list(dict.fromkeys(
                            capture_timeout_killed
                            + close_timeout_killed
                            + _reap_stranded_chromium(browser)
                        ))
                        if killed:
                            logger.warning(
                                "Force-killed stranded Chromium after %s/%s: %s",
                                page_id,
                                vp["name"],
                                ",".join(map(str, killed)),
                            )

        # DOM snapshot once per page (desktop context, mirrors GT capture) — used
        # by the continuous structural (DOM-similarity) and GT-relative quality scorers.
        if dom_dir is not None:
            browser = None
            try:
                browser = await browser_type.launch(
                    headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                ctx = await browser.new_context(viewport={"width": 1920, "height": 1080})
                try:
                    p = await ctx.new_page()
                    dom = await extract_dom_snapshot(p, url)
                finally:
                    await ctx.close()
                dpath = Path(dom_dir) / page_id
                dpath.mkdir(parents=True, exist_ok=True)
                (dpath / "dom_snapshot.json").write_text(
                    json.dumps(dom, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning("Agent DOM capture failed for %s: %s", page_id, e)
            finally:
                if browser is not None:
                    try:
                        await asyncio.wait_for(browser.close(), timeout=30)
                    except Exception as e:
                        logger.warning("Agent DOM browser close failed for %s: %s", page_id, e)


async def _anti_cheat_check(page, agent_url: str) -> dict:
    """Run anti-cheat detection on agent output.

    Checks for screenshot-paste cheating:
    - Large image covering >70% viewport with <50 total elements
    - No selectable text
    - Trivial DOM structure
    """
    try:
        await page.set_viewport_size({"width": 1920, "height": 1080})
        await page.goto(agent_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        result = await page.evaluate(ANTI_CHEAT_SCRIPT)

        is_blank = (result["total_elements"] < 10
                    and not result["text_selectable"]
                    and not result["large_image_cover"])

        is_cheating = (result["large_image_cover"]
                      and (not result["text_selectable"]
                           or (result["max_dom_depth"] < 5
                               and result["main_children_count"] < 3)))

        return {
            "is_cheating": is_cheating,
            "is_blank": is_blank,
            "details": result,
        }

    except Exception as e:
        logger.warning("Anti-cheat check failed: %s", e)
        return {"is_cheating": False, "is_blank": False, "error": str(e)}


async def _collect_live_browser_metrics(
    browser_type,
    agent_url: str,
    serve_dir: Path,
    page_info_list: list[dict],
    target_pages: list[str],
    agent_screenshots_dir: Path,
    agent_layout_dir: Path,
    agent_region_dir: Path,
    agent_dom_dir: Path,
    gt_dom_dir: Path,
) -> tuple[dict, dict]:
    """Capture browser-backed evidence without sharing browser lifetimes.

    ``_take_agent_screenshots`` owns and may force-reap Chromium processes.  The
    anti-cheat and quality stages must therefore finish/close before capture, or
    start only after capture, respectively.  A single long-lived browser here is
    unsafe even though all launches share the same Playwright driver.
    """
    async with _isolated_browser(browser_type, "anti-cheat") as anti_browser:
        context = await anti_browser.new_context(reduced_motion="reduce")
        try:
            page = await context.new_page()
            anti_cheat = await _anti_cheat_check(page, agent_url)
        finally:
            await context.close()

    await _take_agent_screenshots(
        browser_type,
        agent_url,
        page_info_list,
        agent_screenshots_dir,
        agent_layout_dir,
        agent_region_dir,
        dom_dir=agent_dom_dir,
    )

    try:
        from evaluation.quality_score import compute_quality_score_for_site

        async with _isolated_browser(browser_type, "continuous quality") as quality_browser:
            quality_cont = await compute_quality_score_for_site(
                quality_browser,
                agent_url,
                serve_dir,
                gt_dom_dir,
                agent_dom_dir,
                target_pages,
            )
    except Exception as e:
        logger.error("Continuous quality score failed: %s", e)
        quality_cont = {"overall_score": None}

    return anti_cheat, quality_cont


def _apply_vlm_overrides(eval_config: dict, vlm_overrides: dict) -> dict:
    """Return a copy of ``eval_config`` with runtime ``vlm_judge`` overrides applied.

    Only non-None override values are applied (so partial overrides — e.g. just
    ``{"enabled": False}`` — leave the other baked fields intact). ``enabled=False``
    IS applied (it is not None), so a runtime "off" wins over a baked ``enabled=True``.
    Never mutates the input ``eval_config`` or its nested ``vlm_judge`` dict — critical
    because ``eval_config`` may share the ``vlm_judge`` reference with the module-global
    ``config.DEFAULT_EVAL_CONFIG``. A falsy/empty ``vlm_overrides`` returns the input
    unchanged (backward-compatible).
    """
    if not vlm_overrides:
        return eval_config
    merged_vlm = dict(eval_config.get("vlm_judge") or {})
    for k, v in vlm_overrides.items():
        if v is not None:
            merged_vlm[k] = v
    new_cfg = dict(eval_config)
    new_cfg["vlm_judge"] = merged_vlm
    return new_cfg


def _isolation_verdict(workspace_dir: Path, agent_output_dir: Path = None):
    """True / False / None — was this rollout's network actually confined?

    None means "no record", which is NOT "no": artifacts predating isolation have no
    marker, and those are exactly the runs whose egress must still be penalised.

    Several candidate paths are tried because the runner KNOWS the workspace while
    the scorer INFERS it (workspace_dir is derived from agent_output_dir.parent with
    a fallback). If that inference lands one directory off, a strict single-path
    lookup would read "no record" for a run that was in fact isolated, and penalise
    it — the exact false penalty this gate is supposed to have stopped making. The
    authoritative harness-written copy is preferred over the in-workspace legacy
    path, which the agent could have forged.
    """
    from runner.run_agent import _marker_path

    cands = []
    for ws in (workspace_dir, agent_output_dir.parent if agent_output_dir else None):
        if ws is None:
            continue
        cands.append(_marker_path(ws))
    cands += [Path(workspace_dir) / ".net_isolation"]          # pre-move artifacts
    for m in cands:
        try:
            if m.is_file():
                return m.read_text(encoding="utf-8").strip() == "1"
        except OSError:
            continue
    return None


def _egress_gate(workspace_dir: Path, agent_output_dir: Path = None):
    """(egress report, whether to penalise).

    Penalise only what prevention did NOT already stop. With the namespace in force
    a non-loopback host has no route, so the trajectory line is an attempt that
    returned nothing: the run acquired no external content, and capping it at 0.10
    would score intent over outcome. When isolation is off or unrecorded the same
    line means the fetch SUCCEEDED — the case this gate exists for (an agent pulling
    the upstream repo's file tree from the GitHub API after being blocked from reading
    the reference files) — and it still caps.

    Scans the agent's own trajectory only; harness model-transport logs would make
    every run a hit.
    """
    try:
        active = _isolation_verdict(workspace_dir, agent_output_dir)
    except (OSError, ImportError):
        active = None
    try:
        from evaluation.egress_scan import scan_for_egress
        traj = [Path(workspace_dir) / "trajectory.jsonl",
                Path(workspace_dir) / "agent_run.log"]
        egress = scan_for_egress(traj, isolation_active=active)
    except Exception as e:                      # noqa: BLE001 — a detector fault must not
        logger.error("egress scan failed: %s", e)   # break evaluation, and must not penalise
        return {"is_egress": False, "error": str(e), "isolation_active": active}, False

    if not egress.get("is_egress"):
        return egress, False
    if active is True:
        logger.warning("egress attempt(s) had no route (isolation on), not penalised: %s",
                       list(egress.get("hits") or {})[:5])
        egress["blocked_by_isolation"] = True
        return egress, False
    return egress, True


async def evaluate_task(
    task_dir: Path,
    agent_output_dir: Path,
    output_dir: Path = None,
    vlm_overrides: dict = None,
    reference_candidate: bool = False,
) -> dict:
    """Run full evaluation pipeline for one task.

    Args:
        task_dir: Directory containing task.json, evaluation/, site/
        agent_output_dir: Directory containing agent's output (index.html)
        output_dir: Directory to write evaluation results
        vlm_overrides: Optional runtime overrides for the ``vlm_judge`` sub-config
            (e.g. {"enabled": bool, "model": str, "backend": str, "base_url": str,
            "api_key": str, "mode": str}). These win over BOTH the baked
            ``evaluation/eval_config.json`` and ``config.DEFAULT_EVAL_CONFIG`` — the
            authoritative runtime control for the VLM judge. ``None`` values are
            ignored; ``None`` for the whole arg leaves the loaded config untouched
            (backward-compatible). This is how the harness turns the judge on/off
            and points it at a specific model/endpoint without regenerating datasets.
        reference_candidate: the candidate is the frozen reference itself. Scoring
            still runs in full, but agent-integrity gates cannot cap the result.

    Returns:
        dict with scores, breakdown, and metadata
    """
    from evaluation.test_runner import run_tests
    from evaluation.vlm_judge import create_vlm_judge
    from evaluation.score_aggregator import aggregate_scores, format_score_report

    task_dir = Path(task_dir)
    agent_output_dir = Path(agent_output_dir)

    if output_dir is None:
        output_dir = agent_output_dir / "eval_results"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load task configuration
    task_json_path = task_dir / "task.json"
    if not task_json_path.exists():
        return {"error": f"task.json not found: {task_json_path}", "final_score": 0.0}

    task_json = json.loads(task_json_path.read_text())

    # Load eval config
    eval_config_path = task_dir / "evaluation" / "eval_config.json"
    if eval_config_path.exists():
        eval_config = json.loads(eval_config_path.read_text())
    else:
        eval_config = dict(DEFAULT_EVAL_CONFIG)

    # Apply runtime VLM-judge overrides. These are authoritative: they win over the
    # baked eval_config.json AND DEFAULT_EVAL_CONFIG so the judge can be toggled /
    # repointed per-run without rebuilding the dataset.
    if vlm_overrides:
        eval_config = _apply_vlm_overrides(eval_config, vlm_overrides)
        _vlm = eval_config.get("vlm_judge", {})
        logger.info(
            "VLM judge runtime override applied: enabled=%s model=%s backend=%s",
            _vlm.get("enabled"), _vlm.get("model"), _vlm.get("backend"),
        )

    # Check agent output exists
    agent_html = agent_output_dir / "index.html"
    if not agent_html.exists():
        for candidate in agent_output_dir.glob("**/*.html"):
            agent_html = candidate
            break

    if not agent_html.exists():
        return {
            "error": "No HTML output found in agent output directory",
            "final_score": 0.0,
        }

    # Determine the serve directory
    serve_dir = agent_html.parent

    # Resolve target pages
    target_pages = task_json.get("constraints", {}).get("target_pages", [])
    site_meta_path = task_dir / "site_meta.json"
    page_info_list = []
    if site_meta_path.exists():
        site_meta = json.loads(site_meta_path.read_text())
        page_map = {p["id"]: p for p in site_meta.get("pages", [])}
        for pid in target_pages:
            if pid in page_map:
                page_info_list.append(page_map[pid])
            else:
                page_info_list.append({"id": pid, "path": f"/{pid}/" if pid != "homepage" else "/"})
    else:
        for pid in target_pages:
            page_info_list.append({"id": pid, "path": f"/{pid}/" if pid != "homepage" else "/"})

    result = {
        "task_id": task_json.get("task_id", "unknown"),
        "domain": task_json.get("domain", "unknown"),
        "eval_target": "reference" if reference_candidate else "recreation",
    }

    from playwright.async_api import async_playwright

    async with SiteServer(str(serve_dir)) as agent_server:
        async with async_playwright() as pw:
            # Steps 1/2/2.6 use stage-scoped browsers. Screenshot cleanup may
            # force-reap Chromium, so no anti-cheat or quality browser may remain
            # alive across the screenshot stage.
            agent_screenshots_dir = output_dir / "agent_screenshots"
            agent_layout_dir = output_dir / "agent_layout"
            agent_region_dir = output_dir / "agent_regions"
            agent_dom_dir = output_dir / "agent_dom"
            gt_dom_dir = task_dir / "evaluation" / "gt_dom"
            anti_cheat, quality_cont = await _collect_live_browser_metrics(
                pw.chromium,
                agent_server.url,
                serve_dir,
                page_info_list,
                target_pages,
                agent_screenshots_dir,
                agent_layout_dir,
                agent_region_dir,
                agent_dom_dir,
                gt_dom_dir,
            )
            result["anti_cheat"] = anti_cheat
            result["quality_continuous"] = quality_cont

    # Step 2.5: Compute visual score (SSIM-p25 + LPIPS + Layout-IoU + region)
    from evaluation.visual_score import compute_visual_score_for_site
    gt_screenshots_dir = task_dir / "evaluation" / "gt_screenshots"
    gt_layout_dir = task_dir / "evaluation" / "gt_layout"
    gt_region_dir = task_dir / "evaluation" / "gt_regions"

    visual_ssim_result = compute_visual_score_for_site(
        gt_screenshots_dir, agent_screenshots_dir, target_pages,
        gt_layout_dir=gt_layout_dir, agent_layout_dir=agent_layout_dir,
        gt_region_dir=gt_region_dir, agent_region_dir=agent_region_dir,
    )
    result["visual_ssim"] = visual_ssim_result

    # Step 2.7: continuous structural score (DOM-tree similarity vs GT dom_snapshot)
    try:
        from evaluation.structural_score import compute_structural_score_for_site
        structural_cont = compute_structural_score_for_site(
            gt_dom_dir, agent_dom_dir, target_pages)
    except Exception as e:
        logger.error("Continuous structural score failed: %s", e)
        structural_cont = {"overall_score": None}
    result["structural_continuous"] = structural_cont

    # Step 3: Run Playwright tests (skip visual — scored via SSIM above)
    test_dir = task_dir / "evaluation" / "tests"
    test_results = {}
    if test_dir.exists():
        try:
            async with SiteServer(str(serve_dir)) as agent_server:
                test_results = await run_tests(
                    test_dir, agent_server.url, eval_config,
                    skip_dimensions=["visual"],
                )
        except Exception as e:
            logger.error("Test execution failed: %s", e)
    else:
        logger.warning("No test directory found: %s", test_dir)

    # Step 4: VLM judge (if enabled)
    vlm_judge = create_vlm_judge(eval_config)
    gt_screenshots_dir = task_dir / "evaluation" / "gt_screenshots"

    gt_shots = {}
    agent_shots = {}
    for pid in target_pages:
        gt_dir = gt_screenshots_dir / pid
        agent_dir = agent_screenshots_dir / pid
        gt_files = [str(gt_dir / f"{v}.png") for v in ["desktop", "mobile"]
                    if (gt_dir / f"{v}.png").exists()]
        agent_files = [str(agent_dir / f"{v}.png") for v in ["desktop", "mobile"]
                      if (agent_dir / f"{v}.png").exists()]
        if gt_files:
            gt_shots[pid] = gt_files
        if agent_files:
            agent_shots[pid] = agent_files

    vlm_task_context = dict(task_json)
    for fname, key in [
        ("vlm_assertions.json", "vlm_assertions"),
        ("vlm_checklist.json", "vlm_checklist"),
    ]:
        p = task_dir / "evaluation" / fname
        if p.exists():
            try:
                vlm_task_context[key] = json.loads(p.read_text())
            except Exception as e:
                logger.warning("Failed to load %s: %s", fname, e)

    vlm_result = await vlm_judge.judge(gt_shots, agent_shots, vlm_task_context)

    # Step 5: Aggregate scores — blend continuous scorers with their (tightened)
    # Playwright test pass-rates (DOM-similarity primary for structural; GT-relative
    # quality scorer primary for quality). Fall back to whichever side is available.
    STRUCTURAL_DOM_W = 0.7   # weight of continuous DOM-similarity vs binary structural tests
    QUALITY_SCORER_W = 0.6   # weight of continuous quality scorer vs binary quality tests

    def _dim_passrate(dim):
        r = test_results.get(dim)
        if r is None:
            return None
        if hasattr(r, "pass_rate"):
            return r.pass_rate
        if isinstance(r, dict):
            return r.get("pass_rate")
        return None

    def _blend(primary, secondary, w_primary):
        if primary is None and secondary is None:
            return None
        if primary is None:
            return secondary
        if secondary is None:
            return primary
        return w_primary * primary + (1.0 - w_primary) * secondary

    structural_final = _blend(structural_cont.get("overall_score"),
                              _dim_passrate("structural"), STRUCTURAL_DOM_W)
    quality_final = _blend(quality_cont.get("overall_score"),
                           _dim_passrate("quality"), QUALITY_SCORER_W)
    func_r = test_results.get("functional")
    functional_weighted = _weighted_functional_score(
        getattr(func_r, "details", None) if func_r is not None else None)
    result["structural_final"] = structural_final
    result["quality_final"] = quality_final
    result["functional_weighted"] = functional_weighted

    visual_ssim_score = visual_ssim_result.get("overall_score", None)

    # Real per-dimension counts for dimension_details (P2-4): functional reports its
    # binary test pass/total; the continuous dims report COVERAGE (units scored / units
    # attempted), so a "0 tests => dead dimension" sanity check no longer misfires on a
    # dimension that is in fact scored continuously.
    n_pages = len(target_pages) if target_pages else 0
    visual_scored = sum(
        1 for s in (visual_ssim_result.get("per_page", {}) or {}).values()
        if s.get("per_viewport"))
    if func_r is not None and hasattr(func_r, "passed"):
        _f_passed, _f_total = func_r.passed, func_r.total
    elif isinstance(func_r, dict):
        _f_passed, _f_total = func_r.get("passed", 0), func_r.get("total", 0)
    else:
        _f_passed, _f_total = 0, 0

    def _cov(scored):
        scored = scored or 0
        return {"passed": scored, "total": n_pages,
                "failed": max(0, n_pages - scored), "continuous": True}

    dimension_counts = {
        "functional": {"passed": _f_passed or 0, "total": _f_total or 0,
                       "failed": max(0, (_f_total or 0) - (_f_passed or 0)),
                       "continuous": False},
        "visual": _cov(visual_scored),
        "structural": _cov(structural_cont.get("n_scored", 0)),
        "quality": _cov(quality_cont.get("n_scored", 0)),
    }

    scores = aggregate_scores(test_results, vlm_result, eval_config,
                              visual_score=visual_ssim_score,
                              structural_score=structural_final,
                              quality_score=quality_final,
                              functional_score=functional_weighted,
                              dimension_counts=dimension_counts)

    # Fine-grained (additive) functional sub-scores per spec file — diagnostic only,
    # does NOT change final_score or the pooled functional dimension above.
    scores["functional_subscores"] = _functional_subscores(
        getattr(func_r, "details", None) if func_r is not None else None)

    # ---- Integrity gates (broadened anti-cheat, P0-2 / P1-1 / P1-2) -----------------
    # Any confirmed violation caps the final score at INTEGRITY_CAP. Each detector is
    # isolated in try/except so a detector fault never breaks evaluation — it records an
    # error and simply does not penalise.
    workspace_dir = agent_output_dir.parent
    for _cand in (agent_output_dir.parent, agent_output_dir):
        if (_cand / "src").is_dir() or (_cand / "agent_run.log").is_file():
            workspace_dir = _cand
            break
    integrity = {"cap": INTEGRITY_CAP, "reasons": []}

    # (1) screenshot-paste (existing live-DOM detector)
    integrity["anti_cheat"] = anti_cheat
    if anti_cheat.get("is_cheating", False):
        integrity["reasons"].append("screenshot_paste")

    # (2) read-the-answer leak (scans persisted workspace artifacts; excludes the
    # evaluator's own output_dir to avoid a re-score feedback loop)
    try:
        from evaluation.leak_scan import scan_workspace_for_leak
        leak = scan_workspace_for_leak(
            workspace_dir,
            workspace_dir / "agent_run.log",
            output_dir=output_dir,
            protected_input_roots=(task_dir.parent,),
        )
    except Exception as e:
        logger.error("leak scan failed: %s", e)
        leak = {"is_leak": False, "error": str(e)}
    integrity["leak_scan"] = leak
    if leak.get("is_leak"):
        integrity["reasons"].append("read_answer_leak")

    # Did prevention actually engage? The runner drops a marker because eval is a
    # SEPARATE process in the deployment platform path (rollout runs with --skip-eval first), so an
    # in-memory flag or an env var would not survive. Missing marker -> None,
    # i.e. "not attempted / unknown", never a silent "yes".
    egress, _egress_penalise = _egress_gate(workspace_dir, agent_output_dir)
    integrity["egress"] = egress
    if _egress_penalise:
        integrity["reasons"].append("external_egress")

    # (3) verbatim scrape (fingerprint-gated, reads raw bytes)
    try:
        from evaluation.scrape_gate import compute_scrape_gate
        scrape = compute_scrape_gate(agent_html, workspace_dir / "src", serve_dir,
                                     gt_dom_dir=gt_dom_dir, target_pages=target_pages)
    except Exception as e:
        logger.error("scrape gate failed: %s", e)
        scrape = {"verdict": "CLEAN", "error": str(e)}
    integrity["scrape_gate"] = scrape
    if scrape.get("verdict") == "SCRAPE":
        integrity["reasons"].append("verbatim_scrape")

    # (4) originality — structural-overlap monitoring. A faithful recreation is expected to
    # converge on the reference DOM, so similarity alone is not proof of copying. Enforcement
    # remains with scrape_gate / leak_scan; both tag-only and tag+class overlap stay in the
    # result for auditing.
    try:
        from evaluation.originality_score import compute_originality
        originality = compute_originality(gt_dom_dir, agent_dom_dir, target_pages)
    except Exception as e:
        logger.error("originality check failed: %s", e)
        originality = {
            "monitoring_only": True,
            "threshold_exceeded": False,
            "penalize": False,
            "flag": False,
            "error": str(e),
        }
    integrity["originality"] = originality
    if originality.get("threshold_exceeded"):
        logger.info("originality high-overlap signal recorded (monitoring-only)")

    if reference_candidate:
        # A reference necessarily resembles the reference and may contain assets or
        # markup that would be forbidden in an agent submission. Preserve gate output
        # as diagnostics, but do not let submission-integrity policy corrupt this
        # evaluator/fixture check.
        integrity["diagnostic_reasons"] = list(integrity["reasons"])
        integrity["reasons"] = []
        integrity["skipped"] = True
        integrity["skip_reason"] = "frozen_reference_candidate"

    integrity["penalized"] = bool(integrity["reasons"])
    if integrity["penalized"]:
        scores["final_score"] = min(scores["final_score"], INTEGRITY_CAP)
        scores["integrity_penalty"] = True
        scores["integrity_reasons"] = integrity["reasons"]
        # keep the legacy field set for any downstream consumer that reads it
        scores["anti_cheat_penalty"] = True
        logger.warning("Integrity penalty (%s) — final score capped at %.2f",
                       ",".join(integrity["reasons"]), INTEGRITY_CAP)

    result["integrity"] = integrity
    result["scores"] = scores
    result["final_score"] = scores["final_score"]

    # Save results
    results_path = output_dir / "scores.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # Per-test pass/fail detail — a standalone artifact (kept OUT of scores.json /
    # metrics to avoid bloating every job's metrics payload). Best-effort: a write
    # failure must never fail a scored run.
    try:
        (output_dir / "test_details.json").write_text(
            json.dumps(
                {"task_id": result.get("task_id"),
                 "final_score": result.get("final_score"),
                 "test_details": _collect_test_details(test_results)},
                ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to write test_details.json: %s", e)

    report = format_score_report(scores)
    (output_dir / "score_report.txt").write_text(report)

    logger.info(
        "Evaluation complete for %s: final_score=%.4f",
        result["task_id"], result["final_score"],
    )

    return result
