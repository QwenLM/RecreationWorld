"""Read-the-answer leak detection for RecreationBench Web.

The existing ``_anti_cheat_check`` only catches screenshot-paste cheating. It cannot see
the most damaging cheat found in forensics: the agent **reads the leaked ground truth and
scorer** (GT screenshots under the protected dataset root, the scoring Playwright specs,
and the scorer source) and optimises the
replica against the known answer.

The real fix is sandbox isolation (P0-1, separate change). This detector is defence-in-
depth *and* lets us re-score already-completed runs: it scans the agent's persisted
artifacts for evidence that it touched the leaked files.

We scan the agent **workspace** (authored output + source + the run log + any session
transcript), not where the trajectory log happens to live, because the tell-tale evidence
persists in the workspace regardless: copied ``*.spec.ts`` files, a strategy memo, or code
that imports the scorer / opens GT paths. The signature literals are highly specific to the
leaked tree and the scorer internals, so an honest agent cannot produce them — yielding a
near-zero false-positive detector.

R3 additions: (1) the literal set now includes the scorer / anti-cheat SOURCE identifiers
(function & constant names + module filenames), so reading the scorer or the gate code is
caught even without leaked path prefixes; (2) the LLM **trajectory logs** are now
STREAM-scanned with an ambient-safe
literal subset — closing the gap where scorer-source reconnaissance lived only in the
trajectory (agent_run.log is often truncated) and so evaded the workspace-only scan.
"""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

LEAK_CAP = 0.10

_SCORER_ROOT = str(Path(__file__).resolve().parent.parent)

# High-specificity literals: present only if the agent touched the leaked GT tree or the
# scorer source. Deliberately conservative (no generic words / public filenames) to avoid
# false positives. NB: "eval_config.json" was intentionally dropped — it is a generic,
# publicly-documented filename that can appear in honest logs/paths without proving leak
# access. Protected input roots are supplied by the evaluator at runtime.
_LEAK_LITERALS = [
    _SCORER_ROOT,                   # scorer location in this checkout or installation
    "gt_screenshots",              # leaked GT screenshots dir
    "gt_regions",                  # leaked GT region crops
    "scripted_validation_report",  # GT build artifact
]

# Scorer / anti-cheat SOURCE identifiers (function & constant names + module filenames).
# These occur ONLY in /opt/mockweb-bench/evaluation/*.py source — they are never ambient
# claude-code context and never appear in a rebuilt site's own React/TS. So reading (or
# copying) the scorer / anti-cheat source is caught by these regardless of the path used.
# (R3: the old detector missed scorer-source reconnaissance because it only knew the two
# path prefixes above, and those are ambient in the trajectory log.)
_SCORER_SOURCE_LITERALS = [
    "compute_pixel_similarity", "coverage_asymmetric",   # visual_score internals
    "compute_scrape_gate", "FINGERPRINT_PATTERNS",       # scrape_gate (anti-cheat) internals
    "scan_workspace_for_leak",                           # leak_scan (anti-cheat) internal
    "_anti_cheat_check",                                 # evaluator internal
    "scrape_gate.py", "leak_scan.py", "visual_score.py", "score_aggregator.py",
    "gt_generator.py", "structural_score.py", "quality_score.py", "originality_score.py",
]

# Workspace scan = leaked paths + scorer source (a workspace file mentioning any of these is
# a violation: honest React source never contains scorer internals or the leaked paths).
def _literal_pattern(values):
    literals = [str(value) for value in values if str(value)]
    return re.compile("|".join(re.escape(value) for value in literals) or r"(?!)")


def _literal_bytes_pattern(values):
    literals = [str(value).encode("utf-8") for value in values if str(value)]
    return re.compile(b"|".join(re.escape(value) for value in literals) or rb"(?!)")


_LEAK_RE = _literal_pattern(_LEAK_LITERALS + _SCORER_SOURCE_LITERALS)

# Trajectory scan = AMBIENT-SAFE subset only. Logs echo the agent's own environment,
# where working and configuration paths appear as ambient noise in every request, so
# bare path prefixes must not be cap signals there. What remains is
# unambiguous: reading the withheld GT (gt_screenshots/…) or the scorer/anti-cheat SOURCE.
_TRAJECTORY_CAP_LITERALS = [
    "gt_screenshots", "gt_regions", "scripted_validation_report",
] + _SCORER_SOURCE_LITERALS
_TRAJECTORY_CAP_RE = re.compile("|".join(re.escape(s) for s in _TRAJECTORY_CAP_LITERALS))

