#!/usr/bin/env bash
###############################################################################
# RecreationBench Web native worker
#
# The runtime provider prepares this pod; this worker owns dataset -> agent -> build -> eval.
###############################################################################
set -euo pipefail

# Public process contract shared by all five RecreationBench entrypoints.
# Native child codes stay in logs/metrics; only these codes leave main.sh.
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
    if [ "$native_rc" -eq 130 ] || [ "$native_rc" -eq "$RC_TIMEOUT" ]; then
        exit "$RC_TIMEOUT"
    fi
    echo "[mockweb] FATAL: unexpected worker failure (native exit=${native_rc}); reporting infrastructure error" >&2
    exit "$RC_INFRA"
}
trap on_unexpected_error ERR

OUTPUT_DIR="${OUTPUT_DIR:-/workspace/output}"
DATA_DIR="${DATA_DIR:-/workspace/dataset}"
RB_SHARED_DIR="${RB_SHARED_DIR:-${SHARED_DIR:-/workspace/shared}}"
# The rollout cwd is the cross-platform POSIX contract. The completed tree is copied into the deployment platform
# output directory after build/evaluation so artifact storage remains separate from agent paths.
WORKSPACE_DIR="/workspace/recreation"
ARTIFACT_DIR="${OUTPUT_DIR}/recreation"
mkdir -p "${OUTPUT_DIR}" "${DATA_DIR}"

DOMAIN="${INSTANCE_ID:?INSTANCE_ID is required}"
STAGE="${STAGE:-recreation_eval}"
RB_EVAL_TARGET="${RB_EVAL_TARGET:-${EVAL_TARGET:-recreation}}"
AGENT_CLI="${AGENT_CLI:-claude}"
case "$AGENT_CLI" in
    claude) SCAFFOLD="claude-code" ;;
    codex)  SCAFFOLD="codex" ;;
    *) echo "[mockweb] ERROR: invalid AGENT_CLI=$AGENT_CLI (want claude or codex)" >&2; exit "$RC_INFRA" ;;
esac
export AGENT_CLI SCAFFOLD
export RB_EVAL_TARGET
export CONTEXT_1M="${CONTEXT_1M:-false}"
export AUTO_COMPACT_WINDOW="${AUTO_COMPACT_WINDOW:-}"
export THINKING_EFFORT="${THINKING_EFFORT:-max}"
export BROWSER_MCP="${BROWSER_MCP:-playwright}"
RB_CUA_DRIVER_REF="${RB_CUA_DRIVER_REF:-qwen:0.7.3}"
RB_CUA_PREFLIGHT_MODE="${RB_CUA_PREFLIGHT_MODE:-strict}"
export RB_CUA_DRIVER_REF RB_CUA_PREFLIGHT_MODE
# The frozen unified suite is the only release input. The materialiser turns
# <prefix>/web/<domain>/ into the dataset layout the scorer reads; gt_* is
# regenerated from the site mirror because the release does not carry a second
# screenshot corpus. Do not add a packed-dataset fallback here: a missing
# unified prefix must stop the run instead of silently grading different input.
RB_ARTIFACT_ROOT="${RB_ARTIFACT_ROOT:-}"
export RB_ARTIFACT_BACKEND RB_ARTIFACT_DRIVER RB_ARTIFACT_ROOT
export RB_ARTIFACT_BUCKET RB_ARTIFACT_ENDPOINT RB_ARTIFACT_ACCESS_KEY_ID RB_ARTIFACT_ACCESS_KEY_SECRET
RB_UNIFIED_PREFIX="${RB_UNIFIED_PREFIX:-}"
RB_UNIFIED_PLATFORM="${RB_UNIFIED_PLATFORM:-web}"
export RB_UNIFIED_PREFIX RB_UNIFIED_PLATFORM
ALLOW_IMAGE_READ="${ALLOW_IMAGE_READ:-true}"
export ALLOW_IMAGE_READ
# Enhanced (rollout-aligned) task prompt toggle. When "true", the runner swaps the
# step-1 browser/exploration section to the spec.md exploration prompt
# (prompts/browser_mcp_{browser_mcp}_enhanced.txt). Read by runner/run_agent.py.
ENHANCED_PROMPT="${ENHANCED_PROMPT:-false}"
export ENHANCED_PROMPT
# Full-site rebuild toggle. When "true", the runner drops task.json's fixed
# target_pages list from the task prompt and instructs the agent to crawl the served
# copy and rebuild every reachable page it can (skipping only pages it judges broken).
# Only widens what the agent BUILDS; scoring is unchanged (the scorer still measures
# the original target_pages). Read by runner/run_agent.py via the REPLICATE_ALL_PAGES
# env (its --replicate-all-pages CLI default), so no explicit flag pass is needed.
REPLICATE_ALL_PAGES="${REPLICATE_ALL_PAGES:-false}"
export REPLICATE_ALL_PAGES
TIMEOUT_MULTIPLIER="${TIMEOUT_MULTIPLIER:-10.0}"

# VLM visual-fidelity judge — OFF by default. When enabled, its pass-rate replaces
# the SSIM/LPIPS visual dimension; the model/endpoint/key are configured separately
# from the agent model via the params below (injected as env by the platform).
USE_VLM_JUDGE="${USE_VLM_JUDGE:-false}"
VLM_JUDGE_MODEL="${VLM_JUDGE_MODEL:-}"
VLM_JUDGE_BACKEND="${VLM_JUDGE_BACKEND:-}"
VLM_JUDGE_BASE_URL="${VLM_JUDGE_BASE_URL:-}"
VLM_JUDGE_API_KEY="${VLM_JUDGE_API_KEY:-}"
VLM_JUDGE_MODE="${VLM_JUDGE_MODE:-}"
VLM_JUDGE_MAX_CONCURRENCY="${VLM_JUDGE_MAX_CONCURRENCY:-}"

echo "[mockweb] === RecreationBench Web Evaluation ==="
echo "[mockweb] domain=${DOMAIN} scaffold=${SCAFFOLD} browser_mcp=${BROWSER_MCP} allow_image_read=${ALLOW_IMAGE_READ} enhanced_prompt=${ENHANCED_PROMPT} replicate_all_pages=${REPLICATE_ALL_PAGES}"
case "$BROWSER_MCP" in
    playwright|cua-driver) ;;
    *) echo "[mockweb] ERROR: invalid BROWSER_MCP=$BROWSER_MCP (want playwright or cua-driver)" >&2; exit "$RC_INFRA" ;;
esac
case "$STAGE" in
    setup|eval|recreation_eval) ;;
    *) echo "[mockweb] ERROR: invalid STAGE=$STAGE (want setup, eval, or recreation_eval)" >&2; exit "$RC_INFRA" ;;
esac
case "$RB_EVAL_TARGET" in
    recreation|reference) ;;
    *) echo "[mockweb] ERROR: invalid RB_EVAL_TARGET=$RB_EVAL_TARGET (want recreation or reference)" >&2; exit "$RC_INFRA" ;;
esac
if [ "$RB_EVAL_TARGET" = reference ] && [ "$STAGE" != eval ]; then
    echo "[mockweb] ERROR: RB_EVAL_TARGET=reference requires STAGE=eval" >&2
    exit "$RC_INFRA"
fi
if [ "$STAGE" = eval ] && [ "$RB_EVAL_TARGET" != reference ]; then
    echo "[mockweb] ERROR: standalone Web eval requires RB_EVAL_TARGET=reference; no recreation artifact was supplied to this pod" >&2
    exit "$RC_INFRA"
