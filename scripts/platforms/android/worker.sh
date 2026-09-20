#!/bin/bash
# ==========================================================================
# Android App Recreation — RecreationBench native worker
# ==========================================================================
# Release lifecycle: Recreation -> Eval. The reference app and frozen tests are
# materialized exclusively from one unified instance. Setup is a standalone
# permission diagnostic.
#
# ==========================================================================




# NOTE: no `set -x` here. Command tracing echoes every expanded argument into the
# deployment job log, which leaks MODEL_API_KEY / ANTHROPIC_AUTH_TOKEN and artifact
# store credentials to anyone who can read job logs. The masked env dump below gives
# the same debuggability without exposing credentials.
set -eo pipefail

readonly RC_OK=0 RC_DATA=1 RC_INFRA=2 RC_TIMEOUT=143

normalize_public_rc() {
    case "${1:-$RC_INFRA}" in
        "$RC_OK"|"$RC_DATA"|"$RC_INFRA"|"$RC_TIMEOUT") printf '%s' "$1" ;;
        130) printf '%s' "$RC_TIMEOUT" ;;
        *) printf '%s' "$RC_INFRA" ;;
    esac
}
on_unexpected_error() {
    local native_rc=$?
    trap - ERR
    { [ "$native_rc" -eq 130 ] || [ "$native_rc" -eq "$RC_TIMEOUT" ]; } && exit "$RC_TIMEOUT"
    echo "FATAL: unexpected shell failure (native exit=$native_rc); reporting infrastructure error" >&2
    exit "$RC_INFRA"
}
trap on_unexpected_error ERR

AGENT_CLI="${AGENT_CLI:-claude}"
case "$AGENT_CLI" in
    claude) _PINNED_SCAFFOLD_VERSION="2.1.177" ;;
    codex)  _PINNED_SCAFFOLD_VERSION="0.145.0" ;;
    *) echo "ERROR: invalid AGENT_CLI=$AGENT_CLI (want claude or codex)" >&2; exit "$RC_INFRA" ;;
esac
SCAFFOLD_VERSION="${SCAFFOLD_VERSION:-${_PINNED_SCAFFOLD_VERSION}}"
[ "$SCAFFOLD_VERSION" = "$_PINNED_SCAFFOLD_VERSION" ] || {
    echo "ERROR: $AGENT_CLI scaffold_version=$SCAFFOLD_VERSION; this release runtime is pinned to $_PINNED_SCAFFOLD_VERSION" >&2
    exit "$RC_INFRA"
}
export AGENT_CLI RB_AGENT_CLI="$AGENT_CLI" SCAFFOLD_VERSION
export LITELLM_PORT="${LITELLM_PORT:-4000}"
RB_CUA_PREFLIGHT_MODE="${RB_CUA_PREFLIGHT_MODE:-strict}"
export RB_CUA_PREFLIGHT_MODE

# The deployment adapter maps provider credentials and legacy parameter names into
# this provider-neutral artifact contract before entering RecreationBench.
RB_ARTIFACT_PREFIX="${RB_ARTIFACT_PREFIX:-recreation-bench/results/android}"
RB_ARTIFACT_RESTORE_PREFIX="${RB_ARTIFACT_RESTORE_PREFIX:-}"
_RB_ARTIFACT_RESTORE_PREFIX_EXPLICIT=0
[ -n "${RB_ARTIFACT_RESTORE_PREFIX:-}" ] && _RB_ARTIFACT_RESTORE_PREFIX_EXPLICIT=1

# A fresh eval-only run reads the immutable recreation candidate from one namespace and
# writes its new metrics/eval artifacts to another.  Historical recreation+eval and
# same-prefix resume jobs do not pass a restore prefix, so they retain the old layout.
normalize_artifact_prefix() {
    PYTHONPATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" \
        python3 -m infrastructure.artifacts.cli normalize-key "$1"
}

RB_ARTIFACT_PREFIX="$(normalize_artifact_prefix "$RB_ARTIFACT_PREFIX")" || exit "$RC_INFRA"
RB_ARTIFACT_RESTORE_PREFIX="$(normalize_artifact_prefix "${RB_ARTIFACT_RESTORE_PREFIX:-$RB_ARTIFACT_PREFIX}")" || exit "$RC_INFRA"
export RB_ARTIFACT_BACKEND RB_ARTIFACT_DRIVER RB_ARTIFACT_ROOT
export RB_ARTIFACT_PREFIX RB_ARTIFACT_RESTORE_PREFIX

# Model routing is prepared by the deployment adapter before this worker starts.
# The runtime consumes only the generic endpoint contract and does not coordinate
# provider containers through shared files.

# The deployment adapter prepares the pod image and emulator target before it
# enters RB. The benchmark's pinned Android/Gradle toolchain is prepared later
# by scripts/android/stages/bootstrap.sh; this worker never installs pod tools.

# ── 基础上下文 ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${OUTPUT_DIR:-/workspace/output}"
RB_SHARED_DIR="${RB_SHARED_DIR:-${SHARED_DIR:-/workspace/shared}}"
export RB_SHARED_DIR
mkdir -p "$RB_SHARED_DIR"
INSTANCE_ID="${INSTANCE_ID:-${TASK_ID:-}}"
TASK_ID="${TASK_ID:-${INSTANCE_ID}}"
SOURCE_TASK_ID="${SOURCE_TASK_ID:-$TASK_ID}"
PASS_ID="${PASS_ID:-}"
STAGE="${STAGE:-recreation_eval}"
RB_EVAL_TARGET="${RB_EVAL_TARGET:-${EVAL_TARGET:-recreation}}"
export RB_EVAL_TARGET
if [ "$_RB_ARTIFACT_RESTORE_PREFIX_EXPLICIT" = 1 ] && [ "$STAGE" = eval ] && \
        [ "$RB_ARTIFACT_RESTORE_PREFIX" = "$RB_ARTIFACT_PREFIX" ]; then
    echo "ERROR: eval-only restore and output prefixes must differ" >&2
    exit "$RC_INFRA"
fi

# Release execution is recreation -> eval. setup is a standalone permission diagnostic.
normalize_stage_list() {
    local raw="${1,,}"
    raw="${raw// /}"
    case "$raw" in
        setup|recreation|eval) printf '%s' "$raw" ;;
        recreation_eval) printf '%s' "recreation,eval" ;;
        *)
            echo "ERROR: invalid STAGE='$1'" >&2
            echo "       want: setup | recreation | eval | recreation_eval" >&2
            return 1
            ;;
    esac
}

