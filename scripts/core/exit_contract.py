#!/usr/bin/env python3
"""One public process-exit contract for every RecreationBench platform.

Native workers keep their own detailed exit/status vocabulary in the normalized
state and artifacts.  The deployment platform process, however, exposes exactly four
codes, selected here after the final result has been assembled:

``0`` completed
    A terminal, authoritative result exists.  This includes a low score, a
    legitimate model-failure zero, and an explicit scoring exclusion.
``1`` data_error
    The frozen task/input is deterministically invalid or unavailable.  Running
    the same task again is not expected to help.
``2`` infra_error
    Runtime, bootstrap, transport, judge, or contract infrastructure failed.
``143`` terminated
    The deployment platform runtime delivered SIGTERM (normally the wall-clock deadline).

The mapping deliberately does not use ``metrics.passed``.  ``passed`` describes
pipeline health, while the process code tells an orchestrator whether a terminal
result exists and whether retrying can help.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

OUTCOME_COMPLETED: Final = "completed"
OUTCOME_DATA_ERROR: Final = "data_error"
OUTCOME_INFRA_ERROR: Final = "infra_error"
OUTCOME_TERMINATED: Final = "terminated"

OUTCOME_CLASSES: Final = (
    OUTCOME_COMPLETED,
    OUTCOME_DATA_ERROR,
    OUTCOME_INFRA_ERROR,
    OUTCOME_TERMINATED,
)

RC_OK: Final = 0
RC_DATA: Final = 1
RC_INFRA: Final = 2
RC_TIMEOUT: Final = 143

_PROCESS_CODES: Final = {
    OUTCOME_COMPLETED: RC_OK,
    OUTCOME_DATA_ERROR: RC_DATA,
    OUTCOME_INFRA_ERROR: RC_INFRA,
    OUTCOME_TERMINATED: RC_TIMEOUT,
}

STAGE_PASS: Final = "pass"
STAGE_FAIL: Final = "fail"
STAGE_TIMEOUT: Final = "timeout"
STAGE_ERROR: Final = "error"
STAGE_NOT_RUN: Final = "not_run"

STAGE_STATUSES: Final = (
    STAGE_PASS,
    STAGE_FAIL,
    STAGE_TIMEOUT,
    STAGE_ERROR,
    STAGE_NOT_RUN,
)

_OUTCOME_PRIORITY: Final = {
    OUTCOME_COMPLETED: 0,
    OUTCOME_DATA_ERROR: 1,
    OUTCOME_INFRA_ERROR: 2,
    OUTCOME_TERMINATED: 3,
}

_REASON_RE = re.compile(r"[^a-z0-9_]+")


def validate_outcome_class(value: object) -> str:
    """Return a valid outcome class or fail at the shared boundary."""
    name = str(value or "").strip().lower()
    if name not in OUTCOME_CLASSES:
        raise ValueError(
            f"unknown outcome_class {value!r}; expected one of {OUTCOME_CLASSES}"
        )
    return name


def normalize_reason_code(value: object, *, fallback: str) -> str:
    """Return one stable, machine-readable reason code.

    Native workers may still carry a free-form ``error`` alongside this field.
    The reason code is deliberately small and portable so orchestration never
    has to parse platform log text.
    """
    reason = _REASON_RE.sub("_", str(value or "").strip().lower()).strip("_")
    return (reason or fallback)[:96]


def stage_outcome(
    *,
    status: str,
    outcome_class: str,
    reason_code: str,
    native_exit_code: int | None = None,
    result_complete: bool | None = None,
    retryable: bool | None = None,
) -> dict:
    """Build and validate one platform-independent stage result."""
    canonical_status = str(status or "").strip().lower()
    if canonical_status not in STAGE_STATUSES:
        raise ValueError(
            f"unknown stage status {status!r}; expected one of {STAGE_STATUSES}"
        )
    outcome = validate_outcome_class(outcome_class)
    if native_exit_code is not None and (
        isinstance(native_exit_code, bool) or not isinstance(native_exit_code, int)
    ):
        raise ValueError("native_exit_code must be an integer or null")

    expected_complete = outcome == OUTCOME_COMPLETED
    expected_retryable = outcome in (OUTCOME_INFRA_ERROR, OUTCOME_TERMINATED)
    complete = expected_complete if result_complete is None else result_complete
    can_retry = expected_retryable if retryable is None else retryable
    if not isinstance(complete, bool) or not isinstance(can_retry, bool):
        raise ValueError("result_complete and retryable must be booleans")
    if canonical_status == STAGE_PASS and outcome != OUTCOME_COMPLETED:
        raise ValueError("a passing stage must have outcome_class=completed")
    if canonical_status == STAGE_ERROR and outcome != OUTCOME_INFRA_ERROR:
        raise ValueError("an error stage must have outcome_class=infra_error")
    if canonical_status == STAGE_FAIL and outcome not in (
        OUTCOME_COMPLETED,
        OUTCOME_DATA_ERROR,
    ):
        raise ValueError(
            "a failed stage must have outcome_class=completed or data_error"
        )
    if canonical_status == STAGE_TIMEOUT and outcome != OUTCOME_TERMINATED:
        raise ValueError("a timed-out stage must have outcome_class=terminated")
    if outcome == OUTCOME_TERMINATED and canonical_status not in (
        STAGE_TIMEOUT,
        STAGE_NOT_RUN,
    ):
        raise ValueError("terminated stage outcome must be timeout or not_run")
    if complete != expected_complete:
        raise ValueError(
            f"{outcome} stage outcome must have result_complete={expected_complete!r}"
        )
    if can_retry != expected_retryable:
        raise ValueError(
            f"{outcome} stage outcome must have retryable={expected_retryable!r}"
        )

    return {
        "status": canonical_status,
        "outcome_class": outcome,
        "reason_code": normalize_reason_code(reason_code, fallback="unspecified"),
        "native_exit_code": native_exit_code,
        "result_complete": complete,
        "retryable": can_retry,
    }


def validate_stage_outcome(value: object) -> dict:
    """Validate and return a canonical copy of one stage outcome."""
    if not isinstance(value, Mapping):
        raise ValueError("stage outcome must be a mapping")
    return stage_outcome(
        status=value.get("status"),
        outcome_class=value.get("outcome_class"),
        reason_code=value.get("reason_code"),
        native_exit_code=value.get("native_exit_code"),
        result_complete=value.get("result_complete"),
        retryable=value.get("retryable"),
    )


def outcome_from_stage_status(
    status: str,
    *,
    raw: Mapping | None = None,
    native_exit_code: int | None = None,
) -> dict:
    """Normalize a legacy native stage record, failing unknown errors closed.

    This is a migration boundary, not a platform policy engine. A successful
    stage is completed, an explicit timeout is terminated, and every otherwise
    unclassified failure is infrastructure. Platforms must explicitly declare
    deterministic data errors and valid model-zero results.
    """
    raw = raw or {}
    explicit = raw.get("outcome_class")
    if explicit is not None:
        outcome = validate_outcome_class(explicit)
    elif status == STAGE_PASS:
        outcome = OUTCOME_COMPLETED
    elif status == STAGE_TIMEOUT or native_exit_code == RC_TIMEOUT:
        outcome = OUTCOME_TERMINATED
    else:
        outcome = OUTCOME_INFRA_ERROR

    reason = raw.get("reason_code") or raw.get("fail_code") or raw.get("error")
    if not reason:
        reason = {
            STAGE_PASS: "completed",
            STAGE_TIMEOUT: "stage_timeout",
            STAGE_NOT_RUN: "stage_not_run",
        }.get(status, "stage_failed")
    # A legacy flat ``fail`` carries no classification. Fail closed as an
    # infrastructure ``error`` until the platform supplies explicit evidence;
    # keeping ``status=fail`` with ``infra_error`` would blur the shared
    # fail-vs-error distinction we enforce at the boundary.
    canonical_status = (
        STAGE_ERROR
        if status == STAGE_FAIL and outcome == OUTCOME_INFRA_ERROR
        else status
    )
    return stage_outcome(
        status=canonical_status,
        outcome_class=outcome,
        reason_code=str(reason),
        native_exit_code=native_exit_code,
        result_complete=raw.get("result_complete"),
        retryable=raw.get("retryable"),
    )


def aggregate_stage_outcomes(
    outcomes: Mapping[str, object], requested_stages: Sequence[str]
) -> dict:
    """Reduce canonical stage results to the one public process outcome."""
    if not isinstance(outcomes, Mapping):
        raise ValueError("stage_outcomes must be a mapping")
    requested = tuple(requested_stages)
    if not requested:
        raise ValueError("requested_stages must not be empty")
    missing = [name for name in requested if name not in outcomes]
    if missing:
        raise ValueError(f"stage_outcomes omitted requested stages: {missing}")

    canonical = {name: validate_stage_outcome(outcomes[name]) for name in requested}
    selected_name = max(
        requested,
        key=lambda name: _OUTCOME_PRIORITY[canonical[name]["outcome_class"]],
    )
    selected = canonical[selected_name]
    return {
        "outcome_class": selected["outcome_class"],
        "reason_code": selected["reason_code"],
        "result_complete": all(item["result_complete"] for item in canonical.values()),
        "retryable": selected["retryable"],
        "process_exit_code": _PROCESS_CODES[selected["outcome_class"]],
    }


def selected_native_exit_code(
    outcomes: Mapping[str, object], requested_stages: Sequence[str]
) -> int | None:
    """Return the native code belonging to the aggregate's decisive stage."""
    requested = tuple(requested_stages)
    canonical = {name: validate_stage_outcome(outcomes[name]) for name in requested}
    selected_name = max(
        requested,
        key=lambda name: _OUTCOME_PRIORITY[canonical[name]["outcome_class"]],
    )
    return canonical[selected_name]["native_exit_code"]