fi
if [ "$STAGE" = recreation_eval ]; then
    _HAS_RECREATION=1
else
    _HAS_RECREATION=0
fi
[ -n "${RB_UNIFIED_PREFIX:-}" ] || {
    echo "[mockweb] ERROR: RB_UNIFIED_PREFIX is required; packed dataset fallback is retired" >&2
    exit "$RC_INFRA"
}


# The deployment adapter fetches and verifies the immutable RB artifact before
# invoking this worker. Resolve shared modules from that tree; transport and
# release-pin handling do not belong in benchmark code.
_RB_CORE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

is_truthy() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

rb_core_dir() {
    printf '%s' "$(dirname "${_RB_CORE_ROOT}")"
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

# The repo root (parent of scripts/).
rb_pipeline_dir() {
    local s; s="$(rb_scripts_dir)" || return 1
    [ -n "${s}" ] || return 1
    printf '%s' "$(dirname "${s}")"
}

# The provider has already prepared the pod. Run the benchmark worker directly;
# it must never call deployment adapter or re-enter the shared RB controller.
###############################################################################
# Phase 1: Materialise the frozen unified instance
###############################################################################
# Security: evaluation/ (gt_screenshots, *.spec.ts, eval_config) must NOT
# exist on the filesystem while the agent is running — it will only be
# extracted after the agent finishes, right before the scorer runs.
_SITE_TARBALL="/var/lib/mockweb-scorer/${DOMAIN}.tar.gz"
echo "[mockweb] Phase 1: Materialising frozen unified instance..."
mkdir -p "$(dirname "${_SITE_TARBALL}")"
# Build the SAME <domain>/{site,evaluation} tarball the native scorer expects,
# so the agent-visible extraction (which excludes evaluation/) and the deferred
# scorer-side extraction both consume one immutable source.
_UNI_ROOT="/var/lib/mockweb-scorer/unified"
_RB_DIR="$(rb_core_dir)"
rm -rf "${_UNI_ROOT}"; mkdir -p "${_UNI_ROOT}/inst" "${_UNI_ROOT}/dataset"
chmod 700 "${_UNI_ROOT}"
RB_SCRIPTS_DIR="$(rb_scripts_dir 2>/dev/null || true)"
if [ -z "${RB_SCRIPTS_DIR}" ] || [ ! -f "${RB_SCRIPTS_DIR}/common/rb_unify/eval_bridge.py" ]; then
    echo "[mockweb] ERROR: rb_unify not available; cannot materialise the unified instance" >&2
    exit "$RC_INFRA"
fi
_COMMON="${RB_SCRIPTS_DIR}/common"
PYTHONPATH="${RB_SCRIPTS_DIR}" python3 -m infrastructure.artifacts.cli configured \
    >/dev/null || exit "$RC_INFRA"
_UNI_SRC="${RB_UNIFIED_PREFIX%/}/${RB_UNIFIED_PLATFORM}/${DOMAIN}"
echo "[mockweb]   unified instance ← ${_UNI_SRC}"
PYTHONPATH="${RB_SCRIPTS_DIR}" python3 -m infrastructure.artifacts.cli \
    xfer dl_dir "${_UNI_SRC}" "${_UNI_ROOT}/inst/${DOMAIN}/" >/dev/null \
    || exit "$RC_INFRA"
PYTHONPATH="${RB_SCRIPTS_DIR}:${_COMMON}" \
    python3 "${RB_SCRIPTS_DIR}/web/runtime.py" materialize \
        "${_UNI_ROOT}/inst/${DOMAIN}" "${_UNI_ROOT}/dataset" "${DOMAIN}" \
    || exit "$RC_DATA"
tar -czf "${_SITE_TARBALL}" -C "${_UNI_ROOT}/dataset" "${DOMAIN}"
echo "[mockweb]   repacked unified dataset → ${_SITE_TARBALL}"
# Everything downstream reads _SITE_TARBALL only, so reclaim both staging copies.
_uni_sz="$(du -sm "${_UNI_ROOT}" 2>/dev/null | cut -f1)"
rm -rf "${_UNI_ROOT}"
echo "[mockweb]   reclaimed ${_uni_sz:-?} MiB of unified staging"
df -h /var/lib/mockweb-scorer 2>/dev/null | tail -1 | sed 's/^/[mockweb]   disk: /'

# Agent-visible tree: site/ + task.json + site_meta.json (NO evaluation/).
tar -xzf "${_SITE_TARBALL}" \
    --exclude='*/evaluation' --exclude='*/evaluation/*' \
    -C "${DATA_DIR}"

if [ ! -d "${DATA_DIR}/${DOMAIN}" ]; then
    echo "[mockweb] ERROR: ${DATA_DIR}/${DOMAIN} not found after extraction" >&2
    ls -la "${DATA_DIR}" >&2
    exit "$RC_DATA"
fi
echo "[mockweb]   Extracted agent-visible tree to ${DATA_DIR}/${DOMAIN} (evaluation/ withheld)"

###############################################################################
# Phase 1.4: use Web code from the current RecreationBench checkout
###############################################################################
# Older images may contain a compatibility evaluator under /opt/mockweb-bench. The
# runner and evaluator code always come from scripts/web in this checkout so one
# source revision owns the scored behavior. The image supplies runtime dependencies:
# Playwright browsers, Node.js, the Python packages,
# torch/torchvision + lpips with AlexNet weights pre-warmed into /root/.cache/torch, and
# @playwright/test at batch_run/node_modules.
echo "[mockweb] Phase 1.4: validating the prepared RecreationBench runtime..."
if [ -z "${RB_SCRIPTS_DIR:-}" ] || [ ! -d "${RB_SCRIPTS_DIR}/web" ]; then
    echo "[mockweb] FATAL: prepared runtime does not contain scripts/web" >&2
    exit "$RC_INFRA"
fi
RB_WEB_DIR="${RB_SCRIPTS_DIR}/web"
export RB_WEB_DIR RB_SCRIPTS_DIR
echo "[mockweb]   web code: ${RB_WEB_DIR} ($(find "${RB_WEB_DIR}" -name '*.py' | wc -l) py files)"
# core/ is a SIBLING of web/ in the artifact, which is the whole reason this refactor also
# unblocks `from core import ...` inside the runner -- impossible from the image, whose
# scripts/web has no sibling core/.
export PYTHONPATH="${RB_SCRIPTS_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
# @playwright/test: the image bakes it at batch_run/node_modules (Dockerfile: "test_runner.py
# looks for PROJECT_ROOT/batch_run/node_modules/@playwright/test"). Running from rb moves
# PROJECT_ROOT, so point the override that evaluation/test_runner.py::_find_node_modules
# already supports at the image's copy. The template npm deps need nothing: copytree ignores
# node_modules, and the image's pre-install warmed the npm CACHE, which lives in HOME.
export MOCKWEB_NODE_MODULES="${MOCKWEB_NODE_MODULES:-/opt/mockweb-bench/batch_run/node_modules}"
if [ ! -d "${MOCKWEB_NODE_MODULES}/@playwright/test" ]; then
    echo "[mockweb]   FATAL: @playwright/test not at ${MOCKWEB_NODE_MODULES}" >&2
    exit "$RC_INFRA"
fi
# The recreation build itself is network-sealed, but bootstrap still has network.
# Materialise dependencies from the exact lockfile in the pinned rb artifact here,
# before permission setup and before the agent starts. This keeps rb code and its
# executable dependency graph atomic without rebuilding the orchestrator image for
# every rb pin.
#
# Install outside RB_WEB_DIR: that tree is the immutable downloaded artifact. The
# lock hash names the cache so a future multi-rollout job cannot reuse dependencies
# from another pin. `.complete` is written only after npm ci succeeds; a partial
# install is never accepted. Dev dependencies are required (Vite/Tailwind live there).
if [ "$_HAS_RECREATION" = 1 ]; then
    _RB_TEMPLATE_DIR="${RB_WEB_DIR}/template"
    _RB_TEMPLATE_LOCK="${_RB_TEMPLATE_DIR}/package-lock.json"
    if [ ! -f "${_RB_TEMPLATE_DIR}/package.json" ] || [ ! -f "${_RB_TEMPLATE_LOCK}" ]; then
        echo "[mockweb]   FATAL: pinned rb template is missing package.json or package-lock.json" >&2
        exit "$RC_INFRA"
    fi
    _RB_TEMPLATE_LOCK_SHA="$(sha256sum "${_RB_TEMPLATE_LOCK}" | awk '{print $1}')"
    _RB_TEMPLATE_DEPS_ROOT="${MOCKWEB_TEMPLATE_DEPS_ROOT:-${RB_SHARED_DIR}/mockweb-template-deps}"
    _RB_TEMPLATE_DEPS_DIR="${_RB_TEMPLATE_DEPS_ROOT}/${_RB_TEMPLATE_LOCK_SHA}"
    if [ ! -f "${_RB_TEMPLATE_DEPS_DIR}/.complete" ]; then
        echo "[mockweb]   installing template dependencies from rb lock ${_RB_TEMPLATE_LOCK_SHA:0:12}..."
        _RB_TEMPLATE_DEPS_TMP="${_RB_TEMPLATE_DEPS_ROOT}/.${_RB_TEMPLATE_LOCK_SHA}.$$"
        rm -rf "${_RB_TEMPLATE_DEPS_TMP}"
        mkdir -p "${_RB_TEMPLATE_DEPS_TMP}"
        cp "${_RB_TEMPLATE_DIR}/package.json" "${_RB_TEMPLATE_LOCK}" "${_RB_TEMPLATE_DEPS_TMP}/"
        if ! (cd "${_RB_TEMPLATE_DEPS_TMP}" && npm ci --include=dev --prefer-offline --no-audit --no-fund); then
            rm -rf "${_RB_TEMPLATE_DEPS_TMP}"
            echo "[mockweb]   FATAL: npm ci failed for pinned rb template lock" >&2
            exit "$RC_INFRA"
        fi
        touch "${_RB_TEMPLATE_DEPS_TMP}/.complete"
        rm -rf "${_RB_TEMPLATE_DEPS_DIR}"
        mv "${_RB_TEMPLATE_DEPS_TMP}" "${_RB_TEMPLATE_DEPS_DIR}"
    fi
    export MOCKWEB_TEMPLATE_NODE_MODULES="${_RB_TEMPLATE_DEPS_DIR}/node_modules"
    if [ ! -d "${MOCKWEB_TEMPLATE_NODE_MODULES}" ]; then
        echo "[mockweb]   FATAL: npm ci completed without node_modules" >&2
        exit "$RC_INFRA"
    fi
    # The agent may execute dependencies but must not rewrite the trusted dependency
    # tree. Its authored source remains writable in the separate workspace.
    chown -R root:root "${_RB_TEMPLATE_DEPS_DIR}"
    chmod -R a-w,a+rX "${_RB_TEMPLATE_DEPS_DIR}"
    echo "[mockweb]   template dependencies ready: lock=${_RB_TEMPLATE_LOCK_SHA:0:12} source=rb-bootstrap"
fi

# Establish and ATTEST the agent's file boundary before model setup. The boundary
# needs no endpoint, so a boundary-only canary can exercise it independently. Off
# by default; the flag
# turns it on so it can be canaried without touching the production lane. FAIL CLOSED: if the
# boundary cannot be attested the job stops, because a scored run at an isolation level nobody
# verified is worse than no run.
#
# Web is where this boundary is cleanest: run_agent.py SERVES the reference over localhost and asks
# the agent to rebuild what it SEES, so nothing about the task needs the agent to read those files --
# and on this platform the tests ship inside the same site payload, so one protected path covers the
# reference and the answer key together.
case "${RB_PERM_SETUP:-}" in
  1|true|TRUE|yes|on)
    echo "[mockweb] permission isolation: attesting the agent's file boundary"
    _RB_WEB_DIR="${RB_SCRIPTS_DIR}/web"
    if [ -z "${_RB_WEB_DIR}" ]; then
        echo "[mockweb] ERROR: scripts/web/setup_stage.py not found in the rb tree -- is rb_pipeline_commit published?"
        exit "$RC_INFRA"
    fi
    # NOT `| tee`: without pipefail the `if` would test TEE's exit status, which is always 0, so the
    # fail-closed branch could never fire -- a boundary that silently does not hold. Write the file,
    # then echo it.
    _RB_PERM_RC=0
    python3 "${_RB_WEB_DIR}/setup_stage.py" \
            --run-id "${INSTANCE_ID:-${TASK_ID:-web}}" \
            --site-dir "${DATA_DIR}/${DOMAIN}/site" \
            --workspace-dir "${WORKSPACE_DIR}" \
            --report "${OUTPUT_DIR}/permission_report.json" \
            > "${OUTPUT_DIR}/permission_setup.txt" 2>&1 || _RB_PERM_RC=$?
    sed 's/^/[mockweb]   /' "${OUTPUT_DIR}/permission_setup.txt" || true
    if [ "$_RB_PERM_RC" -ne 0 ]; then
        echo "[mockweb] ERROR: permission isolation did NOT hold (rc=$_RB_PERM_RC) -- refusing to run the agent"
        exit "$RC_INFRA"
    fi
    # The standalone setup stage stops here; release execution continues into run_agent.
    if [ "$STAGE" = setup ]; then
        echo "[mockweb] permission isolation attested; setup stage complete"
        exit "$RC_OK"
    fi
    ;;