STAGE_LIST="$(normalize_stage_list "$STAGE")" || exit "$RC_INFRA"
RB_NEEDS_MODEL=0
case ",$STAGE_LIST," in *,recreation,*) RB_NEEDS_MODEL=1 ;; esac
case "$RB_EVAL_TARGET" in
    recreation|reference) ;;
    *) echo "ERROR: invalid RB_EVAL_TARGET='$RB_EVAL_TARGET'" >&2; exit "$RC_INFRA" ;;
esac
if [ "$RB_EVAL_TARGET" = reference ] && [ "$STAGE" != eval ]; then
    echo "ERROR: RB_EVAL_TARGET=reference requires STAGE=eval" >&2
    exit "$RC_INFRA"
fi
# MUST be exported: write_metrics decides "did this job grade anything?" from
# STAGE_LIST. Unexported it reads as empty, and then EVERY job reports
# eval_status=not_evaluated — worse than the raw-STAGE bug that motivated the change.
export STAGE_LIST

# ── Exit-code contract ────────────────────────────────────────────────────────
# The batch runner's only real question is "is retrying this worth anything?", and
# a single collapsed code cannot answer it. Four codes, and nothing else:
#
#   0   RC_OK      the pipeline ran to completion, including a low-scoring recreation.
#   1   RC_DATA    the frozen unified input is absent or invalid.
#                  Retrying runs the identical thing again. DO NOT retry.
#   2   RC_INFRA   a problem with our environment. Bootstrap download, artifact auth, the
#                  emulator never booting, a sidecar never becoming ready, a missing
#                  required param, a corrupt asset payload. Retrying may well work.
#   143 RC_TIMEOUT deployment platform hit runtime_timeout_sec and sent SIGTERM.
#
# Per-stage detail goes in metrics.json's `stages` map, not into the exit code.
# Shared truthiness test. Defined up here because the model-routing and
# compaction gates below both call it, long before the old definition site.
# This worker is shipped inside the immutable RecreationBench tree. Resolve shared
# modules locally; fetching benchmark code is the external launcher's responsibility.
_RB_CORE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

is_truthy() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

rb_scripts_dir() {
    [ -f "${_RB_CORE_ROOT}/core/agent_config.py" ] || return 1
    printf '%s' "${_RB_CORE_ROOT}"
}

# scripts/common — the other convention. Kept as a wrapper so both sets of callers are
# untouched by the merge.
ensure_rb_core() {
    local s; s="$(rb_scripts_dir)" || return 1
    [ -n "${s}" ] || return 1
    printf '%s/common' "${s}"
}

# The repo ROOT (parent of scripts/). macOS runs the controller from here and rsyncs the tree to
# the VM, so it needs this rather than the sys.path root -- the third of three conventions, all
# derived from one resolution instead of three hand-rolled finds.
rb_pipeline_dir() {
    local s; s="$(rb_scripts_dir)" || return 1
    [ -n "${s}" ] || return 1
    printf '%s' "$(dirname "${s}")"
}

# The provider has already prepared the pod. Run the benchmark worker directly;
# it must never call deployment adapter or re-enter the shared RB controller.
# Control flow is separate from process status: a legitimate model-zero result
# stops the stage sequence without inventing a fifth numeric exit code.
PIPELINE_STOP=0

# Per-stage outcome, surfaced in metrics.json.
#   ok | reused | failed | skipped
declare -A STAGE_STATUS=()
stage_status_set() { STAGE_STATUS["$1"]="$2"; }
stage_status_json() {
    local out="" s
    for s in setup recreation eval; do
        [ -n "${STAGE_STATUS[$s]:-}" ] || continue
        out="${out:+${out},}\"${s}\":\"${STAGE_STATUS[$s]}\""
    done
    printf '{%s}' "$out"
}

# Fail with a classified code, still leaving a metrics.json behind. Guarded by
# `declare -F` because bootstrap failures happen before write_metrics is defined,
# and an infra death that writes nothing at all is the hardest kind to diagnose.
die() {
    local rc="${1:-$RC_INFRA}"; shift || true
    echo "" >&2
    echo "FATAL(exit=$rc): $*" >&2
    if declare -F write_metrics >/dev/null 2>&1; then
        set +e
        declare -F collect_artifacts >/dev/null 2>&1 && collect_artifacts >/dev/null 2>&1
        if [ "$rc" = "$RC_DATA" ]; then
            write_metrics "$rc" dataset_error || true
        else
            write_metrics "$rc" pipeline_error || true
        fi
    fi
    exit "$rc"
}

# ── versioned artifact prefixes ───────────────────────────────────────────
AAR_VERSION="${VERSION:-${AAR_VERSION:-v0.1.0}}"
RB_ARTIFACT_RUN_PREFIX="${RB_ARTIFACT_PREFIX%/}/${AAR_VERSION}/${SOURCE_TASK_ID}"
RB_ARTIFACT_RESTORE_RUN_PREFIX="${RB_ARTIFACT_RESTORE_PREFIX%/}/${AAR_VERSION}/${SOURCE_TASK_ID}"
RECREATION_STAGE="${RECREATION_STAGE:-recreation}"
# EVAL_STAGE：eval 产物的 artifact store 子目录名。必须随 RECREATION_STAGE 区分，
# 否则 skill/noskill（或不同 model）的 eval 产物会写同一 eval/ 目录互相覆盖。
#   - RECREATION_STAGE=recreation（默认/legacy） -> eval（保持向后兼容）
#   - 其它（如 recreation-opus-4-6 / recreation-noskill-opus-4-6）
#       -> eval-recreation-opus-4-6 / eval-recreation-noskill-opus-4-6
# 可用 EVAL_STAGE 环境变量显式覆盖。
if [ -z "${EVAL_STAGE:-}" ]; then
    if [ "$RECREATION_STAGE" = "recreation" ]; then
        EVAL_STAGE="eval"
    else
        EVAL_STAGE="eval-${RECREATION_STAGE}"
    fi
fi
RB_ARTIFACT_UPLOAD="${RB_ARTIFACT_UPLOAD:-auto}"
# mobile-mcp coordinate space: 0=absolute, 1=relative.
export MOBILE_MCP_COORDINATE_SPACE="${MOBILE_MCP_COORDINATE_SPACE:-${mobile_mcp_coordinate_space:-0}}"