def apply_stage_outcomes(
    state: dict, outcomes: Mapping[str, object], requested_stages: Sequence[str]
) -> dict:
    """Attach canonical per-stage evidence and its aggregate to ``state``."""
    canonical = {
        name: validate_stage_outcome(value) for name, value in outcomes.items()
    }
    state["stage_outcomes"] = canonical
    aggregate = aggregate_stage_outcomes(canonical, requested_stages)
    state.update(aggregate)
    state["pipeline_exit"] = aggregate["process_exit_code"]
    return state


def validate_state_outcomes(state: dict, requested_stages: Sequence[str]) -> dict:
    """Require coherent per-stage and aggregate evidence at the shared boundary."""
    outcomes = state.get("stage_outcomes")
    aggregate = aggregate_stage_outcomes(outcomes, requested_stages)
    canonical = {
        name: validate_stage_outcome(value) for name, value in outcomes.items()
    }
    flat = state.get("stages") or {}
    for name in requested_stages:
        outcome_status = canonical[name]["status"]
        expected_flat = (
            STAGE_PASS
            if outcome_status == STAGE_PASS
            else STAGE_NOT_RUN if outcome_status == STAGE_NOT_RUN else STAGE_FAIL
        )
        if flat.get(name) != expected_flat:
            raise ValueError(
                f"stage {name!r} status {flat.get(name)!r} conflicts with "
                f"stage_outcomes status {outcome_status!r}"
            )
    for field in (
        "outcome_class",
        "reason_code",
        "result_complete",
        "retryable",
        "process_exit_code",
    ):
        if field in state and state[field] != aggregate[field]:
            raise ValueError(
                f"state {field}={state[field]!r} conflicts with stage outcomes "
                f"({aggregate[field]!r})"
            )
    if state.get("pipeline_exit") != aggregate["process_exit_code"]:
        raise ValueError(
            f"state pipeline_exit={state.get('pipeline_exit')!r} conflicts with "
            f"stage outcomes ({aggregate['process_exit_code']!r})"
        )
    state["stage_outcomes"] = canonical
    state.update(aggregate)
    return state