esac

# The image tag is rolling and Kubernetes may reuse a cached image. Converge the selected browser
# MCP from RB's canonical package contract instead of duplicating package/version logic here.
if [ "$_HAS_RECREATION" = 1 ] && [ "$BROWSER_MCP" = "playwright" ]; then
    PYTHONPATH="$RB_SCRIPTS_DIR" python3 -m core.mcp_settings ensure-playwright || {
        echo "[mockweb] FATAL: Playwright MCP runtime convergence failed" >&2
        exit "$RC_INFRA"
    }
fi

if [ "$_HAS_RECREATION" = 1 ]; then
    : "${RB_AGENT_BASE_URL:?deployment adapter must provide RB_AGENT_BASE_URL}"
    : "${RB_AGENT_API_KEY:?deployment adapter must provide RB_AGENT_API_KEY}"
fi

###############################################################################
# Phase 2: Configure agent scaffold (claude-code | codex)
###############################################################################
# Run the whole orchestrator under the AGENT's HOME so BOTH scaffolds' config
# (claude ~/.claude, codex ~/.codex) and the config the de-privileged agent reads
# at runtime resolve to the same path. The agent runs as the non-root `agent` uid
# (see the isolation block below). Phase 1's short-lived artifact client already
# exited and removed its private credential file, so this does not affect it.
export HOME=/home/agent
mkdir -p "${HOME}"

