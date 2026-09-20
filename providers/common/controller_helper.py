#!/usr/bin/env python3
"""Small file-backed helpers used by public sandbox controller scripts."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import sys
import tarfile
from pathlib import Path


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def check_modules(names: list[str]) -> int:
    missing = [name for name in names if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemExit(f"missing Python modules: {missing}")
    print("python modules: ok")
    return 0


def write_visual_metrics(args: argparse.Namespace) -> int:
    payload = {
        "task_id": args.task_id,
        "platform": args.platform,
        "stage": "visual_only",
        "pipeline_exit_code": 0,
        "passed": True,
        "task_score": None,
        "visual_only": True,
    }
    if args.platform == "linux":
        payload.update(
            {
                "program_score": None,
                "vlm_score": None,
                "eval_status": "not_evaluated",
                "recreation_success": True,
            }
        )
    if args.url:
        payload["url"] = args.url
    write_json(args.output, payload)
    if args.print_json:
        print("metrics.json:")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def write_error_metrics(args: argparse.Namespace) -> int:
    write_json(
        args.output,
        {
            "task_id": args.task_id,
            "platform": args.platform,
            "stage": args.stage,
            "pipeline_exit_code": args.exit_code,
            "passed": False,
            "task_score": 0.0,
            "eval_status": "pipeline_error",
            "error": "pipeline did not produce metrics.json",
        },
    )
    return 0


def write_setup_metrics(args: argparse.Namespace) -> int:
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except Exception as exc:
        report = {"passed": False, "error": f"permission report missing or invalid: {exc}"}
    passed = report.get("passed") is True and args.exit_code == 0
    native_exit = args.exit_code or (0 if passed else 2)
    status = "pass" if passed else "error"
    write_json(
        args.output,
        {
            "task_id": args.task_id,
            "platform": "web",
            "stage": "setup",
            "passed": passed,
            "pipeline_exit_code": native_exit,
            "native_exit_code": args.exit_code,
            "task_score": None,
            "excluded_from_scoring": True,
            "score_absent_reason": "stage 'setup' produced no recreation eval result",
            "stage_setup": "succeeded" if passed else "failed",
            "stage_outcomes": {
                "setup": {
                    "status": status,
                    "outcome_class": "completed" if passed else "infra_error",
                    "reason_code": "completed" if passed else "permission_setup_failed",
                    "native_exit_code": args.exit_code,
                    "result_complete": passed,
                    "retryable": not passed,
                }
            },
            "permission_report": report,
        },
    )
    return 0


def materialize_web_input(args: argparse.Namespace) -> int:
    if not args.local_root:
        raise SystemExit("RB_UNIFIED_LOCAL_ROOT is required for non-setup stages")
    from common.rb_unify.eval_bridge import copy_unified_from_local, materialize_web_dataset

    report = copy_unified_from_local(
        Path(args.local_root),
        "web",
        args.task_id,
        args.instance_dir,
        components=("reference", "tests"),
        override=args.app,
    )
    if not report.get("prefix"):
        raise SystemExit(f"unified instance not found: {report.get('tried', [])}")
    domain = args.domain or Path(report["prefix"]).name
    materialized = materialize_web_dataset(args.instance_dir, args.dataset_dir, domain=domain)
    required = ("task_json", "site_meta", "eval_config")
    if (
        any(not materialized.get(name) for name in required)
        or not materialized.get("site")
        or not materialized.get("specs")
    ):
        detail = json.dumps(materialized, ensure_ascii=False)
        raise SystemExit(f"incomplete web unified input: {detail}")
    print(domain)
    return 0


def render_codex_config(
    *,
    model: str,
    base_url: str,
    platform: str,
    environ: dict[str, str] | None = None,
) -> str:
    from core.agent_config import codex_config_toml, codex_model_slug
    from core.model_endpoint import from_environment as endpoint_from_environment

    endpoint = endpoint_from_environment(
        environ or {},
        provider="recreationbench",
        name="RecreationBench model endpoint",
        base_url=base_url,
        wire_api="responses" if platform in {"macos", "web"} else "",
    )
    return codex_config_toml(codex_model_slug(model), endpoint)


def write_codex_config(args: argparse.Namespace) -> int:

    args.output.write_text(
        render_codex_config(
            model=args.model,
            base_url=args.base_url,
            platform=args.platform,
            environ=os.environ,
        ),
        encoding="utf-8",
    )
    return 0


def seal_web_inputs(args: argparse.Namespace) -> int:
    """Apply and attest the Web provider's root/agent filesystem boundary."""
    from core.permission_setup import PermissionSpec, setup

    workspace = args.workspace.absolute()
    if len(workspace.parent.parts) < 3:
        raise SystemExit("workspace must have a dedicated output parent")
    if workspace.parent.is_symlink():
        raise SystemExit("output parent must not be a symlink")

    workspace.parent.mkdir(parents=True, exist_ok=True)
    os.chown(workspace.parent, 0, 0)
    workspace.parent.chmod(0o755)
    spec = PermissionSpec.from_dict(
        {
            "schema_version": 1,
            "platform": "web",
            "run_id": args.run_id,
            "trusted": {"user": "root"},
            "agent": {"user": args.agent_user},
            "paths": {
                "protected": [str(path.absolute()) for path in args.protected],
                "writable": [{"path": str(workspace), "mode": "0755"}],
            },
        }
    )
    report = setup(spec).as_dict()
    write_json(args.report, report)
    if not report["passed"]:
        failures = "; ".join(
            check["detail"] for check in report["checks"] if not check["ok"]
        )
        print(f"Web permission attestation failed: {failures}", file=sys.stderr)
        return 77
    print("Web input and scorer permissions verified")
    return 0


