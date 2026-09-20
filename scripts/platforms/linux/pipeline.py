#!/usr/bin/env python3
"""Linux (AT-SPI) pipeline for a prepared SSH target.

The caller owns provisioning, readiness and cleanup.  This module consumes the
``TargetSpec`` produced by ``rb run --host`` and runs the native Linux worker.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from core import exit_contract, trajectory
from core.inpod import worker_env
from core.normalize import normalize, read_eval_dir
from core.pipeline import PipelineConfig, TargetSpec, stages_for
from core.vlm_route import desktop_vlm_base_url

PLATFORM = "linux"
PASS_RULE = "exit_and_stages"


def apply_host_env(target: TargetSpec, env: dict) -> dict:
    """Translate the one prepared target into the legacy SSH worker environment."""
    if target.kind != "ssh" or not target.host:
        raise RuntimeError("Linux release requires a prepared SSH target")
    env.update(
        {
            "RB_SSH_HOST": target.host,
            "RB_SSH_USER": target.user,
            "RB_SSH_PASSWORD": target.password,
            "RB_SSH_PORT": str(target.port or 22),
            "RB_SSH_KEY_PATH": target.key_path,
        }
    )
    return env


def normalize_state(
    task_id: str,
    model: str,
    state: dict,
    eval_dir: Path,
    *,
    pipeline_exit: int | None = None,
    requested_stages: Sequence[str] | None = None,
) -> dict:
    """Map native Linux output to the normalized pipeline state.

    Delegates to the
    shared core.normalize; linux stages are already {name:{status:'pass'}})."""
    ev = read_eval_dir(eval_dir)
    structured = state.get("stage_outcomes")
    if requested_stages is not None:
        if not isinstance(structured, Mapping):
            raise ValueError("Linux native state omitted structured stage outcomes")
        raw_stages = structured
        selected = tuple(requested_stages)
        declared_public = state.get("process_exit_code")
        if isinstance(declared_public, bool) or not isinstance(declared_public, int):
            raise ValueError("Linux native state omitted integer process_exit_code")
    else:
        raw_stages = structured or state.get("stages", {}) or {}
        selected = tuple(raw_stages)
    missing = [name for name in selected if name not in raw_stages]
    if missing:
        raise ValueError(f"Linux native state omitted requested stages: {missing}")
    selected_stages = {name: raw_stages[name] for name in selected}
    if pipeline_exit is None:
        pipeline_exit = state.get("process_exit_code", state.get("pipeline_exit"))
    normalized = normalize(
        task_id,
        model,
        selected_stages,
        {"eval": ev} if ev else {},
        summary=state.get("summary"),
        pipeline_exit=pipeline_exit,
    )
    normalized = exit_contract.apply_stage_outcomes(
        normalized,
        normalized["stage_outcomes"],
        selected,
    )
    if requested_stages is not None and (
        normalized["process_exit_code"] != declared_public
    ):
        raise ValueError(
            "Linux native process_exit_code conflicts with structured stage outcomes"
        )
    native_exit = exit_contract.selected_native_exit_code(
        normalized["stage_outcomes"], selected
    )
    normalized["native_exit_code"] = (
        native_exit if native_exit is not None else normalized["pipeline_exit"]
    )
    return normalized


def run(task: dict, cfg: PipelineConfig) -> dict:
    """Drive the prepared Linux target and return one normalized state."""
    if cfg.target.kind != "ssh" or not cfg.target.host:
        raise RuntimeError(
            "Linux requires a prepared SSH target; provision it first and pass --host"
        )
    return _run_on_host(task, cfg)


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def surface_recreation_artifacts(
    task_id: str,
    stage: str,
    output_dir: Path,
    *,
    environ=None,
) -> None:
    """Surface Linux trajectory products from local state or their canonical artifact store keys."""
    if stage not in {"recreation", "recreation_eval"}:
        return

    environ = os.environ if environ is None else environ
    preferred = output_dir / "pipeline_state" / task_id / "recreation"
    local_stage = preferred if preferred.is_dir() else None
    if local_stage is None:
        local_stage = next(
            (
                path
                for path in sorted(output_dir.glob("pipeline_state/**/recreation"))
                if path.is_dir()
            ),
            None,
        )
    if local_stage is not None:
        surfaced = trajectory.surface_stage(local_stage, output_dir, stage="recreation")
        if surfaced:
            print(
                f"[trajectory] surfaced {len(surfaced)} local artifact(s) -> {output_dir}",
                flush=True,
            )

    destination = output_dir / trajectory.CANONICAL_STREAM_NAME
    sessions_destination = output_dir / trajectory.CANONICAL_SESSIONS_DIR
    local_sessions = (
        local_stage / trajectory.CANONICAL_SESSIONS_DIR if local_stage else None
    )
    need_stream = not _nonempty_file(destination)
    need_sessions = local_sessions is None or not local_sessions.is_dir()
    if not need_stream and not need_sessions:
        return

    prefix = str(environ.get("RB_ARTIFACT_PREFIX", "")).rstrip("/")
    if not prefix or not task_id:
        if need_stream:
            print(
                "[trajectory] WARN: no local trajectory and artifact config incomplete; skip",
                flush=True,
            )
        return

    try:
        from infrastructure.artifacts import artifact_store_from_environment

        store = artifact_store_from_environment(environ)
    except Exception as exc:
        print(
            f"[trajectory] WARN: could not initialise artifact store: {exc}",
            flush=True,
        )
        return

    layout = trajectory.artifact_layout(prefix, task_id, "recreation")
    if need_stream:
        try:
            store.get_file(layout["trajectory"], destination)
            print(
                f"[trajectory] surfaced {layout['trajectory']} -> {destination}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[trajectory] WARN: could not fetch {layout['trajectory']}: {exc}",
                flush=True,
            )

    if need_sessions:
        count = 0
        try:
            for key in store.iter_keys(layout["sessions"]):
                if key.endswith("/"):
                    continue
                relative = key[len(layout["sessions"]) :]
                if not relative:
                    continue
                target = sessions_destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                store.get_file(key, target)
                count += 1
            print(
                f"[trajectory] surfaced {count} session file(s) from artifact store -> "
                f"{sessions_destination}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[trajectory] WARN: could not fetch sessions from artifact store: {exc}",
                flush=True,
            )


def finalize(metrics: dict, cfg: PipelineConfig, output_path: Path) -> None:
    """Surface deployment platform artifacts and publish Linux metadata after metrics are durable."""
    from infrastructure.artifacts import run_upload

    output_dir = output_path.parent
    task_id = str(metrics.get("task_id") or "")
    try:
        surface_recreation_artifacts(task_id, cfg.stage, output_dir)
    except Exception as exc:
        print(f"[linux] WARN: final trajectory surfacing failed: {exc}", flush=True)
    try:
        run_upload.upload_run(
            task_id=task_id,
            stage=cfg.stage,
            model=cfg.model,
            exit_code=int(metrics.get("process_exit_code", 2)),
            output_dir=str(output_dir),
            subdirs=("logs", "pipeline_state", "sessions"),
            root_files=("metrics.json", "trajectory.jsonl"),
            nested_logs=True,
            stage_copy=True,
        )
    except Exception as exc:
        print(
            f"[linux] WARN: final artifact store metadata upload failed: {exc}",
            flush=True,
        )


def _run_on_host(task: dict, cfg: PipelineConfig) -> dict:
    env = worker_env(task, cfg, os.environ)
    if cfg.extra.get("eval_use_native") not in (None, ""):
        env["RB_EVAL_USE_NATIVE"] = (
            "1"
            if str(cfg.extra["eval_use_native"]).strip().lower()
            in {"1", "true", "yes", "on"}
            else "0"
        )
    if not cfg.vlm_key:
        for name in (
            "RB_VLM_KEY",
            "VLM_API_KEY",
            "VLM_MODEL_API_KEY",
            "VLM_JUDGE_API_KEY",
        ):
            env[name] = cfg.model_api_key
    os.environ.update(env)
    output_dir = Path(
        os.environ.get("RB_OUTPUT_DIR") or os.environ.get("OUTPUT_DIR") or "."
    )
    os.environ["RB_PIPELINE_STATE_DIR"] = str(output_dir / "pipeline_state")
    # Before get_vm_client(): that call is what reads RB_SSH_*.
    apply_host_env(cfg.target, os.environ)
    import linux.worker as native  # imported only for direct/BYO execution

    # The module may already be imported in a long-lived test/submit process; do
    # not let its import-time default point this run at an earlier output tree.
    native.STATE_DIR = output_dir / "pipeline_state"
    client = native.get_vm_client()
    instance_id = cfg.target.host
    image = cfg.extra.get("image") or os.environ.get("DOCKER_IMAGE", "")
    selected = stages_for(cfg.stage)
    try:
        with desktop_vlm_base_url(
            client, cfg.vlm_base_url, enabled="eval" in selected
        ) as guest_vlm_base_url:
            state = native.run_pipeline(
                client,
                task,
                instance_id,
                cfg.model,
                image,
                cfg.model_api_key,
                cfg.model_base_url,
                cfg.auth_token,
                cfg.extra.get("mcp_provider", "cua-driver"),
                cfg.vlm_key or cfg.model_api_key,
                stage=cfg.stage,
                agent_cli=cfg.agent_cli,
                eval_target=cfg.eval_target,
                vlm_model=cfg.vlm_model,
                vlm_base_url=guest_vlm_base_url,
            )
    finally:
        client.close()
    eval_dir = native.STATE_DIR / task["task_id"] / "eval"
    normalized = normalize_state(
        task["task_id"],
        cfg.model,
        state,
        eval_dir,
        requested_stages=selected,
    )
    eval_claims_completion = (
        normalized.get("stage_outcomes", {}).get("eval", {}).get("status")
        == exit_contract.STAGE_PASS
    )
    if (
        "eval" in selected
        and eval_claims_completion
        and not normalized["evals"].get("eval")
    ):
        normalized["stages"]["eval"] = "fail"
        normalized["pipeline_exit"] = exit_contract.RC_INFRA
        normalized["summary"] = {
            "error": "Linux eval produced no readable programmatic/VLM result"
        }
        outcomes = dict(normalized["stage_outcomes"])
        outcomes["eval"] = exit_contract.stage_outcome(
            status=exit_contract.STAGE_ERROR,
            outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
            reason_code="eval_result_missing",
        )
        exit_contract.apply_stage_outcomes(normalized, outcomes, selected)
    native_exit = exit_contract.selected_native_exit_code(
        normalized["stage_outcomes"], selected
    )
    normalized["native_exit_code"] = (
        native_exit if native_exit is not None else normalized["pipeline_exit"]
    )
    return normalized