# Ensure the non-privileged `agent` uid exists. The image (Dockerfile.orchestrator)
# bakes it via `useradd -u 10001`, but the orchestrator runs the rolling image tag
# `master_v2-latest`; a k8s node that still has an older cached image (predating the
# useradd layer) will NOT re-pull on the tag (IfNotPresent), so the isolation
# chown/setpriv below breaks with `chown: invalid user 'agent:agent'`. Rebuilding
# the image does NOT fix stale nodes, and deployment platform has no per-job image/digest override.
# main.sh runs here as root from the git ref, so self-heal: create the account if
# the running image lacks it. Idempotent no-op when the image already provides it.
if ! id -u agent >/dev/null 2>&1; then
    echo "[mockweb] agent uid missing from running image — creating at runtime (uid 10001)"
    useradd -m -u 10001 -s /bin/bash agent 2>/dev/null \
        || useradd -m -s /bin/bash agent 2>/dev/null \
        || { echo "[mockweb] FATAL: could not create agent user" >&2; exit "$RC_INFRA"; }
fi

# Pin the Playwright browser path. We point HOME at /home/agent (for the agent scaffold
# config), but the image installs Chromium elsewhere: the stock/rolling image installs it
# as root with no PLAYWRIGHT_BROWSERS_PATH → /root/.cache/ms-playwright; a freshly-built
# image bakes PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright. Without pinning, both the (root)
# scorer's Playwright and the agent browser MCP resolve ~/.cache/ms-playwright =
# /home/agent/.cache and fail "Executable doesn't exist" → eval_error → 0 score. Probe the
# known locations and export the real path so it no longer depends on HOME.
if [ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ] || ! ls -d "${PLAYWRIGHT_BROWSERS_PATH}"/chromium*/ >/dev/null 2>&1; then
    for _pw in /opt/ms-playwright /root/.cache/ms-playwright /home/agent/.cache/ms-playwright; do
        if ls -d "${_pw}"/chromium*/ >/dev/null 2>&1; then
            export PLAYWRIGHT_BROWSERS_PATH="${_pw}"
            echo "[mockweb] PLAYWRIGHT_BROWSERS_PATH=${_pw} (chromium located)"
            break
        fi
    done
    if [ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
        echo "[mockweb] FATAL: Chromium not found in known Playwright paths" >&2
        exit "$RC_INFRA"
    fi
fi

# (B) Make the browser dir reachable by the NON-ROOT agent uid too (its own browser MCP).
# Fresh image: /opt/ms-playwright is already a+rX. Stale image: chromium is under
# /root/.cache (/root is 0700) so the de-privileged agent can't traverse to it and rebuilds
# "blind". Open the minimal traversal path: o+x on each ancestor dir (traverse only, not
# listable/readable) + o+rX on the browser tree. Reference/answers live under DATA_DIR
# (0700), not /root, so this does not widen the reference isolation.
if [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ] && [ -d "${PLAYWRIGHT_BROWSERS_PATH}" ]; then
    _d="${PLAYWRIGHT_BROWSERS_PATH}"
    while [ "${_d}" != "/" ] && [ -n "${_d}" ]; do chmod o+x "${_d}" 2>/dev/null || true; _d="$(dirname "${_d}")"; done
    chmod -R o+rX "${PLAYWRIGHT_BROWSERS_PATH}" 2>/dev/null || true
fi

# (A) Pin TORCH_HOME the same way. The LPIPS AlexNet backbone weights are pre-warmed into
# /root/.cache/torch at image build (HOME=/root); with HOME=/home/agent the (root) scorer's
# torch.hub would look in /home/agent/.cache/torch, miss, try to download offline, fail, and
# LPIPS is silently dropped — degrading the 50%-weight visual dimension to SSIM-only. Point
# torch at the pre-warmed cache regardless of HOME.
if [ -z "${TORCH_HOME:-}" ]; then
    for _th in /root/.cache/torch /opt/torch /home/agent/.cache/torch; do
        if ls -d "${_th}"/hub/checkpoints/* >/dev/null 2>&1; then
            export TORCH_HOME="${_th}"
            echo "[mockweb] TORCH_HOME=${_th} (LPIPS/torch weights located)"
            break
        fi
    done
fi

if [ "$_HAS_RECREATION" = 1 ] && [ "${SCAFFOLD}" = "codex" ]; then
    echo "[mockweb] Phase 2: Configuring codex..."
    # The deployment adapter owns endpoint preparation and protocol conversion.
    export OPENAI_API_KEY="${RB_AGENT_API_KEY}"
    export OPENAI_BASE_URL="${RB_AGENT_BASE_URL}"
    export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

    _WANT_CODEX="${SCAFFOLD_VERSION:-0.145.0}"
    _HAVE_CODEX="$(codex --version 2>/dev/null | awk '{print $NF}' || true)"
    if [ "${_HAVE_CODEX}" != "${_WANT_CODEX}" ]; then
        echo "[mockweb]   Installing @openai/codex@${_WANT_CODEX}..."
        npm install -g --no-audit --no-fund "@openai/codex@${_WANT_CODEX}" >/dev/null 2>&1
    fi
    _HAVE_CODEX="$(codex --version 2>/dev/null | awk '{print $NF}' || true)"
    [ "${_HAVE_CODEX}" = "${_WANT_CODEX}" ] || {
        echo "[mockweb] FATAL: codex version ${_HAVE_CODEX:-missing}; expected ${_WANT_CODEX}" >&2
        exit "$RC_INFRA"
    }

    mkdir -p "${HOME:-/root}/.codex"
    # Use the endpoint contract prepared by the deployment adapter. The runner
    # appends the browser MCP table to this same file.
    PYTHONPATH="${RB_SCRIPTS_DIR}" python3 -m core.agent_config codex \
        --model "${MODEL}" \
        --base-url "${OPENAI_BASE_URL}" \
        --provider "${RB_AGENT_PROVIDER:-litellm}" \
        --name "${RB_AGENT_PROVIDER_NAME:-litellm}" \
        --wire-api "${RB_AGENT_WIRE_API:-responses}" \
        --supports-websockets "${RB_AGENT_SUPPORTS_WEBSOCKETS:-false}" \
        --credential-env "${RB_AGENT_CREDENTIAL_ENV:-OPENAI_API_KEY}" \
        --request-max-retries "${RB_AGENT_REQUEST_MAX_RETRIES:-10}" \
        --stream-max-retries "${RB_AGENT_STREAM_MAX_RETRIES:-10}" \
        > "${HOME:-/root}/.codex/config.toml"
    echo "[mockweb]   codex → ${OPENAI_BASE_URL} (prepared endpoint)"
elif [ "$_HAS_RECREATION" = 1 ]; then
echo "[mockweb] Phase 2: Configuring claude-code..."

CLAUDE_BASE_URL="${RB_AGENT_BASE_URL}"
CLAUDE_AUTH_TOKEN="${RB_AGENT_API_KEY}"
echo "[mockweb]   claude-code → ${CLAUDE_BASE_URL} (prepared endpoint)"

export ANTHROPIC_BASE_URL="${CLAUDE_BASE_URL}"
export ANTHROPIC_API_KEY="${CLAUDE_AUTH_TOKEN}"
export ANTHROPIC_AUTH_TOKEN="${CLAUDE_AUTH_TOKEN}"
case "$AGENT_CLI" in
    codex) export ANTHROPIC_MODEL="$MODEL" ;;
    *) export ANTHROPIC_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}" ;;
esac
export DISABLE_AUTOUPDATER=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# Stop claude-code from sending the experimental `thinking-token-count`
# anthropic-beta header — the MR gateway rejects it with HTTP 418, killing
# every API call. The RB workspace settings writer carries this too, but exporting
# it here also covers setup before the workspace exists.
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
# Bound every MCP tool call. Unset means UNBOUNDED on this claude generation (verified on
# 2.1.237: a stub MCP server sleeping 600 s was never cut off, while MCP_TOOL_TIMEOUT=8000
# produced `timed out after 8s` and the agent carried on), so one hung browser-MCP call
# would consume the whole time limit with nothing in the score to show why. This export --
# not the equivalent in rb's runner/run_agent.py -- is the load-bearing one: rb's
# scripts/core is FETCHED at runtime into rb_core_dir, so the runner's own
# `from core import mcp_settings` cannot resolve from inside the image (its scripts/web has
# no sibling core/). Shared default: rb scripts/core/mcp_settings.py.
export MCP_TOOL_TIMEOUT="${RB_MCP_TOOL_TIMEOUT:-180000}"
# Force pure-python protobuf so the evaluator's lazy LPIPS import doesn't
# crash on the onnx/protobuf C-extension clash ("Descriptors cannot be
# created directly").
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
export CLAUDE_BASE_URL CLAUDE_AUTH_TOKEN

# ── Disable claude-code's stream / first-byte watchdogs ────────────────────
# The local vLLM behind the 2-hop tunnel has a slow TTFT for big generations
# (large prefill: full-page HTML / many images → ~90k tok). claude-code's
# built-in byte/first-byte watchdog aborts + retries a stream whose first byte
# is slow (CLAUDE_SLOW_FIRST_BYTE_MS defaults to 30000ms), so the big "write
# App.tsx" call gets cancelled repeatedly until MAX_RETRIES is exhausted → run
# dies / ships a scaffold placeholder. We turn the watchdogs off and raise all
# stream/idle/first-byte thresholds so slow-but-alive generations are allowed
# to finish. (Treat the real cause — slow prefill — separately by trimming
# context / per-worker concurrency.)
export CLAUDE_ENABLE_BYTE_WATCHDOG=0
export CLAUDE_ENABLE_STREAM_WATCHDOG=0
export CLAUDE_SLOW_FIRST_BYTE_MS=600000
export CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS=600000
export CLAUDE_STREAM_IDLE_TIMEOUT_MS=600000
export API_TIMEOUT_MS="${RB_API_TIMEOUT_MS:-1800000}"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-${MAX_TOKENS_LIMIT:-128000}}"
if [ -n "$AUTO_COMPACT_WINDOW" ]; then
    export CLAUDE_CODE_AUTO_COMPACT_WINDOW="$AUTO_COMPACT_WINDOW"
else
    unset CLAUDE_CODE_AUTO_COMPACT_WINDOW
fi
unset CLAUDE_AUTOCOMPACT_PCT_OVERRIDE
echo "[mockweb]   compaction: window=${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-Claude default}, pct=Claude default (80%), max_output=${CLAUDE_CODE_MAX_OUTPUT_TOKENS}"

claude --version || { echo "[mockweb] FATAL: claude-code CLI not available" >&2; exit "$RC_INFRA"; }

# If SCAFFOLD_VERSION is set and differs from the pre-installed version,
# reinstall claude-code at the requested version.
_INSTALLED_CC="$(claude --version 2>/dev/null | awk '{print $1}' || true)"
# 2.1.177 is the pin shared with the other four platforms (they converge on it in
# their own main.sh). SCAFFOLD_VERSION still overrides for a deliberate A/B.
_WANT_CC="${SCAFFOLD_VERSION:-2.1.177}"
if [ "${_WANT_CC}" != "latest" ] && [ "${_WANT_CC}" != "${_INSTALLED_CC}" ]; then
    echo "[mockweb]   Reinstalling claude-code@${_WANT_CC} (image has ${_INSTALLED_CC})..."
    npm install -g --no-audit --no-fund "@anthropic-ai/claude-code@${_WANT_CC}" >/dev/null 2>&1
fi
[ "$(claude --version 2>/dev/null | awk '{print $1}' || true)" = "${_WANT_CC}" ] || {
    echo "[mockweb] FATAL: claude-code version mismatch; expected ${_WANT_CC}" >&2
    exit "$RC_INFRA"
}

fi

# ── Scorer source isolation + agent HOME handoff ────────────────────────────
# Hand the agent HOME (incl. the .claude/.codex config written in Phase 2) to the
# non-root agent uid so the de-privileged rollout can read config + write session
# state. And isolate scoring logic: instead of moving the repo (v1 whitelist →
# /opt/testbed), keep it in place and make ONLY evaluation/ root-only 0700 — the
# agent uid can't read it, root (orchestrator + scorer) still can. runner/serving/
# prompts/template/scripts stay agent-readable (framework code the agent needs).
# Create Claude's state directory before the ownership handoff.  The RB runner
# writes mcp.json here later while still running as root; if the directory does
# not exist until then it becomes root:root 0755 and the demoted Claude process
# cannot create ~/.claude/session-env.  This mirrors Android/macOS: root may
# render config, but the runtime user owns the complete state directory.
if [ "$_HAS_RECREATION" = 1 ]; then
    mkdir -p "${HOME}/.claude"
    chown -R agent:agent /home/agent
fi
chmod 700 "${RB_WEB_DIR}/evaluation"

###############################################################################
# Phase 3: Run single instance (claude-code → npm build → evaluate)
###############################################################################
# ── Per-platform inputs to the SHARED pod-termination handler ─────────────────────────
RB_TERM_PLATFORM="web"
RB_TERM_METRICS="${OUTPUT_DIR}/metrics.json"
RB_TERM_TASK_ID="${DOMAIN}"

###############################################################################
# SHARED pod-termination handler (byte-identical across the in-pod templates).
#
# deployment platform kills a job that outruns runtime_timeout_sec with SIGTERM, then SIGKILL after the grace
# period. web/windows/macOS had NO handler at all, so a wall-clock kill left `run_duration null`
# and no metrics.json whatsoever -- indistinguishable from a job that never ran. Two web opus-5
# jobs died exactly that way (exit code 143 at 28800s, zero artifacts).
#
# The grace period is short, so this does the one thing that fits in it: record that the pod was
# killed, in a shape the roll-up can tell apart from a real zero. The status is the neutral
# `pod_terminated`, NOT pod_timeout: SIGTERM arrives for a wall-clock kill AND for an external
# cancel, and both happen -- two web jobs died on RUNTIME_TIMEOUT and four more were swept by a
# cross-account cancel at 08:33 on 2026-08-22. The handler cannot tell them apart, so it must
# not claim to. It deliberately does NOT try to
# upload artifacts -- that would not finish, and a half-written upload is worse than none.
#
# Per-platform inputs, named rather than inlined, so the function text stays identical:
#   RB_TERM_PLATFORM   platform tag for the unified metrics contract
#   RB_TERM_METRICS    path to the metrics.json the scorer would have written
#   RB_TERM_TASK_ID    instance id for the record
###############################################################################
on_term() {
    [ "${_RB_TERM_HANDLED:-0}" = "1" ] && return 0
    _RB_TERM_HANDLED=1
    echo "[rb][sigterm] pod termination received; recording pod_terminated"
    if declare -F rb_provider_stop >/dev/null 2>&1; then
        rb_provider_stop
    fi
    # Preserve any score already written, but the public outcome must still match
    # this process' rc=143. The stdlib writer below updates only the lifecycle
    # envelope and keeps all score/artifact fields intact.
    if [ -s "${RB_TERM_METRICS}" ]; then
        echo "[rb][sigterm] metrics.json already on disk — preserving score and marking termination"
    fi
    PYTHONPATH="${RB_SCRIPTS_DIR}" python3 "${RB_SCRIPTS_DIR}/web/runtime.py" \
        write-termination \
        --path "${RB_TERM_METRICS}" \
        --platform "${RB_TERM_PLATFORM:-}" \
        --task-id "${RB_TERM_TASK_ID:-}" \
        --stage "${STAGE:-recreation_eval}" \
        --model "${MODEL:-}" \
        || echo "[rb][sigterm] WARN: could not write ${RB_TERM_METRICS}"
    exit "${RB_TERM_EXIT_CODE:-$RC_TIMEOUT}"
}
trap on_term SIGTERM SIGINT

echo "[mockweb] Phase 3: Running rollout + evaluation..."
cd "${RB_WEB_DIR}"

# Build VLM-judge CLI args for the scorer (Phase 3c). Absent --vlm-judge → the
# scorer force-disables the judge (overriding any baked eval_config.json) and scores
# the visual dimension via SSIM/LPIPS.
VLM_JUDGE_ARGS=()
if is_truthy "${USE_VLM_JUDGE}"; then
    VLM_JUDGE_ARGS+=(--vlm-judge)
    [ -n "${VLM_JUDGE_MODEL}" ]    && VLM_JUDGE_ARGS+=(--vlm-model    "${VLM_JUDGE_MODEL}")
    [ -n "${VLM_JUDGE_BACKEND}" ]  && VLM_JUDGE_ARGS+=(--vlm-backend  "${VLM_JUDGE_BACKEND}")
    [ -n "${VLM_JUDGE_BASE_URL}" ] && VLM_JUDGE_ARGS+=(--vlm-base-url "${VLM_JUDGE_BASE_URL}")
    [ -n "${VLM_JUDGE_API_KEY}" ]  && VLM_JUDGE_ARGS+=(--vlm-api-key  "${VLM_JUDGE_API_KEY}")
    [ -n "${VLM_JUDGE_MODE}" ]     && VLM_JUDGE_ARGS+=(--vlm-mode     "${VLM_JUDGE_MODE}")
    [ -n "${VLM_JUDGE_MAX_CONCURRENCY}" ] && VLM_JUDGE_ARGS+=(--vlm-max-concurrency "${VLM_JUDGE_MAX_CONCURRENCY}")
    echo "[mockweb]   VLM judge ENABLED (model=${VLM_JUDGE_MODEL:-<config default>} backend=${VLM_JUDGE_BACKEND:-<config default>})"
else
    echo "[mockweb]   VLM judge DISABLED (default) — visual dim scored via SSIM/LPIPS"
fi

if [ "$RB_EVAL_TARGET" = reference ]; then
    # Reference checks grade the exact frozen site tree. Restore the withheld
    # evaluator, then call the RB runner's dedicated no-agent/no-build lane.
    echo "[mockweb] Phase 3: Evaluating frozen reference candidate..."
    if [ -n "${_SITE_TARBALL}" ] && [ -f "${_SITE_TARBALL}" ]; then
        tar -xzf "${_SITE_TARBALL}" -C "${DATA_DIR}" \
            --wildcards '*/evaluation/*' '*/evaluation' 2>/dev/null || true
        rm -f "${_SITE_TARBALL}"
    fi
    if [ ! -d "${DATA_DIR}/${DOMAIN}/evaluation" ]; then
        echo "[mockweb] FATAL: frozen evaluation/ was not restored for reference check" >&2
        exit "$RC_DATA"
    fi
    mkdir -p "${WORKSPACE_DIR}"
    RC=0
    python3 -m runner.run_agent \
        --dataset "${DATA_DIR}" \
        --domain "${DOMAIN}" \
        --model "${MODEL:-reference-check}" \
        --workspace "${WORKSPACE_DIR}" \
        --reference-candidate \
        ${VLM_JUDGE_ARGS[@]+"${VLM_JUDGE_ARGS[@]}"} \
        > "${OUTPUT_DIR}/run.json" || RC=$?
    if [ ${RC} -ne 0 ]; then
        echo "[mockweb] FATAL: reference eval failed before producing a valid score (exit=${RC})" >&2
        if [ "$RC" -eq 130 ] || [ "$RC" -eq "$RC_TIMEOUT" ]; then
            RC=$RC_TIMEOUT
        else
            RC=$RC_INFRA
        fi
    fi
    if [ -d "${WORKSPACE_DIR}/eval_results" ]; then
        rm -rf "${OUTPUT_DIR}/eval_results"
        cp -a "${WORKSPACE_DIR}/eval_results" "${OUTPUT_DIR}/eval_results"
    fi
else
# ─── 3a: Run agent + build (eval will fail since evaluation/ is withheld). ───
# Close the artifact re-fetch path BEFORE the agent runs: the site tarball (which also
# holds the withheld evaluation/ answers) is re-downloadable with these creds, so
# file perms alone wouldn't stop reading the answers. Only needed in Phase 1 (done).
# The tree is already available from Phase 1.4. Keep this idempotent lookup here so
# later metric and artifact phases remain independent of phase ordering.
rb_scripts_dir >/dev/null || true
# Selective, NOT blanket root-only. The blanket `chmod -R go-rwx "$(rb_core_dir)"` was correct
# when only the SCORER came from rb; now the agent's own framework code (runner/, template/,
# prompts/, serving/) is in this tree too, and an unprivileged agent that cannot read it cannot
# run. The same boundary applies to the compatibility evaluator path: everything agent-readable,
# ONLY web/evaluation/ root-0700.
if [ -d "$(rb_core_dir)" ]; then
    chmod -R go+rX "$(rb_core_dir)" 2>/dev/null || true
    chmod -R go-rwx "${RB_WEB_DIR}/evaluation" 2>/dev/null || true
fi

# ── Reference-source isolation: non-root uid + file permissions ──────────────
# The agent (claude/codex rollout AND npm build) runs as the unprivileged `agent`
# uid via a setpriv run-prefix (consumed in runner/run_agent.py::_agent_run_prefix,
# applied to the rollout + build subprocesses). The reference site, the withheld-
# answer tarball, and evaluation/ are root-owned 0700, so the agent can't read them
# off disk — the reference is reachable ONLY over HTTP (served by the root SiteServer,
# which runs outside the prefix). No userns/CAP prerequisite; the uid applies to every
# agent subprocess uniformly. leak_scan remains the third-layer backstop.
chmod 700 "${DATA_DIR}"                                    # reference site + task data (+ answers after 3b)
[ -n "${_SITE_TARBALL:-}" ] && [ -f "${_SITE_TARBALL}" ] && chmod 700 "${_SITE_TARBALL}"
chmod 755 "${OUTPUT_DIR}"                                  # agent must traverse into its workspace
# Ensure every ancestor of the workspace is traversable (o+x) by the non-root agent uid,
# else its `cd`/absolute-path access into the workspace fails EACCES → whole run aborts
# (false 0). OUTPUT_DIR is 0755 but its parent mount may be 0700 root:root.
_wd="$(dirname "${OUTPUT_DIR}")"
while [ "${_wd}" != "/" ] && [ -n "${_wd}" ]; do chmod o+x "${_wd}" 2>/dev/null || true; _wd="$(dirname "${_wd}")"; done
export MOCKWEB_AGENT_UID="$(id -u agent)" MOCKWEB_AGENT_GID="$(id -g agent)"
# The shared invocation contract strips benchmark inputs and upstream credentials.
# This wrapper only changes the operating-system principal and its home directory.
export MOCKWEB_AGENT_RUN_PREFIX="setpriv --reuid=agent --regid=agent --clear-groups -- env HOME=/home/agent"
echo "[mockweb]   Reference isolation ON — agent=non-root uid ${MOCKWEB_AGENT_UID}; ${DATA_DIR} + tarball + evaluation/ root-0700 (HTTP-only reference)"

if [ "${BROWSER_MCP}" = "cua-driver" ]; then
  echo "[mockweb]   BROWSER_MCP=cua-driver — provisioning CUA X stack as agent uid ${MOCKWEB_AGENT_UID}..."
  _AS_AGENT="setpriv --reuid=agent --regid=agent --clear-groups --"
  # The optional Web CUA path is still part of the release contract: converge it to the
  # same qwen fork/version as Linux, Windows and macOS. Never accept an image-baked or
  # partially installed driver merely because its executable exists.
  _CUA_PARSED="$(PYTHONPATH="$RB_SCRIPTS_DIR" python3 "${RB_SCRIPTS_DIR}/web/runtime.py" \
      parse-cua-ref "$RB_CUA_DRIVER_REF")" \
      || { echo "[mockweb] FATAL: invalid RB_CUA_DRIVER_REF=${RB_CUA_DRIVER_REF}" >&2; exit "$RC_INFRA"; }
  _CUA_SOURCE="${_CUA_PARSED%%|*}"
  _CUA_VERSION="${_CUA_PARSED#*|}"
  [ "$_CUA_SOURCE" = qwen ] && [ -n "$_CUA_VERSION" ] || {
    echo "[mockweb] FATAL: Web CUA requires a versioned qwen ref, got ${RB_CUA_DRIVER_REF}" >&2
    exit "$RC_INFRA"
  }
  _CUA_HAVE="$(qwen-cua-driver --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  if [ "$_CUA_HAVE" != "$_CUA_VERSION" ]; then
    echo "[mockweb]   qwen-cua-driver ${_CUA_HAVE:-missing} != pin ${_CUA_VERSION}; installing"
    curl -fsSL --retry 5 --retry-delay 10 --retry-all-errors \
      "https://raw.githubusercontent.com/QwenLM/qwen-code/cua-driver-rs-v${_CUA_VERSION}/packages/cua-driver/scripts/install.sh" \
      -o /tmp/qwen-cua-install.sh || {
        echo "[mockweb] FATAL: could not download qwen-cua-driver installer" >&2
        exit "$RC_INFRA"
      }
    CUA_DRIVER_RS_VERSION="$_CUA_VERSION" CUA_DRIVER_RS_INSTALL_DIR=/usr/local/bin \
      CUA_DRIVER_RS_NO_MODIFY_PATH=1 bash /tmp/qwen-cua-install.sh || {
        echo "[mockweb] FATAL: qwen-cua-driver ${_CUA_VERSION} install failed" >&2
        exit "$RC_INFRA"
      }
    hash -r
    _CUA_HAVE="$(qwen-cua-driver --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  fi
  [ "$_CUA_HAVE" = "$_CUA_VERSION" ] || {
    echo "[mockweb] FATAL: qwen-cua-driver version ${_CUA_HAVE:-missing}; expected ${_CUA_VERSION}" >&2
    exit "$RC_INFRA"
  }
  echo "[mockweb]   qwen-cua-driver ${_CUA_HAVE} verified"
  # Runtime fallback for rolling images: install anything the image did not bake.
  command -v google-chrome-stable >/dev/null 2>&1 || {
    wget -q -O /tmp/gc.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb 2>&1 || true
    [ -f /tmp/gc.deb ] && { dpkg -i /tmp/gc.deb 2>/dev/null || apt-get install -f -y 2>/dev/null || true; rm -f /tmp/gc.deb; }
  }
  command -v Xvfb >/dev/null 2>&1 || apt-get install -y xvfb dbus dbus-x11 at-spi2-core openbox xdotool imagemagick 2>/dev/null || true
  for _cmd in qwen-cua-driver google-chrome-stable Xvfb dbus-daemon dbus-launch openbox; do
    command -v "$_cmd" >/dev/null 2>&1 || {
      echo "[mockweb] FATAL: CUA runtime dependency missing: $_cmd" >&2
      exit "$RC_INFRA"
    }
  done
  # X server + services, all as agent uid (isolation-preserving)
  export DISPLAY=:99
  pgrep -x Xvfb >/dev/null 2>&1 || ${_AS_AGENT} Xvfb :99 -screen 0 1920x1080x24 >/dev/null 2>&1 &
  sleep 2
  [ -S /run/dbus/system_bus_socket ] || { mkdir -p /run/dbus; dbus-daemon --system --fork 2>/dev/null || true; }
  eval "$(${_AS_AGENT} dbus-launch --sh-syntax 2>/dev/null)" || true
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-autolaunch:}"
  pgrep -f at-spi2-registryd >/dev/null 2>&1 || ${_AS_AGENT} /usr/libexec/at-spi2-registryd >/dev/null 2>&1 &
  pgrep -x openbox >/dev/null 2>&1 || ${_AS_AGENT} openbox >/dev/null 2>&1 &
  sleep 1
  export GTK_MODULES="gail:atk-bridge" QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 QT_ACCESSIBILITY=1 ELECTRON_ENABLE_ACCESSIBILITY=1
  # google-chrome wrapper the driver launches (strips headless/gpu, X11 + CDP :9222)
  install -m 0755 \
    "${RB_SCRIPTS_DIR}/web/runtime_assets/chromium_wrapper.sh" \
    /usr/local/bin/chromium
  mkdir -p /tmp/cua-chrome-profile && touch "/tmp/cua-chrome-profile/First Run"
  chown -R agent:agent /tmp/cua-chrome-profile
  qwen-cua-driver doctor 2>&1 || true
  echo "[mockweb]   CUA X stack ready (DISPLAY=${DISPLAY}, DBUS=${DBUS_SESSION_BUS_ADDRESS})"