# Monitoring-only (recorded, does NOT trip is_leak): filesystem reconnaissance of the dataset
# tree. Reaching the dataset's site subtree can be benign asset-hunting, so it is a
# soft signal surfaced for review rather than an automatic cap.
_RECON_RE = _literal_pattern(())

# Byte-compiled variants of the trajectory patterns. Trajectory logs are multi-GB, so we
# scan them at the RAW BYTE level (no utf-8 decode per chunk): all literals are ASCII, so
# byte matching is exact and ~2-3x faster, which is what makes a FULL-FILE scan affordable.
_TRAJECTORY_CAP_RE_B = re.compile(
    b"|".join(re.escape(s.encode("ascii")) for s in _TRAJECTORY_CAP_LITERALS))
_RECON_RE_B = _literal_bytes_pattern(())

_TEXT_EXTS = {".ts", ".tsx", ".js", ".jsx", ".html", ".json", ".css", ".md",
              ".txt", ".log", ".jsonl", ".py", ".sh", ".spec"}
# Log files are NOT walked as workspace source: they echo claude-code's own environment
# (ambient working-directory and settings paths)
# which would false-positive against the path literals. They are stream-scanned separately
# with the ambient-safe _TRAJECTORY_CAP_RE instead (see _stream_scan_log).
_LOG_EXTS = {".log", ".jsonl"}
# Skip dependency/build dirs AND the evaluator's own outputs (eval_results) + the agent's
# CC session dir (.claude). Scanning eval_results would create a re-score feedback loop:
# the evaluator writes scores.json (which embeds this scan's matched literals) back into
# the workspace, so a re-eval would re-detect them. .claude/eval_results also hold large
# captured artifacts that needlessly inflate scan cost.
_SKIP_DIRS = {"node_modules", ".git", "dist", "eval_results", ".claude"}
_MAX_FILE_BYTES = 24_000_000
_MAX_TOTAL_BYTES = 400_000_000  # global budget backstop against pathological trees

# --- trajectory (LLM tool-call) log scanning (R3) -------------------------------------
# The real per-turn tool-call history may live in a model-transport trajectory log outside
# the workspace and outside the often-truncated
# agent_run.log. Stream-scan it so scorer-source reads / GT reads that leave no workspace
# trace are still caught. These logs are 2-12 GB.
#
# FULL-COVERAGE scan (2026-07-03): the log is scanned in its ENTIRETY, chunk by chunk, with
# constant memory — NO head/tail truncation. The earlier 2.5 GB budget scanned only the head
# and tail halves and skipped the middle, so on >2.5 GB trajectories (18/31 of the glm-5.2
# 070315 run) a mid-session scorer/GT read would have been missed. ``MAX_TRAJECTORY_BYTES`` is
# now only a runaway backstop set far above any real log; ``truncated`` is True only if a log
# exceeds even that ceiling (never for real trajectories).
MAX_TRAJECTORY_BYTES = 64_000_000_000   # 64 GB pathological-runaway ceiling (not a real limit)
_TRAJ_CHUNK = 16_000_000
_TRAJ_OVERLAP = 512   # > any literal, so a match spanning a chunk boundary is still found


def _iter_text_files(root: Path, skip_subtree: Path = None):
    if not root or not root.is_dir():
        return
    skip_subtree = skip_subtree.resolve() if skip_subtree else None
    for dpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if skip_subtree is not None:
            # prune the evaluator's own output dir even if it's named non-defaultly
            try:
                if Path(dpath).resolve() == skip_subtree:
                    dirs[:] = []
                    continue
            except OSError:
                pass
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in _LOG_EXTS:
                continue  # logs are stream-scanned separately (ambient-safe literals)
            if ext in _TEXT_EXTS or name.endswith(".spec.ts"):
                yield Path(dpath) / name