# mobile-mcp is always the pinned published package, installed into a trusted path before the
# agent starts. The old
# artifact store-tarball path (mcp_local_source=1) existed for a pre-release build and only
# added a way for the pod to disagree with itself about which MCP it was running.
MOBILE_MCP_PACKAGE="${MOBILE_MCP_PACKAGE:-@qwen-code/mobile-mcp@0.1.5}"

# ── Unified release instance (required) ──────────────────────────────────────
# A unified instance carries the whole task in ONE self-describing prefix:
#   <RB_UNIFIED_PREFIX>/<platform>/<app>/{instance.json,reference/,tests/,vlm_assertions.json}
RB_UNIFIED_PREFIX="${RB_UNIFIED_PREFIX:-}"
RB_UNIFIED_PLATFORM="${RB_UNIFIED_PLATFORM:-android}"
RB_UNIFIED_INSTANCE_PATH=""
RB_UNIFIED_REFERENCE_PATH=""
RB_UNIFIED_TESTS_PATH=""
RB_UNIFIED_VLM_PATH=""

# rb core, fetched ONCE and reused: the unified key resolver, shared session
# collector, and RB CLI all live in it. Echoes the PYTHONPATH root, or nothing
# when the artifact is unavailable (a run without RB_PIPELINE_COMMIT).
# rb's scripts/ dir (parent of scripts/common), i.e. what PYTHONPATH needs for `-m core.*`
# and for android's own tested libs under scripts/android/lib.

# Recreation agent CLI: claude (default) or codex. The deployment adapter supplies
# one ready endpoint and keeps its upstream routing credentials outside the agent.


if [ "$STAGE_LIST" != "setup" ]; then
    [ -n "$RB_UNIFIED_PREFIX" ] || die "$RC_INFRA" "rb_unified_prefix is required for release execution"
    _rb_common="$(ensure_rb_core || true)"
    [ -n "${_rb_common}" ] || die "$RC_INFRA" "rb core unavailable; cannot resolve unified instance"
    _rb_scripts="$(rb_scripts_dir || true)"
    [ -n "${_rb_scripts}" ] || die "$RC_INFRA" "rb scripts unavailable; cannot configure artifact transport"
    _resolve_rc=0
    _RB_UNI_RESOLVED="$(PYTHONPATH="${_rb_scripts}:${_rb_common}" python3 -m rb_unify.eval_bridge resolve --probe \
            --task-id "${INSTANCE_ID}" --prefix "${RB_UNIFIED_PREFIX%/}" \
            --platform "${RB_UNIFIED_PLATFORM}" \
            --override "${RB_UNIFIED_APP:-}" \
            2>/dev/null)" || _resolve_rc=$?
    [ "$_resolve_rc" -eq "$RC_OK" ] || die "$RC_INFRA" "failed to resolve unified Android instance for ${INSTANCE_ID}"
    [ -n "${_RB_UNI_RESOLVED}" ] || die "$RC_DATA" "no unified Android instance for ${INSTANCE_ID}"
    _RB_UNI_BASE="${_RB_UNI_RESOLVED%/}"
    RB_UNIFIED_INSTANCE_PATH="${_RB_UNI_BASE}/instance.json"
    RB_UNIFIED_REFERENCE_PATH="${_RB_UNI_BASE}/reference"
    RB_UNIFIED_TESTS_PATH="${_RB_UNI_BASE}/tests"
    RB_UNIFIED_VLM_PATH="${_RB_UNI_BASE}/vlm_assertions.json"
    echo "[unified] resolved key: ${_RB_UNI_RESOLVED}"
    echo "[unified] reference <- ${RB_UNIFIED_REFERENCE_PATH}"
    echo "[unified] tests     <- ${RB_UNIFIED_TESTS_PATH}"
fi
export RB_UNIFIED_PREFIX RB_UNIFIED_PLATFORM RB_UNIFIED_INSTANCE_PATH
export RB_UNIFIED_REFERENCE_PATH RB_UNIFIED_TESTS_PATH RB_UNIFIED_VLM_PATH
echo "=== Artifact backend: ${RB_ARTIFACT_BACKEND:-auto}"

# ── model endpoint / evaluation credentials ───────────────────────────────
MODEL="${MODEL:-}"
MODEL_BASE_URL="${MODEL_BASE_URL:-}"
MODEL_API_KEY="${MODEL_API_KEY:-}"
if [ "$RB_NEEDS_MODEL" = 1 ]; then
    [ -n "${RB_AGENT_BASE_URL:-}" ] || die "$RC_INFRA" "RB_AGENT_BASE_URL is required for recreation"
    [ -n "${RB_AGENT_API_KEY:-}" ] || die "$RC_INFRA" "RB_AGENT_API_KEY is required for recreation"
fi
# ── Model naming: two names for one model, and one flag to relate them ────────
# MODEL        the deployment routing name. May carry a provider prefix.
# CLAUDE_MODEL what Claude Code itself sees via --model / ANTHROPIC_*_MODEL.
#
# Claude Code decides the context window from the model NAME: a plain name means
# 200K, and only a `[1m]` suffix unlocks the 1M window (it makes CC send the
# context-1m beta header). It also does not recognise provider-prefixed names like
# `mr.aws.claude-opus-4-8` and silently falls back to 200K.
#
# So the suffix is derived from a boolean here rather than hand-written into a
# param. Writing `claude_model: claude-opus-4-8[1m]` by hand was the old way, and it
# put a Claude-Code-only token into a field that also looked like a routing name —
# easy to copy into `model` by accident, where it reaches the upstream as an unknown
# model. The submitter strips a provider prefix into claude_model; context_1m chooses
# the window.
CONTEXT_1M="${CONTEXT_1M:-${context_1m:-false}}"
RECREATION_MODEL="${RECREATION_MODEL:-$MODEL}"
CLAUDE_MODEL="${CLAUDE_MODEL:-${claude_model:-claude-opus-4-8}}"
if is_truthy "${CONTEXT_1M}"; then
    case "$CLAUDE_MODEL" in
        *"[1m]") ;;
        *) CLAUDE_MODEL="${CLAUDE_MODEL}[1m]" ;;
    esac