fi

# Capture the child status in an OR-list: set +e alone leaves the ERR trap
# active and would exit before collecting the result and partial trajectory.
AGENT_RC=0
python3 -m runner.run_agent \
    --dataset "${DATA_DIR}" \
    --domain  "${DOMAIN}" \
    --model   "${MODEL}" \
    --workspace "${WORKSPACE_DIR}" \
    --browser-mcp "${BROWSER_MCP}" \
    --timeout-multiplier "${TIMEOUT_MULTIPLIER}" \
    --skip-eval \
    > "${OUTPUT_DIR}/run_agent_only.json" || AGENT_RC=$?

if [ ${AGENT_RC} -ne 0 ]; then
    echo "[mockweb] FATAL: recreation agent phase failed before producing a valid result (exit=${AGENT_RC})" >&2
    if [ "$AGENT_RC" -eq 130 ] || [ "$AGENT_RC" -eq "$RC_TIMEOUT" ]; then
        RC=$RC_TIMEOUT
    else
        RC=$RC_INFRA
    fi
    # Keep the runner's actual API/process error and skip evaluation. The common
    # finalization below writes metrics and stages the partial workspace first.
    cp "${OUTPUT_DIR}/run_agent_only.json" "${OUTPUT_DIR}/run.json"
else

# ─── 3b: Restore full scorer environment for evaluation. ─────────────────────
# Extract per-site evaluation/ (GT screenshots, specs) from site tarball.
if [ -n "${_SITE_TARBALL}" ] && [ -f "${_SITE_TARBALL}" ]; then
    echo "[mockweb] Phase 3b: Extracting evaluation/ for scorer..."
    tar -xzf "${_SITE_TARBALL}" -C "${DATA_DIR}" \
        --wildcards '*/evaluation/*' '*/evaluation' 2>/dev/null || true
    rm -f "${_SITE_TARBALL}"
    if [ ! -d "${DATA_DIR}/${DOMAIN}/evaluation" ]; then
        echo "[mockweb] FATAL: evaluation/ (answer key) not extracted from tarball — cannot score this domain. Failing the job so it is excluded from scoring, rather than recording a bogus 0." >&2
        exit "$RC_DATA"
    fi