def _stream_scan_log(path: Path, cap_re, recon_re, budget: int = MAX_TRAJECTORY_BYTES) -> dict:
    """Stream-scan a trajectory log for cap/recon literals IN FULL, without loading it whole.

    The ENTIRE file is read chunk by chunk (constant memory) — no head/tail truncation — so a
    scorer/GT read anywhere in a multi-GB trajectory is caught. ``cap_re``/``recon_re`` are
    BYTE patterns matched against raw bytes (all literals are ASCII), avoiding a per-chunk utf-8
    decode so a full scan stays affordable. A ``_TRAJ_OVERLAP``-byte carry makes matches that
    straddle a chunk boundary still hit (presence is exact; counts may double-count a match on a
    boundary). ``budget`` is only a runaway backstop (64 GB); ``truncated`` is True solely if a
    log exceeds it — never for a real trajectory."""
    cap_hits: dict = {}
    recon_total = 0
    scanned = 0
    truncated = False
    try:
        size = path.stat().st_size
    except OSError:
        return {"cap_hits": {}, "recon_total": 0, "bytes_scanned": 0, "truncated": False}
    end = size
    if size > budget:
        end = budget          # pathological-runaway guard only (never hit by real logs)
        truncated = True
    try:
        with open(path, "rb") as fh:
            carry = b""
            pos = 0
            while pos < end:
                buf = fh.read(min(_TRAJ_CHUNK, end - pos))
                if not buf:
                    break
                pos += len(buf)
                window = carry + buf
                for m in cap_re.findall(window):
                    cap_hits[m] = cap_hits.get(m, 0) + 1
                recon_total += len(recon_re.findall(window))
                scanned += len(buf)
                carry = window[-_TRAJ_OVERLAP:]
    except OSError as e:
        logger.warning("trajectory log scan failed for %s: %s", path, e)
    # decode byte-pattern match keys back to str for the returned hit map
    cap_hits_str = {(k.decode("ascii", "ignore") if isinstance(k, bytes) else k): v
                    for k, v in cap_hits.items()}
    return {"cap_hits": cap_hits_str, "recon_total": recon_total,
            "bytes_scanned": scanned, "truncated": truncated}


def _discover_trajectory_logs(workspace_dir, agent_log_path) -> list:
    """Locate trajectory logs to stream-scan: the explicit agent run log + workspace
    agent_run.log + provider-neutral trajectory candidates in sibling ``logs/`` dirs."""
    logs, seen = [], set()

    def _add(p):
        try:
            p = Path(p)
            rp = p.resolve()
            if p.is_file() and rp not in seen:
                seen.add(rp)
                logs.append(p)
        except OSError:
            pass

    if agent_log_path:
        _add(agent_log_path)
    if workspace_dir:
        ws = Path(workspace_dir)
        _add(ws / "agent_run.log")
        # Only genuine agent trajectory logs — the model request/response stream and any
        # claude-code session transcript. Deliberately EXCLUDE the harness orchestrator log
        # (main.log): it emits benign protected-dataset lines of its own that
        # are the harness's doing, not the agent's, and must not be attributed to the agent.
        #
        # ``<ws>/sessions`` is where run_agent._collect_sessions lands the CLIs' OWN
        # transcripts (trajectory.collect_local, ``{stage}_{user}_{main|sub}_{basename}``
        # keeping the .jsonl suffix). Without it codex was un-auditable: model tool calls
        # may appear only in a transport log while the Codex rollout lives under
        # $CODEX_HOME/sessions, so the two logs/ dirs held
        # no codex evidence at all and every codex run scanned clean by construction.
        for logs_dir in (ws.parent / "logs", ws / "logs", ws / "sessions"):
            try:
                if logs_dir.is_dir():
                    for pattern in (
                        "agent_run.log",
                        "trajectory.jsonl",
                        "*.jsonl",
                        "*trajectory*.log",
                        "*model*.log",
                        "*proxy*.log",
                    ):
                        for log_file in sorted(logs_dir.glob(pattern)):
                            _add(log_file)
            except OSError:
                pass
    return logs