def override_pipeline_outcome(
    state: dict,
    requested_stages: Sequence[str],
    *,
    outcome_class: str,
    reason_code: str,
    native_exit_code: int | None,
) -> dict:
    """Replace the terminal stage outcome with authoritative wrapper evidence."""
    requested = tuple(requested_stages)
    outcomes = {
        name: validate_stage_outcome(value)
        for name, value in (state.get("stage_outcomes") or {}).items()
    }
    target = next(
        (
            name
            for name in requested
            if (state.get("stages") or {}).get(name) != STAGE_PASS
        ),
        requested[-1],
    )
    outcome = validate_outcome_class(outcome_class)
    status = (
        STAGE_TIMEOUT
        if outcome == OUTCOME_TERMINATED
        else STAGE_FAIL if outcome == OUTCOME_DATA_ERROR else STAGE_ERROR
    )
    state.setdefault("stages", {})[target] = STAGE_FAIL
    outcomes[target] = stage_outcome(
        status=status,
        outcome_class=outcome,
        reason_code=reason_code,
        native_exit_code=native_exit_code,
    )
    return apply_stage_outcomes(state, outcomes, requested)


def requested_stage_names(
    requested_stage: str | None, state: Mapping
) -> tuple[str, ...]:
    if requested_stage == "recreation_eval":
        return ("recreation", "eval")
    if requested_stage:
        return (requested_stage,)
    stages = state.get("stages")
    return tuple(stages) if isinstance(stages, Mapping) else ()