fi
RECREATION_CLAUDE_MODEL="$CLAUDE_MODEL"
echo "==> model routing: upstream='${MODEL}' claude-code='${CLAUDE_MODEL}' context_1m=${CONTEXT_1M}"
# 编排层以**大写** env 注入 job param（证据：入口只读 ${VERSION}，产物确实落到
# 传入的 version 目录）。这里同时挂上小写形式，与上面 CLAUDE_MODEL 等行的惯例一致。
# One canonical judge contract across all five templates.
VLM_JUDGE_API_KEY="${VLM_JUDGE_API_KEY:-${vlm_judge_api_key:-$MODEL_API_KEY}}"
VLM_JUDGE_MODEL="${VLM_JUDGE_MODEL:-${vlm_judge_model:-}}"
# The deployment boundary supplies the model and OpenAI-compatible endpoint.
VLM_JUDGE_BASE_URL="${VLM_JUDGE_BASE_URL:-${vlm_judge_base_url:-}}"
case "$STAGE" in
    eval|recreation_eval)
        [ -n "$VLM_JUDGE_MODEL" ] || die "$RC_INFRA" "VLM_JUDGE_MODEL is required for evaluation"
        [ -n "$VLM_JUDGE_BASE_URL" ] || die "$RC_INFRA" "VLM_JUDGE_BASE_URL is required for evaluation"
        ;;
esac

RECREATION_TIMEOUT="${RECREATION_TIMEOUT:-72000}" # 20h

# ── prepared target / model endpoint ──────────────────────────────────────
# deployment adapter waits for the sidecar and passes the exact serial through TargetSpec.
DEVICE_ID="${DEVICE_ID:?prepared Android DEVICE_ID is required}"

# ── 目录约定 ──────────────────────────────────────────────────────────────
WORK="${WORK:-/workspace}"
REFERENCE_DIR="$WORK/reference"
TESTS_DIR="$WORK/tests"
# The stage owns durable logs/artifacts under stages/recreation. The agent itself always works at
# /workspace/recreation, which the RB stage binds to REC_DIR/recreation.
REC_DIR="$WORK/stages/recreation"
EVAL_DIR="$WORK/eval"
# The pinned RB artifact installs its Android prompt/skill/verifier here.
APP_DIR="${APP_DIR:-$WORK/app_assets}"

# The deployment endpoint is the only model route exposed to the benchmark runtime.
export ANTHROPIC_AUTH_TOKEN="${RB_AGENT_API_KEY:-}"
export ANTHROPIC_MODEL="${CLAUDE_MODEL}"
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-${CLAUDE_MODEL}}"
# Claude Code single-response output-token limit. Keep one release-facing token
# limit: deployment platform injects max_tokens_limit as MAX_TOKENS_LIMIT for the proxy, and the
# CLI uses that same canonical value for its context reservation.
# ⚠️ 该值会从模型 contextWindow 中预留，预留过大 → 有效 compact 窗口被压缩 → auto-compact 抖动
# (rapid_refill_breaker)。小上下文/小输出模型（如 glm-5.2 真实 maxOutputTokens=32000）应下调到其
# 真实单次输出上限，避免 128000 从 200K 窗口里白白吃掉 128K。
# 数值受上游模型/网关单次输出硬上限约束，若网关拒绝再下调。
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-${MAX_TOKENS_LIMIT:-${max_tokens_limit:-128000}}}"
echo "==> CLAUDE_CODE_MAX_OUTPUT_TOKENS=${CLAUDE_CODE_MAX_OUTPUT_TOKENS}"

# Bound every MCP tool call. Unset means UNBOUNDED on this claude generation (verified on
# 2.1.237: a 600s stub-server hang was never cut off), and mobile-mcp reaches the emulator
# over adb, which wedges. A timeout is recoverable -- the agent is told the call timed out
# and continues -- whereas a hang consumes the whole stage budget. Shared default lives in
# scripts/core/mcp_settings.py (rb); RB_MCP_TOOL_TIMEOUT retunes a live run.
export MCP_TOOL_TIMEOUT="${RB_MCP_TOOL_TIMEOUT:-180000}"
echo "==> MCP_TOOL_TIMEOUT=${MCP_TOOL_TIMEOUT}"
export API_TIMEOUT_MS="${RB_API_TIMEOUT_MS:-1800000}"
# Context selection and compaction policy are independent. CONTEXT_1M controls
# only the model's [1m] suffix; an explicitly submitted compact window must also
# reach ordinary-context runs (for example the shared 262144-token policy).
_COMPACT_WINDOW="${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-${AUTO_COMPACT_WINDOW:-}}"
if [ -n "${_COMPACT_WINDOW}" ]; then
    export CLAUDE_CODE_AUTO_COMPACT_WINDOW="${_COMPACT_WINDOW}"
else
    unset CLAUDE_CODE_AUTO_COMPACT_WINDOW
fi

# Do not invent a percentage override. With no explicit override Claude Code
# uses its own default (currently 80%), which keeps policy consistent across
# platforms and CLI versions.
_COMPACT_PCT="${CLAUDE_AUTOCOMPACT_PCT_OVERRIDE:-${claude_autocompact_pct_override:-}}"
if [ -n "${_COMPACT_PCT}" ]; then
    export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="${_COMPACT_PCT}"
else
    unset CLAUDE_AUTOCOMPACT_PCT_OVERRIDE
fi
echo "==> compaction: window=${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-Claude default}, pct=${CLAUDE_AUTOCOMPACT_PCT_OVERRIDE:-Claude default (80%)}, context_1m=${CONTEXT_1M}"
# ── thinking effort: one tier, two channels ──────────────────────────────────
CC_THINK_EFFORT="${THINKING_EFFORT:-max}"
case "$CC_THINK_EFFORT" in
    low|medium|high|xhigh|max) ;;
    # Reject unknown tiers here so the deployment cannot silently select another value.
    *) die "$RC_INFRA" "thinking_effort='$CC_THINK_EFFORT' is not one of low/medium/high/xhigh/max" ;;
esac
CC_EFFORT_ARGS=(--effort "$CC_THINK_EFFORT")
echo "==> effort: ${CC_THINK_EFFORT}"
# 冻结评测脚本通过通用 VLM 环境变量调用共享判分实现。
export VLM_API_KEY="${VLM_JUDGE_API_KEY}"
export JUDGE_MODEL="${VLM_JUDGE_MODEL}"
export VLM_BASE_URL="${VLM_JUDGE_BASE_URL}"