fi

# Scorer stayed in place (no whitelist move); evaluation/ was chmod 700 in Phase 3a
# and root reads it fine for scoring — nothing to restore.

# ─── 3c: Re-run with --skip-agent to score (build + eval only). ──────────────
# The scorer (eval) runs IN-PROCESS as ROOT, so it reads the answers (root-0700) and the
# agent-owned workspace fine. KEEP the setpriv prefix set: the re-build here
# (build_agent_output → npm install / npm run build) executes agent-authored code
# (vite.config, package.json postinstall), so it must stay NON-ROOT — otherwise
# agent-controlled build code would run as root with the answer key already on disk (a cheat
# vector). Only the build subprocess is wrapped by the prefix; the in-process scorer and the
# root SiteServer are unaffected.
cd "${RB_WEB_DIR}"
RC=0
python3 -m runner.run_agent \
    --dataset "${DATA_DIR}" \
    --domain  "${DOMAIN}" \
    --model   "${MODEL}" \
    --workspace "${WORKSPACE_DIR}" \
    --browser-mcp "${BROWSER_MCP}" \
    --skip-agent \
    ${VLM_JUDGE_ARGS[@]+"${VLM_JUDGE_ARGS[@]}"} \
    > "${OUTPUT_DIR}/run.json" || RC=$?