def from_native_exit(code: object, *, one: str = OUTCOME_DATA_ERROR) -> str:
    """Translate a worker code when that worker has no richer outcome field.

    Code 1 is the only ambiguous legacy value.  Android already defines it as
    deterministic data failure; callers whose worker historically collapsed all
    errors to 1 pass ``one=OUTCOME_INFRA_ERROR`` instead.
    """
    try:
        rc = int(code)
    except (TypeError, ValueError):
        return OUTCOME_INFRA_ERROR
    if rc == RC_OK:
        return OUTCOME_COMPLETED
    if rc in (RC_TIMEOUT, 128 + 2, -2, -15):
        return OUTCOME_TERMINATED
    if rc == RC_DATA:
        return validate_outcome_class(one)
    if rc == RC_INFRA:
        return OUTCOME_INFRA_ERROR
    # Arbitrary child codes are native evidence, not additions to the public
    # contract.  Unknown failures are fail-closed as infrastructure errors.
    return OUTCOME_INFRA_ERROR


def infer_outcome_class(
    state: dict, metrics: dict, *, requested_stage: str | None = None
) -> str:
    """Infer the public outcome after platform normalization.

    Adapters should set ``state.outcome_class`` whenever native evidence can
    distinguish data from infrastructure.  This fallback exists for the desktop
    workers that still report a legacy binary code; their shared ``exit_code``
    taxonomy supplies the infra distinction where it is known.
    """
    stage_outcomes = state.get("stage_outcomes")
    requested = requested_stage_names(requested_stage, state)
    if stage_outcomes is not None and requested:
        return aggregate_stage_outcomes(stage_outcomes, requested)["outcome_class"]

    # Only a platform's explicit exclusion is a completed terminal result.  The
    # metrics builder also marks setup-only runs as excluded from score
    # aggregation; that bookkeeping marker must not turn a failed setup into rc=0.
    if state.get("excluded_from_scoring"):
        return OUTCOME_COMPLETED

    explicit = state.get("outcome_class")
    if explicit is not None:
        return validate_outcome_class(explicit)

    try:
        native = int(state.get("pipeline_exit", metrics.get("pipeline_exit_code", 0)))
    except (TypeError, ValueError):
        return OUTCOME_INFRA_ERROR

    native_outcome = from_native_exit(native)
    if native_outcome == OUTCOME_TERMINATED:
        return OUTCOME_TERMINATED
    if native == RC_OK:
        return OUTCOME_COMPLETED

    exit_code = str(metrics.get("exit_code") or "")
    if (
        native == RC_INFRA
        or exit_code.startswith("infra:")
        or (state.get("sandbox_info") or {}).get("error")
    ):
        return OUTCOME_INFRA_ERROR

    # A failed permission/setup diagnostic is an environment boundary failure,
    # never malformed benchmark data.
    if requested_stage == "setup":
        return OUTCOME_INFRA_ERROR

    # The remaining legacy code 1 is a deterministic stage/data failure.  As
    # adapters gain richer native reports they override this explicitly.
    return OUTCOME_DATA_ERROR


