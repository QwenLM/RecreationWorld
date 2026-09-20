#!/usr/bin/env python3
"""Unified trajectory + transcript capture for all 5 RecreationBench platforms.

ONE stdlib-only collector, exercised in every execution context so the SAME logic runs
everywhere (no per-platform reimplementation):
  * web  — imported in-pod: ``from core import trajectory; trajectory.collect_local(...)``
  * android — via CLI where rb-core is fetched: ``python3 -m core.trajectory collect ...``
  * linux/mac/windows — the module is transferred as a normal runtime file and invoked by CLI.

Two artifacts per stage, kept side by side:
  * stream-json trajectory — ``trajectory.jsonl`` (already produced everywhere via
    ``--output-format stream-json``); copied here as ``{stage}_stream.jsonl``.
  * transcript — Claude Code native session files ``~/.claude/projects/**/*.jsonl``
    (the richer record), classified main (UUID-named) vs sub (``agent-*``), per user.

Canonical local layout:   <output>/sessions/{stage}_{user}_{main|sub}_{basename}
                          <output>/trajectory.jsonl        (stream-json, unchanged)
Canonical artifact store layout:     {prefix}/{task_id}/{stage}/sessions/
                          {prefix}/{task_id}/{stage}/trajectory.jsonl

Design constraint: stdlib-only (os/re/shutil/pathlib) so the transferred module runs on a
bare VM without installing RecreationBench as a package.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# Claude Code derived files that are NOT the session transcript (never collect these).
# Substrings, not globs. "trajectory.jsonl" has no leading dot on purpose: Claude Code
# writes the stream-json dump as a bare trajectory.jsonl, and requiring the dot let it
# through as if it were a session transcript — web's collector picked it up as
# recreation_agent_main_trajectory.jsonl. The dotted form is still matched.
EXCLUDE_SUBSTRINGS = (".checkpoint.", "trajectory.jsonl")


# Claude Code keeps its transcript under ~/.claude/projects; codex keeps the equivalent
# ("rollout") under $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl, CODEX_HOME
# defaulting to ~/.codex. Both are session transcripts of the same run, so they belong in the
# same sessions/ directory under the same naming convention.
CLAUDE_SESSIONS = ".claude/projects"
CODEX_SESSIONS = "sessions"  # under CODEX_HOME

# A rollout is unbounded: 85 MB observed locally, and codex's own docs warn about hundreds of MB
# to GB. The deployment platform flags any artifact file over 100 MB, so a single rollout can
# dominate (or break) the
# upload. Files above the cap are SKIPPED and named in the log -- never truncated, because a
# half-file of JSONL is not a shorter transcript, it is a corrupt one.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


def agent_session_dirs(home, user_label="main", codex_home=None):
    """Every session-transcript dir for one user: Claude Code's and codex's.

    Returned as ``[(dir, label), ...]`` for ``collect_local``. Owned here so the five platforms
    cannot each hand-list a different subset -- which is exactly how codex' rollout went
    uncollected on all of them. Non-existent dirs are harmless: ``_list_dir`` returns [].
    """
    home = str(home).rstrip("/")
    ch = str(codex_home or os.environ.get("CODEX_HOME") or (home + "/.codex")).rstrip(
        "/"
    )
    return [
        ("%s/%s" % (home, CLAUDE_SESSIONS), user_label),
        ("%s/%s" % (ch, CODEX_SESSIONS), user_label),
    ]


def classify(basename: str) -> str:
    """Subagent transcripts are named ``agent-*.jsonl``; the main session is UUID-named."""
    return "sub" if basename.startswith("agent-") else "main"


def select_sessions(listing, since_ts=0.0, until_ts=None):
    """Pure filter (no fs access). ``listing`` = iterable of ``(path, mtime)``. Keep
    ``*.jsonl`` session transcripts modified at/after ``since_ts`` (and, when ``until_ts`` is
    given, strictly before it), dropping Claude Code derived files (checkpoints, the
    stream-json trajectory).

    ``until_ts`` exists so a caller can BUCKET one projects dir into consecutive stages
    against a single marker -- macOS preserves stage1 vs stage2 transcripts that way, and
    without it that bucketing could not be expressed here and had to be hand-rolled."""
    out = []
    for path, mtime in listing:
        name = os.path.basename(str(path))
        if not name.endswith(".jsonl"):
            continue
        if any(x in name for x in EXCLUDE_SUBSTRINGS):
            continue
        if float(mtime) + 1e-6 < float(since_ts):
            continue
        if until_ts is not None and float(mtime) >= float(until_ts):
            continue
        out.append(path)
    return out


def _list_dir(projects_dir):
    """Return ``[(path, mtime), ...]`` for every ``*.jsonl`` under ``projects_dir`` (recursive)."""
    base = Path(projects_dir)
    if not base.is_dir():
        return []
    res = []
    for p in base.rglob("*.jsonl"):
        try:
            res.append((str(p), p.stat().st_mtime))
        except OSError:
            pass
    return res


def session_name(stage: str, user: str, basename: str) -> str:
    """The canonical collected-transcript filename. One definition, because windows cannot
    use ``collect_local``: robocopy is the only copier that handles paths over 260 chars, so
    windows copies first and renames after via ``rename_collected`` -- which must produce
    byte-identical names or the platforms diverge in their output while sharing the code.
    """
    return "%s_%s_%s_%s" % (stage, user, classify(basename), basename)


def rename_collected(dest, stage="recreation", user="main", stream_src=None):
    """Normalise an ALREADY-copied tree in ``dest`` to the canonical flat naming.

    For the one platform that cannot copy through this module (windows: >260-char paths need
    robocopy), so the naming and the exclusion rules still come from here. Idempotent: a file
    already named ``{stage}_`` is left alone, so re-running never double-prefixes. Flattens
    robocopy's ``/E`` subtree and prunes the emptied directories. Best-effort throughout.
    """
    dest = Path(dest)
    if not dest.is_dir():
        return []
    moved = []
    for src in sorted(dest.rglob("*.jsonl")):
        bn = src.name
        if bn.startswith("%s_" % stage):
            continue
        if any(x in bn for x in EXCLUDE_SUBSTRINGS):
            try:
                src.unlink()
            except OSError:
                pass
            continue
        out = dest / session_name(stage, user, bn)
        try:
            if src.resolve() != out.resolve():
                shutil.move(str(src), str(out))
            moved.append(out)
        except OSError:
            pass
    # prune what /E left behind, deepest first so parents empty out too
    for d in sorted((x for x in dest.rglob("*") if x.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    if stream_src and Path(stream_src).is_file():
        out = dest / ("%s_stream.jsonl" % stage)
        try:
            shutil.copy2(stream_src, out)
            moved.append(out)
        except OSError:
            pass
    return moved


def collect_local(
    projects_dirs,
    dest,
    since_ts=0.0,
    stage="recreation",
    stream_src=None,
    until_ts=None,
    max_bytes=DEFAULT_MAX_BYTES,
):
    """Copy session transcripts into ``dest`` as ``{stage}_{user}_{main|sub}_{basename}``,
    and (if given) the stream-json ``stream_src`` as ``{stage}_stream.jsonl``.

    ``projects_dirs``: list of ``dir`` or ``(dir, user_label)`` (default label ``main``) so
    the multi-user case (root + a de-privileged agent user) is one call. Returns the list of
    written dest paths. Best-effort: unreadable files/dirs are skipped, never raised."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for entry in projects_dirs:
        if isinstance(entry, (tuple, list)):
            pdir, user = entry[0], (entry[1] if len(entry) > 1 else "main")
        else:
            pdir, user = entry, "main"
        for src in select_sessions(_list_dir(pdir), since_ts, until_ts):
            bn = os.path.basename(str(src))
            if max_bytes:
                try:
                    sz = os.path.getsize(src)
                except OSError:
                    sz = 0
                if sz > max_bytes:
                    # Named, not silently dropped: a missing transcript that nobody announced
                    # is indistinguishable from a run that produced none.
                    print(
                        "[trajectory] SKIPPED %s (%d bytes > max_bytes %d)"
                        % (bn, sz, max_bytes)
                    )
                    continue
            out = dest / session_name(stage, user, bn)
            try:
                shutil.copy2(src, out)
                written.append(out)
            except OSError:
                pass
    if stream_src and Path(stream_src).is_file():
        out = dest / ("%s_stream.jsonl" % stage)
        try:
            shutil.copy2(stream_src, out)
            written.append(out)
        except OSError:
            pass
    return written