export OUTPUT_DIR INSTANCE_ID TASK_ID SOURCE_TASK_ID PASS_ID STAGE
export AAR_VERSION RB_ARTIFACT_PREFIX RB_ARTIFACT_RESTORE_PREFIX RB_ARTIFACT_RUN_PREFIX RB_ARTIFACT_RESTORE_RUN_PREFIX RECREATION_STAGE EVAL_STAGE
export RB_ARTIFACT_UPLOAD
export MODEL MODEL_BASE_URL MODEL_API_KEY RECREATION_MODEL CLAUDE_MODEL RECREATION_CLAUDE_MODEL
export APP_DIR WORK REFERENCE_DIR TESTS_DIR REC_DIR EVAL_DIR DEVICE_ID
export RB_ARTIFACT_BUCKET RB_ARTIFACT_ENDPOINT RB_ARTIFACT_ACCESS_KEY_ID RB_ARTIFACT_ACCESS_KEY_SECRET
export RB_ARTIFACT_THIRD_PARTY_PREFIX

# The deployment adapter supplies the endpoint the agent can reach. Keep the
# historical loopback route as a compatibility fallback for older adapters.
: "${RB_AGENT_BASE_URL:?deployment adapter must provide RB_AGENT_BASE_URL}"
export ANTHROPIC_BASE_URL="$RB_AGENT_BASE_URL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-$CLAUDE_MODEL}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-$CLAUDE_MODEL}"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-$CLAUDE_MODEL}"
export ANTHROPIC_API_KEY="${RB_AGENT_API_KEY:-${ANTHROPIC_API_KEY:-$MODEL_API_KEY}}"
export ANTHROPIC_AUTH_TOKEN="${RB_AGENT_API_KEY:-${ANTHROPIC_AUTH_TOKEN:-$MODEL_API_KEY}}"

mkdir -p "$OUTPUT_DIR" "$REFERENCE_DIR" "$TESTS_DIR" "$REC_DIR" "$EVAL_DIR"

# ── 「本 job 真跑过 agent 的阶段」标记 ─────────────────────────────────────
# trajectory surface 与 metrics 的 passed 判定都需要区分「这一轮真跑了 agent」和
# 「产物是从 artifact store 复用下载来的」。仅看 trajectory.jsonl 是否存在无法区分：复用路径
# A restored recreation may carry a prior trajectory, so presence alone is not enough.
# 标记目录刻意放在 $WORK 下、不在任何 stage 目录内 —— upload_stage_outputs 整目录
# 上传 stage 目录，放里面会被传到 artifact store 再在复用时下载回来，反过来污染判据。
AGENT_RUNS_DIR="${AGENT_RUNS_DIR:-$WORK/.agent_runs}"
mkdir -p "$AGENT_RUNS_DIR"
export AGENT_RUNS_DIR

mark_agent_ran() { touch "$AGENT_RUNS_DIR/$1" 2>/dev/null || true; }
agent_ran() { [ -e "$AGENT_RUNS_DIR/$1" ]; }

# (shared env file already written at script start - see EARLY block above)

# 强制重跑指定阶段：逗号分隔的阶段名（recreation,eval）或 all；
# 命中的阶段跳过本地/artifact store 一切复用，强制重新执行。空=不强制。
FORCE_STAGE="${FORCE_STAGE:-}"



# 判断某阶段是否被要求强制重跑。
force_stage_enabled() {
    local stage="$1"
    local fs=",${FORCE_STAGE,,},"
    fs="${fs// /}"
    case "$fs" in
        *,all,*)        return 0 ;;
        *",${stage},"*) return 0 ;;
    esac
    return 1
}

# ── Claude Code version pin ────────────────────────────────────────────────────
# One scaffold version across all five platforms, so a score is attributable to the
# agent that produced it. Before this, only web pinned anything (2.1.183); windows ran
# `npm install -g @anthropic-ai/claude-code` unpinned — i.e. whatever `latest` was that
# day — and linux/macOS/android silently used whatever their image happened to bake.
# Two runs a week apart could differ in the agent and nothing recorded it.
#
# RECORD always and CONVERGE when the version differs. A different working CLI is
# still a different benchmark environment, so failure to reach the pin is fatal.
if [ "$RB_NEEDS_MODEL" = 1 ]; then
    if [ "$AGENT_CLI" = "claude" ]; then
        CLAUDE_CODE_PIN="$SCAFFOLD_VERSION"
    else
        # The Android image still carries Claude Code for shared diagnostics, but the selected
        # Codex pin must not be interpreted as a Claude package version.
        CLAUDE_CODE_PIN="2.1.177"
    fi
    _cc_ver() { claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1; }
    _cc_have="$(_cc_ver)"
    if [ "${_cc_have}" != "${CLAUDE_CODE_PIN}" ]; then
        echo "==> claude-code ${_cc_have:-<none>} != pin ${CLAUDE_CODE_PIN}; installing the pin"
        npm install -g --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_PIN}" \
            >/dev/null 2>&1 || die "$RC_INFRA" "failed to install claude-code@${CLAUDE_CODE_PIN}"
        _cc_have="$(_cc_ver)"
    fi
    [ "$_cc_have" = "$CLAUDE_CODE_PIN" ] || \
        die "$RC_INFRA" "claude-code version ${_cc_have:-missing}; expected ${CLAUDE_CODE_PIN}"
    export CLAUDE_CODE_VERSION="${_cc_have:-unknown}"
    echo "==> claude-code: ${CLAUDE_CODE_VERSION} (pin ${CLAUDE_CODE_PIN})"
fi

echo "=== Android App Recreation Config ==="
echo "INSTANCE_ID:     ${INSTANCE_ID}"
echo "STAGE:           ${STAGE}  ->  [${STAGE_LIST}]"
echo "FORCE_STAGE:     ${FORCE_STAGE:-<none>}"
echo "MODEL:           ${MODEL}"
echo "MODEL_BASE_URL:  ${MODEL_BASE_URL}"
echo "RB_ARTIFACT_RUN_PREFIX: ${RB_ARTIFACT_RUN_PREFIX}"
echo "RB_ARTIFACT_RESTORE_RUN_PREFIX: ${RB_ARTIFACT_RESTORE_RUN_PREFIX}"
echo "APP_DIR:         ${APP_DIR}"
echo "OUTPUT_DIR:      ${OUTPUT_DIR}"
echo "DEVICE_ID:       ${DEVICE_ID}"
echo "====================================="
echo ""

# 替代 `set -x` 的可观测手段：完整 env 快照，但对凭证类变量做脱敏。
# 对齐 recreation-bench-linux main.sh 的做法。
dump_env_masked() {
    echo "======== Environment (masked) ========"
    env | sort | sed -E \
        -e '/(KEY|SECRET|PASSWORD|TOKEN|PASS|PRIVATE|AUTH|CREDENTIAL|_SK|_AK|LITELLM_MODELS|EXTRA_ENVS)/ s/=.*/=******/'
    echo "======================================"
}
dump_env_masked