if [ ${RC} -ne 0 ]; then
    echo "[mockweb] FATAL: eval phase failed before producing a valid score (exit=${RC})" >&2
    if [ "$RC" -eq 130 ] || [ "$RC" -eq "$RC_TIMEOUT" ]; then
        RC=$RC_TIMEOUT
    else
        RC=$RC_INFRA
    fi
fi
fi
fi

###############################################################################
# Phase 4: Convert run.json → metrics.json
###############################################################################
echo "[mockweb] Phase 4: Writing metrics.json..."
PYTHONPATH="${RB_SCRIPTS_DIR}" python3 "${RB_SCRIPTS_DIR}/web/runtime.py" \
    write-metrics \
    --output-dir "${OUTPUT_DIR}" \
    --agent-rc "${AGENT_RC:-0}" \
    --eval-target "${RB_EVAL_TARGET}" \
    --scaffold "${SCAFFOLD:-claude-code}"

###############################################################################
# Phase 5: Slim workspace before artifact upload
###############################################################################
# Self-describing artifact: "where is the recreated app" had five answers at three depths across
# the platforms, and web's does not even use the word recreation -- the workspace IS the recreated
# site. The artifact store keys cannot be renamed without orphaning everything stored, so the artifact
# carries a pointer from the ONE shared definition (core/recreation_artifact.py).
if [ "$RB_EVAL_TARGET" != reference ] && [ -d "${WORKSPACE_DIR}" ]; then
  ( if [ -n "${RB_SCRIPTS_DIR:-}" ]; then
      python3 -m core.recreation_artifact write \
        --dest "${WORKSPACE_DIR}" --platform web --root . 2>&1
    fi ) || echo "[mockweb] WARN: recreation manifest not written (run unaffected)"

  echo "[mockweb] Phase 5: Removing bulky build artifacts from workspace..."
  rm -rf "${WORKSPACE_DIR}/node_modules" \
         "${WORKSPACE_DIR}/dist" \
         "${WORKSPACE_DIR}/.npm" \
         "${WORKSPACE_DIR}/.cache"

  rm -rf "${ARTIFACT_DIR}"
  cp -a "${WORKSPACE_DIR}" "${ARTIFACT_DIR}"
  _TOOL_CAPTURE_SRC="$(dirname "${WORKSPACE_DIR}")/.$(basename "${WORKSPACE_DIR}")-tool-use-screenshots"
  if [ -d "${_TOOL_CAPTURE_SRC}" ]; then
    cp -a "${_TOOL_CAPTURE_SRC}" "${ARTIFACT_DIR}/tool_use_screenshots"
  fi
  echo "[mockweb]   recreation artifact staged: ${ARTIFACT_DIR}"
  # Preserve the stage-owned copy and expose the same root-level trajectory surface as the desktop
  # and Android jobs. This also copies native Claude sessions / Codex rollouts to sessions/.
  if [ -n "${RB_SCRIPTS_DIR:-}" ]; then
    PYTHONPATH="${RB_SCRIPTS_DIR}" python3 -m core.trajectory surface \
      --stage-dir "${ARTIFACT_DIR}" --output-dir "${OUTPUT_DIR}" --stage recreation || \
      echo "[mockweb] WARN: canonical trajectory surface failed (run unaffected)"
  fi