def parse_last_json(path: Path) -> dict:
    text = path.read_text(errors="replace")
    for offset, char in enumerate(reversed(text)):
        if char != "{":
            continue
        try:
            value = json.loads(text[len(text) - offset - 1 :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def write_web_result_metrics(args: argparse.Namespace) -> int:
    result = parse_last_json(args.input)
    write_json(
        args.output,
        {
            "task_id": args.task_id,
            "platform": "web",
            "stage": args.stage,
            "pipeline_exit_code": args.exit_code,
            "passed": result.get("status") == "success",
            "task_score": result.get("final_score", 0.0),
            "web_result": result,
        },
    )
    return 0


def join_parts(args: argparse.Namespace) -> int:
    parts = sorted(args.parts_dir.glob("part-*"))
    if not parts:
        raise SystemExit(f"no upload parts found in {args.parts_dir}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as output:
        for part in parts:
            with part.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
    print("joined_parts", len(parts), args.output.stat().st_size)
    return 0


def pack_linux_results(args: argparse.Namespace) -> int:
    task_root = args.output_dir / "pipeline_state" / args.task_id

    def add_if_exists(archive: tarfile.TarFile, path: Path, arcname: str) -> None:
        if path.exists():
            archive.add(path, arcname=arcname)

    args.bundle.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.bundle, "w:gz") as archive:
        add_if_exists(archive, args.output_dir / "metrics.json", "metrics.json")
        add_if_exists(archive, task_root, "pipeline_state")
        for recreation in sorted(task_root.glob("recreation_*/recreation")):
            add_if_exists(archive, recreation, "recreation")
            break
        eval_dir = task_root / "eval"
        if eval_dir.exists():
            add_if_exists(archive, eval_dir, "eval")
        else:
            for candidate in sorted(task_root.glob("atspi_eval_*")):
                add_if_exists(archive, candidate, "eval")
                break
    print(args.bundle, args.bundle.stat().st_size)
    return 0


def remote_status(args: argparse.Namespace) -> int:
    pid = args.pid.read_text().strip() if args.pid.exists() else ""
    alive = bool(pid and (Path("/proc") / pid).exists())
    exit_value = args.exit.read_text().strip() if args.exit.exists() else ""
    print("pid", pid, "alive", int(alive), "exit", exit_value)
    print("log_size", args.log.stat().st_size if args.log.exists() else 0)
    print("metrics", int(args.metrics.exists()))
    if args.log.exists():
        print("--- log_tail ---")
        print("\n".join(args.log.read_text(errors="replace").splitlines()[-args.tail_lines :]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-modules")
    check.add_argument("modules", nargs="+")
    check.set_defaults(run=lambda args: check_modules(args.modules))

    secret = sub.add_parser("random-token")
    secret.set_defaults(run=lambda _args: print(secrets.token_urlsafe(12)) or 0)

    visual = sub.add_parser("write-visual-metrics")
    visual.add_argument("--task-id", required=True)
    visual.add_argument("--platform", required=True)
    visual.add_argument("--output", required=True, type=Path)
    visual.add_argument("--url", default="")
    visual.add_argument("--print-json", action="store_true")
    visual.set_defaults(run=write_visual_metrics)

    error = sub.add_parser("write-error-metrics")
    error.add_argument("--task-id", required=True)
    error.add_argument("--platform", required=True)
    error.add_argument("--stage", required=True)
    error.add_argument("--exit-code", required=True, type=int)
    error.add_argument("--output", required=True, type=Path)
    error.set_defaults(run=write_error_metrics)

    setup = sub.add_parser("write-web-setup-metrics")
    setup.add_argument("--task-id", required=True)
    setup.add_argument("--report", required=True, type=Path)
    setup.add_argument("--exit-code", required=True, type=int)
    setup.add_argument("--output", required=True, type=Path)
    setup.set_defaults(run=write_setup_metrics)

    materialize = sub.add_parser("materialize-web-input")
    materialize.add_argument("--task-id", required=True)
    materialize.add_argument("--instance-dir", required=True, type=Path)
    materialize.add_argument("--dataset-dir", required=True, type=Path)
    materialize.add_argument("--local-root", required=True)
    materialize.add_argument("--app", default="")
    materialize.add_argument("--domain", default="")
    materialize.set_defaults(run=materialize_web_input)

    codex = sub.add_parser("write-codex-config")
    codex.add_argument("--model", required=True)
    codex.add_argument("--base-url", required=True)
    codex.add_argument("--platform", required=True)
    codex.add_argument("--output", required=True, type=Path)
    codex.set_defaults(run=write_codex_config)

    seal = sub.add_parser("seal-web-inputs")
    seal.add_argument("--workspace", required=True, type=Path)
    seal.add_argument("--protected", required=True, action="append", type=Path)
    seal.add_argument("--report", required=True, type=Path)
    seal.add_argument("--run-id", required=True)
    seal.add_argument("--agent-user", default="agent")
    seal.set_defaults(run=seal_web_inputs)

    web_result = sub.add_parser("write-web-result-metrics")
    web_result.add_argument("--input", required=True, type=Path)
    web_result.add_argument("--output", required=True, type=Path)
    web_result.add_argument("--task-id", required=True)
    web_result.add_argument("--stage", required=True)
    web_result.add_argument("--exit-code", required=True, type=int)
    web_result.set_defaults(run=write_web_result_metrics)

    join = sub.add_parser("join-parts")
    join.add_argument("--parts-dir", required=True, type=Path)
    join.add_argument("--output", required=True, type=Path)
    join.set_defaults(run=join_parts)

    pack = sub.add_parser("pack-linux-results")
    pack.add_argument("--task-id", required=True)
    pack.add_argument("--output-dir", required=True, type=Path)
    pack.add_argument("--bundle", required=True, type=Path)
    pack.set_defaults(run=pack_linux_results)

    status = sub.add_parser("remote-status")
    status.add_argument("--log", required=True, type=Path)
    status.add_argument("--exit", required=True, type=Path)
    status.add_argument("--metrics", required=True, type=Path)
    status.add_argument("--pid", required=True, type=Path)
    status.add_argument("--tail-lines", type=int, default=40)
    status.set_defaults(run=remote_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.run(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