# ── stdout 日志脱敏过滤器 ──
# Claude Code stream-json 输出含 base64 截图，会导致 deployment platform job logs 膨胀。
# 此过滤器仅作用于 stdout（deployment platform 可见日志），trajectory.jsonl 保留完整原始数据。

# 收集 Claude Code 原生 session JSONL（须未禁用 session 持久化）。
# 仅复制 since_ts(秒) 之后修改的 *.jsonl，避免混入其它阶段的 session；
# 排除 checkpoint / trajectory 派生文件。落到 dest_dir 供 artifact_upload_dir 整目录上传。

# ==========================================================================
# 环境就绪：emulator + model endpoint
# ==========================================================================


artifact_backend_available() {
    artifact_cli configured >/dev/null 2>&1
}

# Provider SDK setup is isolated behind the artifact adapter. Filesystem runs do
# not install or import a cloud SDK.
ensure_artifact_backend() {
    local scripts
    scripts="$(rb_scripts_dir || true)"
    [ -n "$scripts" ] || return 1
    PYTHONPATH="${scripts}${PYTHONPATH:+:${PYTHONPATH}}" \
        python3 -m infrastructure.artifacts.provision
}

# 统一 artifact 传输 helper。SDK、重试和目录遍历属于 infrastructure adapter；
# worker 只保留 stage 生命周期与 provider-neutral artifact key layout。
# 用法：
#   artifact_xfer dl_file  key         /local/file
#   artifact_xfer dl_dir   prefix      /local/dir
#   artifact_xfer ul_file  /local/file key
#   artifact_xfer ul_dir   /local/dir  prefix
artifact_cli() {
    local scripts
    scripts="$(rb_scripts_dir || true)"
    [ -n "$scripts" ] || {
        echo "[artifact] ERROR: rb scripts unavailable" >&2
        return 1
    }
    PYTHONPATH="${scripts}${PYTHONPATH:+:${PYTHONPATH}}" \
        python3 -m infrastructure.artifacts.cli "$@"
}

artifact_xfer() {
    ensure_artifact_backend || return 1
    artifact_cli xfer "$1" "$2" "$3"
}

# Make a Claude Code run's termination debuggable. Without this the pipeline
# `timeout T claude ... | tee | _b64_filter` swallows claude's real exit code, so a
# wall-clock kill (124), a hard crash, and the agent quietly ending its own turn all
# looked identical — "no APK produced" with no clue why.
# Args: <label> <trajectory.jsonl> <claude_rc> <elapsed_s> <timeout_s>
#   claude_rc is the FIRST pipeline stage's status (124 => our `timeout` fired).
# The last type=result record CC emits carries the true reason: an end_turn with
# subtype=success means the model STOPPED ON ITS OWN (CC ends the loop on any turn
# with no tool call) — not a timeout, not a crash.

# 解析 aapt 可执行路径：优先 PATH，其次从 ANDROID_HOME/ANDROID_SDK_ROOT
# 的 build-tools/*/aapt(aapt2) 自动发现。结果缓存到 _AAPT_BIN。

# 用 aapt 解析 APK 包名，带诊断输出。成功时 stdout 输出包名。

# APK 解析失败时打印充分诊断信息，便于定位 clean.apk 究竟是什么。

# 安装一个 Claude skill（src=.md，dest=~/.claude/skills/<name>/SKILL.md）

# 从资产包读取某阶段的 Claude prompt：${APP_DIR}/<stage>/cc_prompt.txt。
#
# The three agent prompts used to be heredocs in this file. They now live in the pinned RB
# artifact, so a prompt change and its pipeline code always reach the pod at the same commit.
#
# Placeholders are substituted from an EXPLICIT allowlist by Python, not by `eval`
# or `envsubst`: these files are prompt text that models read and humans rewrite
# freely, and eval'ing them would execute whatever `$(...)` ended up inside.
# An unknown ${...} is left verbatim rather than blanked, so a typo is visible in
# prompt.txt instead of silently deleting an instruction.

# ==========================================================================
# Artifact upload/download for split-stage execution. Keys preserve the historical
# <result-root>/<version>/<task>/<stage>/... layout.
# ==========================================================================
artifact_env_available() { artifact_backend_available; }

artifact_upload_enabled() {
    case "${RB_ARTIFACT_UPLOAD:-auto}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        0|false|FALSE|no|NO|off|OFF) return 1 ;;
        auto|"") artifact_env_available ;;
        *) echo "ERROR: invalid RB_ARTIFACT_UPLOAD=$RB_ARTIFACT_UPLOAD"; return 2 ;;
    esac
}

require_artifact_backend() {
    if artifact_env_available; then
        return 0
    fi
    echo "ERROR: an artifact backend is required for split-stage dependencies/uploads"
    echo "       Configure RB_ARTIFACT_BACKEND and its backend-specific settings"
    return 1
}

_artifact_stage_key() {
    printf '%s/%s' "$RB_ARTIFACT_RUN_PREFIX" "$1"
}

_artifact_restore_stage_key() {
    printf '%s/%s' "$RB_ARTIFACT_RESTORE_RUN_PREFIX" "$1"
}

artifact_upload_dir() {
    local local_dir="$1"
    local artifact_stage="$2"
    [ ! -d "$local_dir" ] && { echo "[artifact] ERROR: $local_dir not found"; return 1; }
    require_artifact_backend
    local key; key="$(_artifact_stage_key "$artifact_stage")"
    echo "[artifact] Uploading $local_dir -> ${key}/"
    artifact_xfer ul_dir "$local_dir" "$key"
}

# 即时上传单个文件到 {prefix}/{stage}/{文件名}。
# 与 verify 解耦：产物一生成就调，即使后续阶段失败也能保住。
# 受 RB_ARTIFACT_UPLOAD 开关控制；未启用/缺配置时静默跳过，不阻断流程。
artifact_upload_file() {
    local local_file="$1"
    local artifact_stage="$2"
    local remote_name="${3:-$(basename "$local_file")}"
    [ -s "$local_file" ] || { echo "[artifact] skip upload (missing/empty): $local_file"; return 0; }
    local enabled_status=0
    artifact_upload_enabled || enabled_status=$?
    if [ "$enabled_status" -ne 0 ]; then
        echo "[artifact] upload disabled or backend unavailable; skip instant upload of $remote_name"
        return 0
    fi
    local key; key="$(_artifact_stage_key "$artifact_stage")/$remote_name"
    echo "[artifact] Instant upload $local_file -> $key"
    artifact_xfer ul_file "$local_file" "$key" || \
        echo "[artifact] WARN: instant upload failed for $remote_name (non-fatal)"
}