def describe(state: dict, metrics: dict, *, requested_stage: str | None = None) -> dict:
    """Return the shared outcome fields added to every ``metrics.json``."""
    stage_outcomes = state.get("stage_outcomes")
    requested = requested_stage_names(requested_stage, state)
    if stage_outcomes is not None and requested:
        aggregate = aggregate_stage_outcomes(stage_outcomes, requested)
        for field in (
            "outcome_class",
            "reason_code",
            "result_complete",
            "retryable",
            "process_exit_code",
        ):
            if field in state and state[field] != aggregate[field]:
                raise ValueError(
                    f"state {field}={state[field]!r} conflicts with stage outcomes "
                    f"({aggregate[field]!r})"
                )
        return aggregate

    outcome = infer_outcome_class(state, metrics, requested_stage=requested_stage)
    result_complete = state.get("result_complete")
    if result_complete is None:
        result_complete = outcome == OUTCOME_COMPLETED
    retryable = state.get("retryable")
    if retryable is None:
        retryable = outcome in (OUTCOME_INFRA_ERROR, OUTCOME_TERMINATED)
    fields = {
        "outcome_class": outcome,
        "result_complete": bool(result_complete),
        "retryable": bool(retryable),
        "process_exit_code": _PROCESS_CODES[outcome],
    }
    if state.get("reason_code"):
        fields["reason_code"] = normalize_reason_code(
            state["reason_code"], fallback="unspecified"
        )
    return fields


def process_exit_code(metrics_or_outcome: dict | str) -> int:
    """Map a final metrics object (or class name) to the public process code."""
    if isinstance(metrics_or_outcome, dict):
        declared = metrics_or_outcome.get("process_exit_code")
        outcome = validate_outcome_class(metrics_or_outcome.get("outcome_class"))
        expected = _PROCESS_CODES[outcome]
        if declared is not None and int(declared) != expected:
            raise ValueError(
                f"process_exit_code {declared!r} conflicts with outcome_class {outcome!r}"
            )
        return expected
    return _PROCESS_CODES[validate_outcome_class(metrics_or_outcome)]


__all__ = [
    "OUTCOME_CLASSES",
    "OUTCOME_COMPLETED",
    "OUTCOME_DATA_ERROR",
    "OUTCOME_INFRA_ERROR",
    "OUTCOME_TERMINATED",
    "RC_OK",
    "RC_DATA",
    "RC_INFRA",
    "RC_TIMEOUT",
    "STAGE_ERROR",
    "STAGE_FAIL",
    "STAGE_NOT_RUN",
    "STAGE_PASS",
    "STAGE_STATUSES",
    "STAGE_TIMEOUT",
    "aggregate_stage_outcomes",
    "apply_stage_outcomes",
    "describe",
    "from_native_exit",
    "infer_outcome_class",
    "normalize_reason_code",
    "outcome_from_stage_status",
    "override_pipeline_outcome",
    "process_exit_code",
    "requested_stage_names",
    "selected_native_exit_code",
    "stage_outcome",
    "validate_stage_outcome",
    "validate_state_outcomes",
    "validate_outcome_class",
]