def scan_workspace_for_leak(workspace_dir, agent_log_path=None, output_dir=None,
                            scan_trajectory=True, trajectory_logs=None,
                            trajectory_budget=MAX_TRAJECTORY_BYTES,
                            protected_input_roots=None) -> dict:
    """Scan the agent workspace AND trajectory logs for evidence it read the leaked GT/scorer.

    Args:
        workspace_dir: the agent workspace (contains src/, output/, agent_run.log).
        agent_log_path: optional explicit path to the run log (defaults to
            ``workspace_dir/agent_run.log``).
        output_dir: the evaluator's own output dir to EXCLUDE from the scan (avoids the
            re-score feedback loop where scores.json embeds prior matched literals).
        scan_trajectory: also stream-scan discovered model/agent trajectory logs
            with the ambient-safe literal set (R3). Default True.
        trajectory_logs: explicit list of log paths to stream-scan (overrides discovery).
        trajectory_budget: per-log byte budget for streaming.
        protected_input_roots: dataset roots whose appearance in authored files is a leak
            and whose appearance in trajectories is a monitoring-only reconnaissance signal.

    Returns a dict: {is_leak, hits (literal->count), files (sample), spec_files_in_workspace,
    trajectory_hits, trajectory_recon_total (monitoring only), ...}.
    """
    workspace_dir = Path(workspace_dir) if workspace_dir else None
    protected_roots = [str(Path(path).resolve()) for path in (protected_input_roots or ())]
    workspace_leak_re = _literal_pattern(
        _LEAK_LITERALS + _SCORER_SOURCE_LITERALS + protected_roots
    )
    recon_re_b = _literal_bytes_pattern(protected_roots)
    hits = {}
    files_hit = []
    spec_files = 0
    bytes_scanned = 0
    budget_hit = False

    scanned = set()

    def _scan_file(p: Path):
        nonlocal spec_files, bytes_scanned, budget_hit
        if p in scanned or budget_hit:
            return
        scanned.add(p)
        try:
            if p.name.endswith(".spec.ts"):
                spec_files += 1
            if p.stat().st_size > _MAX_FILE_BYTES:
                return  # skip oversized blobs (leak evidence lives in small text files)
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                t = fh.read(_MAX_FILE_BYTES)
        except Exception:
            return
        bytes_scanned += len(t)
        if bytes_scanned > _MAX_TOTAL_BYTES:
            budget_hit = True
        found = workspace_leak_re.findall(t)
        if found:
            for m in found:
                hits[m] = hits.get(m, 0) + 1
            if len(files_hit) < 25:
                try:
                    rel = str(p.relative_to(workspace_dir)) if workspace_dir else str(p)
                except Exception:
                    rel = str(p)
                files_hit.append(rel)

    out_subtree = Path(output_dir) if output_dir else None
    if workspace_dir:
        for p in _iter_text_files(workspace_dir, skip_subtree=out_subtree):
            _scan_file(p)

    # --- R3: stream-scan model and agent trajectory logs with the
    # AMBIENT-SAFE literal set. This catches reads of the withheld GT or the scorer/anti-cheat
    # SOURCE that left no workspace trace — the exact gap that let scorer-recon go undetected
    # (agent_run.log is often truncated and a full model log may live outside the workspace).
    trajectory_hits: dict = {}
    recon_total = 0
    traj_scanned = []
    traj_truncated = False
    if scan_trajectory:
        logs = (trajectory_logs if trajectory_logs is not None
                else _discover_trajectory_logs(workspace_dir, agent_log_path))
        for lp in logs:
            r = _stream_scan_log(Path(lp), _TRAJECTORY_CAP_RE_B, recon_re_b,
                                 budget=trajectory_budget)
            for m, c in r["cap_hits"].items():
                trajectory_hits[m] = trajectory_hits.get(m, 0) + c
            recon_total += r["recon_total"]
            traj_truncated = traj_truncated or r["truncated"]
            if r["bytes_scanned"]:
                traj_scanned.append(Path(lp).name)

    # merge trajectory CAP hits into the primary hit map so is_leak reflects them; recon
    # Protected-input filesystem hunting is monitoring-only and not merged.
    for m, c in trajectory_hits.items():
        hits[m] = hits.get(m, 0) + c

    return {
        "is_leak": bool(hits),
        "hits": hits,
        "total_hits": sum(hits.values()),
        "files": files_hit,
        "spec_files_in_workspace": spec_files,
        "budget_hit": budget_hit,
        "trajectory_hits": trajectory_hits,
        "trajectory_recon_total": recon_total,   # monitoring only (does NOT set is_leak)
        "trajectory_logs_scanned": traj_scanned,
        "trajectory_truncated": traj_truncated,
        "cap": LEAK_CAP,
    }