artifact_download_dir() {
    local artifact_stage="$1"
    local local_dir="$2"
    require_artifact_backend
    local key; key="$(_artifact_restore_stage_key "$artifact_stage")"
    echo "[artifact] Downloading ${key}/ -> $local_dir"
    mkdir -p "$local_dir"
    artifact_xfer dl_dir "$key" "$local_dir"
}

# 共享的存在性探测：mode=prefix 扫前缀下是否有对象；mode=key 精确查找一个对象。
# stdout: "exists" | "missing"，exit code: 0=exists, 1=missing/error
_artifact_exists() {
    require_artifact_backend
    ensure_artifact_backend
    artifact_cli exists "$1" "$2" 2>/dev/null
}

# Check one concrete artifact before restoring a result directory.
# 用于在下载整个 stage 目录前，先廉价确认关键产物（如 recreated.apk）存在，
# 避免为中断/失败但已上传了日志中间产物的 pod 白下载再校验失败。
artifact_object_exists() {
    _artifact_exists key "$1"
}


# 下载某 stage 所需的上游产物


# Model-gateway request evidence is copied into the canonical proxy_logs artifact
# directory by the shared stage library.

# 回传某 stage 的产物到 artifact store


# ==========================================================================
# Artifacts & Metrics
# ==========================================================================
# ── 把 agent 轨迹提到 $OUTPUT_DIR/trajectory.jsonl ────────────────────────
# deployment platform reads the recreation trajectory from the artifact root.
surface_trajectory() {
    local dest="$OUTPUT_DIR/trajectory.jsonl"
    mkdir -p "$OUTPUT_DIR" 2>/dev/null || true
    if agent_ran recreation && [ -s "$REC_DIR/trajectory.jsonl" ]; then
        cp -f "$REC_DIR/trajectory.jsonl" "$dest" 2>/dev/null || return 0
        printf '%s' recreation > "$OUTPUT_DIR/.trajectory_stage" 2>/dev/null || true
        return 0
    fi

    echo "[trajectory] no agent stage ran in this job; trying the recreation artifact"
    if ! artifact_env_available; then
        echo "[trajectory] artifact backend unavailable; nothing to surface"
        return 0
    fi
    local key="$(_artifact_restore_stage_key "$RECREATION_STAGE")/trajectory.jsonl"
    if artifact_xfer dl_file "$key" "$dest" >/dev/null 2>&1 && [ -s "$dest" ]; then
        echo "[trajectory] surfaced reused recreation from ${key} -> ${dest} ($(wc -c <"$dest" 2>/dev/null || echo 0) bytes)"
        printf '%s' "recreation:reused" > "$OUTPUT_DIR/.trajectory_stage" 2>/dev/null || true
        return 0
    fi
    rm -f "$dest" 2>/dev/null || true
    echo "[trajectory] WARN: no trajectory at ${key}; nothing to surface"
    return 0
}

# Expose native Claude/Codex transcripts at the same deployment platform artifact path used by every platform.
# REC_DIR itself is still uploaded wholesale; this is an additive deployment platform-facing surface.
surface_sessions() {
    local dest="$OUTPUT_DIR/sessions"
    mkdir -p "$dest" 2>/dev/null || return 0
    if [ -d "$REC_DIR/sessions" ]; then
        cp -a "$REC_DIR/sessions/." "$dest/" 2>/dev/null || true
        echo "[trajectory] surfaced sessions from local recreation stage -> ${dest}"
        return 0
    fi
    if ! artifact_env_available; then
        echo "[trajectory] no local sessions and artifact backend unavailable"
        return 0
    fi
    local key="$(_artifact_restore_stage_key "$RECREATION_STAGE")/sessions"
    if artifact_xfer dl_dir "$key" "$dest" >/dev/null 2>&1; then
        echo "[trajectory] surfaced reused sessions from ${key}/ -> ${dest}"
    else
        echo "[trajectory] WARN: no sessions at ${key}/"
    fi
}

collect_artifacts() {
    echo ""
    echo "=========================================="
    echo "=== Collecting Artifacts"
    echo "=========================================="

    mkdir -p "$OUTPUT_DIR"/{recreation,eval}

    # Recreation artifacts
    cp "$REC_DIR/recreation/recreated.apk" "$OUTPUT_DIR/recreation/" 2>/dev/null || true
    cp "$REC_DIR/prompt.txt"               "$OUTPUT_DIR/recreation/" 2>/dev/null || true
    cp "$REC_DIR/recreate.log"             "$OUTPUT_DIR/recreation/" 2>/dev/null || true
    if [ -d "$REC_DIR/tool_use_screenshots" ]; then
        cp -a "$REC_DIR/tool_use_screenshots" "$OUTPUT_DIR/" 2>/dev/null || true
    fi

    # Eval artifacts
    cp "$EVAL_DIR/metrics.json"  "$OUTPUT_DIR/eval/" 2>/dev/null || true
    cp "$EVAL_DIR/eval.log"      "$OUTPUT_DIR/eval/" 2>/dev/null || true
    cp -r "$EVAL_DIR/eval_results" "$OUTPUT_DIR/eval/" 2>/dev/null || true

    # 轨迹可视化：必须在 write_metrics 之前，后者要读它判 passed
    surface_trajectory || true
    surface_sessions || true

    echo "Artifacts: $(find "$OUTPUT_DIR" -type f | wc -l) files"
}

