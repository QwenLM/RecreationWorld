"""The unified result contract — one `metrics.json` per run, identical on all five platforms.

Describes what ``scripts/core/metrics_contract.assemble_metrics`` already emits. It is deliberately a
READER, not a second source of truth: metrics_contract stays the producer, because a schema that
recomputed anything would be a second scoring path, and the whole point of the unified contract is
that no platform's number is recomputed -- android's macro-average and web's 4-dimension weighted
score are carried through verbatim and only the JSON SHAPE is made consistent.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from recreation_bench.task import Platform, normalize_platform

# metrics_contract normalises every stage to one of these two (it maps its internal "pass" and the
# windows-native labels onto them before emitting).
StageStatus = Literal["succeeded", "failed"]
OutcomeClass = Literal["completed", "data_error", "infra_error", "terminated"]
StageOutcomeStatus = Literal["pass", "fail", "timeout", "error", "not_run"]

_STAGE_PREFIX = "stage_"


class EvalCounts(BaseModel):
    """One dimension's raw counts. ``skipped`` is "" in shipped data, not 0."""

    model_config = ConfigDict(extra="allow")

    passed: int = 0
    total: int = 0
    pass_rate: Optional[float] = None
    skipped: object = ""

    @property
    def rate(self) -> Optional[float]:
        """Prefer the counts over a stored rate: a doubled denominator has shipped before."""
        if self.total:
            return self.passed / self.total
        return self.pass_rate


class StageOutcome(BaseModel):
    """Machine-readable evidence emitted for one requested pipeline stage."""

    model_config = ConfigDict(extra="forbid")

    status: StageOutcomeStatus
    outcome_class: OutcomeClass
    reason_code: str
    native_exit_code: Optional[int] = None
    result_complete: bool
    retryable: bool


class RunResult(BaseModel):
    """One graded run.

    Extras are ALLOWED here, unlike TaskInstance: metrics.json carries one dynamic
    ``stage_<name>`` key per stage that ran, and platforms add their own diagnostics. Use
    ``from_metrics`` so those stage keys land in ``stages`` instead of being lost.
    """

    model_config = ConfigDict(extra="allow")

    task_id: str
    # Normalised onto the SUITE's vocabulary by `from_metrics` (metrics.json says "linux" where
    # instance.json says "ubuntu"), so a task<->result join works on one string.
    platform: Optional[Platform] = None
    # What the run actually reported, before normalisation. Kept so the divergence stays visible
    # rather than being silently rewritten.
    platform_reported: Optional[str] = None
    model: str = ""

    # --- outcome -----------------------------------------------------------------------------
    # `passed` is the orchestration-facing verdict. It is the pass rule over
    # pipeline exit AND the platform's required_stages. A run can grade above zero and still be
    # passed=False if a required stage failed.
    passed: bool = False
    pipeline_exit_code: int = 0
    process_exit_code: int = 0
    native_exit_code: Optional[int] = None
    outcome_class: Optional[OutcomeClass] = None
    reason_code: Optional[str] = None
    result_complete: bool = False
    retryable: bool = False
    stage: Optional[str] = None
    # Folded from the dynamic stage_<name> keys by `from_metrics`.
    stages: dict[str, StageStatus] = Field(default_factory=dict)
    stage_outcomes: dict[str, StageOutcome] = Field(default_factory=dict)

    # --- scores ------------------------------------------------------------------------------
    # None means NOT GRADED, which is not the same as 0.0. A recreation killed at its budget with
    # a complete app leaves these None while a genuinely empty recreation scores 0.0 -- treating
    # the first as zero understates the model. Keep them Optional for that reason.
    task_score: Optional[float] = None
    program_score: Optional[float] = None
    vlm_score: Optional[float] = None
    # Precomputed because deployment platform's group aggregation can average ONE field, not a derived combination.
    prog_vlm_avg: Optional[float] = None

    # --- raw counts behind the scores --------------------------------------------------------
    # TWO SHAPES EXIST IN THE WILD and a consumer meets both, so both parse here:
    #
    #   nested  {"programmatic": {"passed": 131, "total": 131, "pass_rate": 1}, "vlm": {...}}
    #           what older deployment platform Linux runs stored for a recreation_eval run,
    #           alongside "eval_status": "resolved"
    #   flat    {"eval_prog": 131, "eval_prog_n": 131, "eval_vlm": 38, "eval_vlm_n": 60}
    #           what core.metrics_contract.assemble_metrics emits
    #
    # `from_metrics` fills whichever is missing from whichever is present, so the accessors below
    # answer the same for either. Do not "unify" by dropping one: the nested form carries skipped
    # and a stored pass_rate the flat form has no field for.
    programmatic: Optional[EvalCounts] = None
    vlm: Optional[EvalCounts] = None
    eval_prog: int = 0
    eval_prog_n: int = 0
    eval_vlm: int = 0
    eval_vlm_n: int = 0
    eval_vlm_err: int = 0
    # Which eval's numbers the scores came from, when a run has more than one.
    scoring_eval: Optional[str] = None
    # "resolved" on a graded run; present only in the nested shape.
    eval_status: Optional[str] = None

    # --- provenance --------------------------------------------------------------------------
    sandbox_id: Optional[str] = None
    sandbox_ip: Optional[str] = None

    @classmethod
    def from_metrics(cls, metrics: dict) -> "RunResult":
        """Parse an emitted metrics.json, folding ``stage_<name>`` keys into ``stages``."""
        data = {
            k: v
            for k, v in metrics.items()
            if k == "stage_outcomes" or not k.startswith(_STAGE_PREFIX)
        }
        data["stages"] = {
            k[len(_STAGE_PREFIX) :]: v
            for k, v in metrics.items()
            if k != "stage_outcomes" and k.startswith(_STAGE_PREFIX)
        }
        raw_platform = data.get("platform")
        if raw_platform:
            data = {**data, "platform": normalize_platform(str(raw_platform))}
            data["platform_reported"] = str(raw_platform)
        obj = cls(**data)
        # Cross-fill the two shapes so callers never branch on which one they were handed.
        if obj.programmatic is None and obj.eval_prog_n:
            obj.programmatic = EvalCounts(passed=obj.eval_prog, total=obj.eval_prog_n)
        if obj.vlm is None and obj.eval_vlm_n:
            obj.vlm = EvalCounts(passed=obj.eval_vlm, total=obj.eval_vlm_n)
        if obj.programmatic is not None and not obj.eval_prog_n:
            obj.eval_prog, obj.eval_prog_n = (
                obj.programmatic.passed,
                obj.programmatic.total,
            )
        if obj.vlm is not None and not obj.eval_vlm_n:
            obj.eval_vlm, obj.eval_vlm_n = obj.vlm.passed, obj.vlm.total
        return obj

    @property
    def graded(self) -> bool:
        """Whether a score exists at all. False for the no-grade case, which is distinct from 0.0."""
        return self.task_score is not None

    @property
    def prog_pass_rate(self) -> Optional[float]:
        """Programmatic pass rate from the raw counts, whichever shape supplied them."""
        if self.eval_prog_n:
            return self.eval_prog / self.eval_prog_n
        return self.programmatic.rate if self.programmatic else None

    @property
    def vlm_pass_rate(self) -> Optional[float]:
        if self.eval_vlm_n:
            return self.eval_vlm / self.eval_vlm_n
        return self.vlm.rate if self.vlm else None