# The canonical names for everything a run saves, in one place. Keep consumers aligned with
# this contract and with what each agent CLI contributes.
#
#   sessions/     BOTH CLIs' session transcripts (Claude Code's ~/.claude/projects and codex'
#                 $CODEX_HOME/sessions rollout), flattened to {stage}_{user}_{main|sub}_{basename},
#                 plus the stream-json copy as {stage}_stream.jsonl.
#   trajectory.jsonl  the stream-json the harness tees while the agent runs. Same name for both
#                 CLIs -- codex' `--json` stream and Claude Code's `--output-format stream-json`
#                 are both "the machine-readable running log", so they do not get separate names.
#   proxy_logs/   the per-request request/response PAIR logs rb copies out of the sidecar's shared
#                 volume. Distinct from the sidecar's own *.log, which is written by the deployment platform
#                 PLATFORM and whose name is not ours -- model-gateway diagnostics globs for that
#                 one instead of pinning a name.
CANONICAL_SESSIONS_DIR = "sessions"
CANONICAL_STREAM_NAME = "trajectory.jsonl"
CANONICAL_PROXY_LOGS_DIR = "proxy_logs"


def surface_stage(stage_dir, output_dir, stage="recreation"):
    """Copy one stage's canonical trajectory products to an artifact root.

    Platforms keep their full stage trees in different places for compatibility, but deployment platform users
    should not need platform-specific paths to find the two trajectory products.  This helper is
    deliberately additive: the stage-owned copies remain in place, while ``output_dir`` gains
    ``trajectory.jsonl`` and ``sessions/``.

    ``sessions/{stage}_stream.jsonl`` is accepted as a fallback source for the top-level stream.
    macOS historically archived only that copy inside its results bundle.
    """
    stage_dir = Path(stage_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []

    sessions_src = stage_dir / CANONICAL_SESSIONS_DIR
    stream_src = stage_dir / CANONICAL_STREAM_NAME
    if not stream_src.is_file():
        candidate = sessions_src / ("%s_stream.jsonl" % stage)
        if candidate.is_file():
            stream_src = candidate

    if stream_src.is_file():
        stream_dst = output_dir / CANONICAL_STREAM_NAME
        try:
            if stream_src.resolve() != stream_dst.resolve():
                shutil.copy2(stream_src, stream_dst)
            written.append(stream_dst)
        except OSError:
            pass

    if sessions_src.is_dir():
        sessions_dst = output_dir / CANONICAL_SESSIONS_DIR
        try:
            if sessions_src.resolve() == sessions_dst.resolve():
                return written
        except OSError:
            pass
        for src in sorted(p for p in sessions_src.rglob("*") if p.is_file()):
            dst = sessions_dst / src.relative_to(sessions_src)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                written.append(dst)
            except OSError:
                pass
    return written


def artifact_layout(prefix, task_id, stage="recreation"):
    """Canonical store keys for a stage's artifacts."""
    base = f"{prefix.rstrip('/')}/{task_id}/{stage}"
    return {
        "sessions": base + "/" + CANONICAL_SESSIONS_DIR + "/",
        "trajectory": base + "/" + CANONICAL_STREAM_NAME,
        "proxy_logs": base + "/" + CANONICAL_PROXY_LOGS_DIR + "/",
    }


def _parse_projects_arg(spec):
    """``dir[:label],dir[:label]`` -> ``[(dir,label), ...]`` (label defaults to ``main``)."""
    dirs = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            d, label = item.rsplit(":", 1)
        else:
            d, label = item, "main"
        dirs.append((d, label))
    return dirs


def last_result_record(path):
    """The LAST `type=result` record in a stream-json trajectory, or None.

    Tolerant by construction: a trajectory can be truncated mid-line (the process was killed)
    or carry non-JSON noise, and a diagnostic that raises on a damaged file is useless exactly
    when it is needed. Unreadable file -> None, same as "no result record".
    """
    res = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue  # non-JSON noise / a truncated final line
                if isinstance(d, dict) and d.get("type") == "result":
                    res = d
    except OSError:
        return None
    return res


# codex-exec streams a DIFFERENT schema: no type=result record, the turn ends with
# turn.completed (or turn.failed). Without this, every healthy codex run was reported as
# "no final result (hard crash / killed mid-stream)" -- which is what the Aegis android run
# was misread as on 2026-08-21, when the trajectory in fact ended with turn.completed and a
# final agent_message saying the APK had been built and installed.
_CODEX_TERMINAL = ("turn.completed", "turn.failed")


def last_protocol_record(path):
    """Return the last Claude/Codex terminal-like record in file order."""
    last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(record, dict) and record.get("type") in (
                    "result",
                    "turn.completed",
                    "turn.failed",
                    "error",
                ):
                    last = record
    except OSError:
        return None
    return last


def _last_codex_terminal(path):
    """Return codex's last terminal record plus preceding error-event count."""
    last = None
    errors = 0
    # Same tolerance as last_result_record: a truncated or noisy file must still yield a
    # verdict, since that is exactly when this is called.
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                kind = rec.get("type")
                if kind in _CODEX_TERMINAL:
                    last = rec
                elif kind == "error":
                    errors += 1
    except OSError:
        return None, errors
    return last, errors


def agent_terminal_status(path):
    """Classify the agent protocol's authoritative terminal record.

    The status is deliberately independent of the process return code: both
    supported CLIs have emitted zero after a protocol-level failure.  Callers
    combine this value with artifact validation and native timeout/termination
    evidence.
    """
    result = last_protocol_record(path)
    if result is not None and result.get("type") == "result":
        subtype = str(result.get("subtype") or "").strip().lower()
        terminal_reason = str(result.get("terminal_reason") or "").strip().lower()
        if subtype in (
            "error_max_turns",
            "error_max_budget_usd",
        ) or terminal_reason in (
            "max_turns",
            "max_budget_usd",
        ):
            return "completed"
        if result.get("is_error") is True or subtype.startswith("error"):
            return "error"
        return "completed" if subtype == "success" else "unknown"

    terminal = result
    if terminal is None:
        return "unknown"
    return "completed" if terminal.get("type") == "turn.completed" else "error"


def terminal_stop_reason(path):
    """``stop_reason`` from the agent's terminal ``result`` record, lowercased, or None.

    Read as a protocol field rather than by matching the message text: a refusal arrives as
    ``stop_reason="refusal"`` with ``api_error_status`` null, and the prose that accompanies it
    ("...appears to violate our Usage Policy...") is free to be reworded at any time.
    """

    result = last_protocol_record(path)
    if result is None or result.get("type") != "result":
        return None
    value = result.get("stop_reason")
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def terminal_final_text(path):
    """The final text carried by the agent's terminal ``result`` record, or None if unavailable.

    An agent that gives up says so.  A terminal record that reports success, flags no error, and
    carries an empty string is something else: an upstream completion that arrived with no content
    and no tool call.  The release prompt makes a text-only response end the session, so the CLI
    exits zero with ``is_error`` false and ``api_error_status`` null, and every error check in the
    pipeline passes -- three agents ended that way within 156ms of each other after 25 to 440
    turns and up to $63 of work, and were scored as though they had chosen to deliver nothing.

    None means "no usable signal": no terminal record, a non-``result`` terminal shape (codex's
    ``turn.completed``), or a ``result`` field that is not a string.  Only an explicit string lets
    a caller act, so a missing field can never be read as emptiness.
    """

    result = last_protocol_record(path)
    if result is None or result.get("type") != "result":
        return None
    value = result.get("result")
    return value if isinstance(value, str) else None


def terminal_error_message(path):
    """Return the authoritative terminal protocol error text, if any."""
    if agent_terminal_status(path) != "error":
        return None
    result = last_protocol_record(path)
    if result is not None and result.get("type") == "result":
        subtype = str(result.get("subtype") or "").strip().lower()
        if (
            result.get("is_error") is True
            or result.get("api_error_status")
            or subtype.startswith("error")
        ):
            return str(
                result.get("result")
                or result.get("api_error_status")
                or result.get("error")
                or subtype
            )
        return None
    terminal = result
    if terminal is None or terminal.get("type") not in ("turn.failed", "error"):
        return None
    error = terminal.get("error") or terminal.get("message") or {}
    return str(error.get("message") or error) if isinstance(error, dict) else str(error)


def _codex_turn_outcome(path):
    """A formatter for codex's terminal record, or None when this is not a codex stream."""
    last = last_protocol_record(path)
    _terminal, errors = _last_codex_terminal(path)
    if last is None or last.get("type") == "result":
        return None

    def fmt(indent):
        if last.get("type") in ("turn.failed", "error"):
            err = last.get("error") or last.get("message") or {}
            msg = (
                str(err.get("message", ""))[:200]
                if isinstance(err, dict)
                else str(err)[:200]
            )
            return [
                indent + "stop reason: codex turn.failed -- %s" % (msg or "no message"),
                indent
                + "NOTE: codex schema (no type=result record); %d error event(s)"
                % errors,
            ]
        usage = last.get("usage") or {}
        out = [
            indent
            + "stop reason: codex turn.completed input_tokens={} output_tokens={} "
            "reasoning_output_tokens={}".format(
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("reasoning_output_tokens"),
            )
        ]
        if errors:
            out.append(
                indent + "NOTE: %d error event(s) before the turn completed" % errors
            )
        return out

    return fmt


def explain_stop(path, indent="    "):
    """Why the agent's run ended, as lines ready to print.

    A stream-json run that ends with subtype=success and is_error=false means the AGENT ended
    its own turn -- Claude Code treats "no tool call" as task-complete and exits 0. That is
    indistinguishable from a healthy finish by exit code alone, so an empty output dir after
    rc=0 gets read as a harness bug when it is actually the model stopping early. Absence of a
    result record is the opposite signal: killed mid-stream or crashed.
    """
    res = last_result_record(path)
    if res is None:
        codex = _codex_turn_outcome(path)
        if codex is not None:
            return codex(indent)
        return [
            indent
            + "stop reason: NO type=result record -- the agent produced no final "
            "result (hard crash / killed mid-stream / truncated output)"
        ]
    out = [
        indent + "stop reason: subtype={} stop_reason={} is_error={} num_turns={} "
        "duration_ms={} terminal_reason={}".format(
            res.get("subtype"),
            res.get("stop_reason"),
            res.get("is_error"),
            res.get("num_turns"),
            res.get("duration_ms"),
            res.get("terminal_reason"),
        )
    ]
    if res.get("subtype") == "success" and not res.get("is_error"):
        out.append(
            indent
            + "NOTE: the agent ENDED ITS OWN TURN (no tool call). CC treats that as "
            "task-complete and exits -- this is NOT a timeout or crash. If no artifact was "
            "produced, the model stopped early on its own."
        )
    return out


def build_parser():
    p = argparse.ArgumentParser(
        description="Collect Claude Code session transcripts + stream-json."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser(
        "collect", help="collect transcripts (+ stream copy) into --dest"
    )
    c.add_argument(
        "--projects",
        default="",
        help="dir[:label],dir[:label] of transcript dirs. Either this or --agent-home.",
    )
    c.add_argument("--dest", required=True, help="output sessions dir")
    c.add_argument(
        "--since", type=float, default=0.0, help="only files with mtime >= this epoch"
    )
    c.add_argument(
        "--since-file",
        default=None,
        help="take the epoch from this file's mtime (a stage start marker). Owned here "
        "rather than in each caller because `stat` flags differ between GNU and BSD, "
        "and macOS's collector runs on the VM's BSD userland.",
    )
    c.add_argument("--stage", default="recreation")
    c.add_argument(
        "--agent-home",
        default=None,
        help="collect BOTH agent CLIs' transcripts for this HOME: ~/.claude/projects and "
        "$CODEX_HOME/sessions. Repeatable as dir[:label] pairs via --projects; this is the "
        "shorthand so no caller hand-lists a subset and silently drops codex' rollout.",
    )
    c.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="skip (and name in the log) any transcript larger than this; 0 disables. codex "
        "rollouts are unbounded and deployment platform flags artifacts over 100 MB.",
    )
    c.add_argument(
        "--until", type=float, default=None, help="only files with mtime < this epoch"
    )
    c.add_argument(
        "--until-file",
        default=None,
        help="take the upper bound from this file's mtime, so one marker splits a projects "
        "dir into two consecutive stages (macOS' stage1-vs-stage2 preservation).",
    )
    c.add_argument(
        "--stream", default=None, help="stream-json trajectory.jsonl to copy alongside"
    )
    r = sub.add_parser(
        "rename",
        help="normalise an already-copied sessions dir to the canonical naming "
        "(for windows, where robocopy does the copying)",
    )
    r.add_argument("--dest", required=True, help="sessions dir to normalise in place")
    r.add_argument("--stage", default="recreation")
    r.add_argument("--user", default="main")
    r.add_argument(
        "--stream", default=None, help="stream-json trajectory.jsonl to copy alongside"
    )
    e = sub.add_parser(
        "explain",
        help="print why the agent's run ended, from a stream-json trajectory",
    )
    e.add_argument("--trajectory", required=True)
    e.add_argument("--indent", default="    ")
    t = sub.add_parser(
        "status",
        help="print completed/error/unknown from the agent protocol terminal record",
    )
    t.add_argument("--trajectory", required=True)
    s = sub.add_parser(
        "surface",
        help="copy a stage's trajectory.jsonl + sessions/ to the final artifact root",
    )
    s.add_argument("--stage-dir", required=True)
    s.add_argument("--output-dir", required=True)
    s.add_argument("--stage", default="recreation")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "collect":
        since = args.since
        if args.since_file:
            try:
                since = os.stat(args.since_file).st_mtime
            except OSError:
                since = args.since  # marker absent -> collect everything, not nothing
        until = args.until
        if args.until_file:
            try:
                until = os.stat(args.until_file).st_mtime
            except OSError:
                until = args.until  # marker absent -> no upper bound, not an empty set
        dirs = _parse_projects_arg(args.projects) if args.projects else []
        for spec in (args.agent_home or "").split(","):
            spec = spec.strip()
            if not spec:
                continue
            h, _, lbl = spec.partition(":")
            dirs.extend(agent_session_dirs(h, lbl or "main"))
        if not dirs:
            build_parser().error("one of --projects / --agent-home is required")
        w = collect_local(
            dirs,
            args.dest,
            since,
            args.stage,
            args.stream,
            until,
            args.max_bytes,
        )
        print("[trajectory] collected %d file(s) -> %s" % (len(w), args.dest))
    elif args.cmd == "explain":
        for line in explain_stop(args.trajectory, args.indent):
            print(line)
    elif args.cmd == "status":
        print(agent_terminal_status(args.trajectory))
    elif args.cmd == "rename":
        w = rename_collected(args.dest, args.stage, args.user, args.stream)
        print("[trajectory] normalised %d file(s) in %s" % (len(w), args.dest))
    elif args.cmd == "surface":
        w = surface_stage(args.stage_dir, args.output_dir, args.stage)
        print(
            "[trajectory] surfaced %d file(s) from %s -> %s"
            % (len(w), args.stage_dir, args.output_dir)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