write_metrics() {
    local wm_exit="${1:-${EXIT_CODE:-0}}"
    local wm_force="${2:-}"
    echo ""
    echo "=========================================="
    echo "=== Writing Metrics (exit=${wm_exit}${wm_force:+, force=${wm_force}})"
    echo "=========================================="

    # Score computation lives in the shared runtime (scripts/android/lib/metrics.py), not in
    # this deployment adapter. Keeping it in one place preserves identical score semantics.
    local _wm_rb; _wm_rb="$(rb_scripts_dir || true)"
    if [ -n "${_wm_rb}" ] && [ -f "${_wm_rb}/android/lib/metrics.py" ]; then
        EVAL_DIR="$EVAL_DIR" WM_EXIT="$wm_exit" WM_FORCE="$wm_force" \
        WM_STAGES="$(stage_status_json)" \
        PYTHONPATH="${_wm_rb}" python3 "${_wm_rb}/android/lib/metrics.py"
    else
        # deployment platform still needs A metrics.json or the job reports nothing at all. Emit the minimum with
        # an explicit reason rather than letting the collector find an empty output dir.
        echo "ERROR: rb core unavailable; writing a minimal metrics.json (scores unavailable)" >&2
        WM_EXIT="$wm_exit" WM_FORCE="$wm_force" \
        WM_STAGES="$(stage_status_json)" \
        OUTPUT_DIR="$OUTPUT_DIR" TASK_ID="$TASK_ID" STAGE="$STAGE" STAGE_LIST="$STAGE_LIST" \
        python3 "${_RB_CORE_ROOT}/android/lib/fallback_metrics.py"
        return "$RC_INFRA"
    fi
}

# ==========================================================================
# Main Flow
# ==========================================================================
# STAGE 归一成 STAGE_LIST 后统一按 canonical 顺序逐个执行。每个阶段都走
# download_stage_deps（同 pod 内前序阶段的产物由 stage_ran_here 短路）→ stage_* →
# verify_* → upload_stage_outputs。任一阶段失败即停，eval 例外：失败也回传产物。
#   recreation,eval is the full release range; setup runs alone.

# ── the benchmark pipeline ────────────────────────────────────────────────────────────────
# Everything below this point is benchmark logic and lives in rb, not in this adapter. Sourced
# (not executed) so the functions keep sharing this shell's globals and adapter helpers.
_RB_PIPE="$(rb_scripts_dir || true)"
if [ -z "${_RB_PIPE}" ] || [ ! -f "${_RB_PIPE}/android/stages/pipeline.sh" ]; then
    die "$RC_INFRA" "rb pipeline library unavailable (android/stages/pipeline.sh); pass rb_pipeline_commit"
fi
# shellcheck source=/dev/null
. "${_RB_PIPE}/android/stages/pipeline.sh"
echo "==> benchmark pipeline sourced from ${_RB_PIPE}/android/stages/pipeline.sh"

if [ "$STAGE_LIST" != "setup" ]; then
    install_runtime_assets || die "$RC_INFRA" "could not install Android runtime assets from the pinned RB artifact"
fi

EXIT_CODE=0

# ── pod 中断兜底（对齐 recreation-bench-linux on_term）──
# deployment platform 到达 runtime_timeout_sec 会给 pod 发 SIGTERM。此前没有 trap，被 kill 的 job
# 既不写 metrics.json 也不 flush 产物，事后只能从日志倒推。这里保证：
# 收产物 -> 写 metrics（eval 已出真分就用真分，否则标 pod_timeout）-> 尽力上传 artifact store。
_TERM_HANDLED=0
on_term() {
    [ "$_TERM_HANDLED" = "1" ] && return 0
    _TERM_HANDLED=1
    echo ""
    echo "[sigterm] pod termination received; collecting artifacts + writing metrics before exit"
    set +e
    set +o pipefail
    # Whatever was mid-flight when the wall clock hit is the interesting part, so
    # record it instead of leaving those stages absent from the map.
    local _s
    for _s in $(printf '%s' "$STAGE_LIST" | tr ',' ' '); do
        [ -n "${STAGE_STATUS[$_s]:-}" ] || stage_status_set "$_s" failed
    done
    collect_artifacts || true
    if [ -s "$EVAL_DIR/metrics.json" ]; then
        write_metrics "$RC_TIMEOUT" || true          # eval 已完成 -> 保留真实分数
    else
        write_metrics "$RC_TIMEOUT" pod_timeout || true
    fi
    # 尽力把各阶段产物刷到 artifact store，让中断的 run 至少留下 trajectory/日志
    if artifact_upload_enabled; then
        [ -d "$REC_DIR" ]     && artifact_upload_dir "$REC_DIR"     "$RECREATION_STAGE" || true
        [ -d "$EVAL_DIR" ]    && artifact_upload_dir "$EVAL_DIR"    "$EVAL_STAGE" || true
    fi
    echo "[sigterm] flush done; exiting $RC_TIMEOUT"
    exit "$RC_TIMEOUT"
}
trap on_term SIGTERM SIGINT

echo "Mode: stages [${STAGE_LIST}]  (from STAGE='${STAGE}')"
IFS=',' read -r -a _STAGE_ARR <<< "$STAGE_LIST"
for s in "${_STAGE_ARR[@]}"; do
    # `|| _rc=$?`, never a bare call: a stage's non-zero return IS the signal this
    # loop reads, and under `set -e` a bare call aborts the script before `_rc=$?`
    # exists — which skipped the stop branch, collect_artifacts (the trajectory
    # surface) and write_metrics, and let the pod exit with the raw stage code.
    _rc=0
    _run_single_stage "$s" || _rc=$?
    if [ "$_rc" -eq "$RC_OK" ] && [ "${PIPELINE_STOP:-0}" = 1 ]; then
        # A recorded non-result. Stop the sequence; the job is still a success.
        echo "stopping after '$s' (no result to carry forward); job still exits 0"
        break
    fi
    [ "$_rc" -eq "$RC_OK" ] && continue
    EXIT_CODE="$(normalize_public_rc "$_rc")"
    break
done

# Anything that never got a chance to run is skipped, not silently absent.
for s in $(printf '%s' "$STAGE_LIST" | tr ',' ' '); do
    [ -n "${STAGE_STATUS[$s]:-}" ] || stage_status_set "$s" skipped
done

trap - SIGTERM SIGINT
_TERM_HANDLED=1

# Wrap-up is best-effort, as in recreation-bench-linux: artifacts, the trajectory the
# observability platform reads, metrics and the artifact store flush all have to happen even for
# a run that failed. Keep errexit and pipefail off for the remainder — a SIGPIPE from
# something as ordinary as `find | head` is exit 141 under pipefail.
set +e
set +o pipefail

collect_artifacts || true
if ! write_metrics "$EXIT_CODE"; then
    echo "WARN: write_metrics failed; reporting infrastructure error"
    [ "$EXIT_CODE" -eq "$RC_TIMEOUT" ] || EXIT_CODE="$RC_INFRA"
fi

echo ""
echo "=========================================="
echo "=== Pipeline complete (exit=$EXIT_CODE) stages=$(stage_status_json)"
echo "=========================================="
exit "$(normalize_public_rc "$EXIT_CODE")"