fi
# Preserve any deployment-provided raw model exchange logs from the shared
# diagnostics directory. Best-effort subshell: must never abort the worker.
( _SHARED_DIR="${RB_SHARED_DIR}"
  _PAIR_LOG_SRC="${_SHARED_DIR}/logs/claudecode"
  echo "[mockweb] Diag: SHARED_DIR=${_SHARED_DIR}"
  ls -la "${_SHARED_DIR}" 2>&1 | head -20 || true
  if [ -d "${_PAIR_LOG_SRC}" ]; then
    _pair_count=$(find "${_PAIR_LOG_SRC}" -maxdepth 1 -name '*.json' | wc -l)
    if [ "${_pair_count}" -gt 0 ]; then
      mkdir -p "${OUTPUT_DIR}/logs/proxy_logs"
      cp -a "${_PAIR_LOG_SRC}"/*.json "${OUTPUT_DIR}/logs/proxy_logs/" 2>/dev/null || true
      _staged=$(find "${OUTPUT_DIR}/logs/proxy_logs" -maxdepth 1 -name '*.json' | wc -l)
      echo "[mockweb] Staged ${_staged}/${_pair_count} proxy pair logs → logs/proxy_logs/ ($(du -sh "${OUTPUT_DIR}/logs/proxy_logs" | awk '{print $1}'))"
    else
      echo "[mockweb] No *.json pair logs in ${_PAIR_LOG_SRC} (pair logging off or no completed calls)"
    fi
  else
    echo "[mockweb] No proxy pair-log dir at ${_PAIR_LOG_SRC}"
  fi
) || echo "[mockweb] WARN: pair-log staging errored (run unaffected)"
echo "[mockweb] Done. Output dir contents:"
ls -la "${OUTPUT_DIR}"
exit "$RC"
