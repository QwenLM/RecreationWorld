#!/usr/bin/env bash
# RecreationBench macOS target launcher
# 用法: ./run.sh --model MODEL --prompt-file FILE [OPTIONS]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Ensure common bin dirs are in PATH (SSH non-interactive sessions have minimal PATH)
export PATH="$HOME/.local/bin:$HOME/local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

# ── Defaults ─────────────────────────────────────────────────────────────────

MODEL=""
PROMPT=""
PROMPT_FILE=""
INTERACTIVE=false
CC_SESSION_ID=""
CC_RESUME_ID=""
API_KEY="${ANTHROPIC_API_KEY:-}"
BASE_URL="${ANTHROPIC_BASE_URL:-}"
START_CHROMIUM=false
ENABLE_CUA=true
TIMEOUT_SEC=""
MAX_TURNS=""
AGENTS_JSON=""
OUTPUT_FILE=""
ALLOWED_TOOLS=""
DISALLOWED_TOOLS=""

CLAUDE_CODE_EFFORT_LEVEL="${CLAUDE_CODE_EFFORT_LEVEL:-}"
CLAUDE_CODE_DISABLE_THINKING="${CLAUDE_CODE_DISABLE_THINKING:-}"

# ── Parse args ───────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case $1 in
        --model)                    MODEL="$2"; shift 2 ;;
        --prompt|-p)                PROMPT="$2"; shift 2 ;;
        --prompt-file)              PROMPT_FILE="$2"; shift 2 ;;
        --api-key)                  API_KEY="$2"; shift 2 ;;
        --base-url)                 BASE_URL="$2"; shift 2 ;;
        --start-chromium)           START_CHROMIUM=true; shift ;;
        --no-cua-driver)            ENABLE_CUA=false; shift ;;
        --no-computer-use)          ENABLE_CUA=false; shift ;;
        --timeout)                  TIMEOUT_SEC="$2"; shift 2 ;;
        --max-turns)                MAX_TURNS="$2"; shift 2 ;;
        --agents)                   AGENTS_JSON="$2"; shift 2 ;;
        --output-file)              OUTPUT_FILE="$2"; shift 2 ;;
        --allowed-tools)            ALLOWED_TOOLS="$2"; shift 2 ;;
        --disallowed-tools)         DISALLOWED_TOOLS="$2"; shift 2 ;;
        --session-id)               CC_SESSION_ID="$2"; shift 2 ;;
        --resume)                   CC_RESUME_ID="$2"; shift 2 ;;
        --effort)                   CLAUDE_CODE_EFFORT_LEVEL="$2"; shift 2 ;;
        --disable-thinking)         CLAUDE_CODE_DISABLE_THINKING="1"; shift ;;
        --interactive|-i)           INTERACTIVE=true; shift ;;
        *) echo "Unknown option: $1"; exit 2 ;;
    esac
done

if [ -z "$MODEL" ]; then
    echo "Usage: ./run.sh --model MODEL --prompt 'TASK' [OPTIONS]"
    echo "       ./run.sh --model MODEL --interactive"
    echo ""
    echo "Required:"
    echo "  --model NAME            Model name (e.g. qwen3.6-plus, claude-sonnet-4-20250514)"
    echo "  --prompt TEXT | -i      Task prompt, or --interactive for REPL mode"
    echo ""
    echo "API connection (or set via env vars ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL):"
    echo "  --api-key KEY           API key"
    echo "  --base-url URL          API base URL (must NOT end with /v1)"
    echo ""
    echo "Environment:"
    echo "  --start-chromium         Start Chromium/Chrome before running"
    echo ""
    echo "MCP tools:"
    echo "  --no-cua-driver          Disable CUA Driver MCP (desktop-control)"
    echo "  --no-computer-use        Alias for --no-cua-driver"
    echo "  --timeout N              Kill agent after N seconds (0 = no limit)"
    echo "  --max-turns N            Max agent turns (default: unlimited)"
    echo ""
    echo "Claude Code native:"
    echo "  --effort LEVEL           Effort level: low/medium/high/max"
    echo "  --disable-thinking       Disable thinking entirely"
    exit 2
fi

# ── Validate API credentials ────────────────────────────────────────────────

if [ -z "$API_KEY" ]; then
    echo "ERROR: API key not set. Use --api-key or export ANTHROPIC_API_KEY"
    exit 2
fi
if [ -z "$BASE_URL" ]; then
    echo "ERROR: Base URL not set. Use --base-url or export ANTHROPIC_BASE_URL"
    exit 2
fi

# Token hub keys (th-*) require Authorization: Bearer header, not x-api-key.
# Always unset the counterpart credential so a stray inherited value (e.g. a real
# ANTHROPIC_AUTH_TOKEN in the environment) can never leak past a dummy api-key.
if [[ "$API_KEY" == th-* ]]; then
    export ANTHROPIC_AUTH_TOKEN="$API_KEY"
    unset ANTHROPIC_API_KEY 2>/dev/null || true
else
    export ANTHROPIC_API_KEY="$API_KEY"
    unset ANTHROPIC_AUTH_TOKEN 2>/dev/null || true
fi
export ANTHROPIC_BASE_URL="$BASE_URL"

PIDS_TO_KILL=()
TEMP_FILES=()
cleanup() {
    for pid in "${PIDS_TO_KILL[@]+"${PIDS_TO_KILL[@]}"}"; do
        kill "$pid" 2>/dev/null || true
    done
    for path in "${TEMP_FILES[@]+"${TEMP_FILES[@]}"}"; do
        rm -f "$path" 2>/dev/null || true
    done
}
trap cleanup EXIT

# ── Chromium ─────────────────────────────────────────────────────────────────

if $START_CHROMIUM; then
    echo "[*] Starting Chromium..."
    if command -v chromium &>/dev/null; then
        chromium --start-maximized >/dev/null 2>&1 &
    elif [ -d "/Applications/Chromium.app" ]; then
        open -a Chromium
    elif [ -d "/Applications/Google Chrome.app" ]; then
        open -a "Google Chrome"
    else
        echo "WARNING: No Chromium/Chrome found, skipping."
    fi
    sleep 1
fi

# ── Verify canonical MCP configuration ────────────────────────────────────

# recreation.sh owns the Claude config; the deployment platform controller owns the Codex config. This launcher
# validates those canonical files but never rewrites them.
if $ENABLE_CUA; then
    CUA_DRIVER_BIN="$(command -v qwen-cua-driver 2>/dev/null || true)"
    if [ -z "$CUA_DRIVER_BIN" ]; then
        echo "FATAL: qwen-cua-driver not found in PATH."
        exit 2
    fi
    echo "[*] qwen-cua-driver found: $CUA_DRIVER_BIN"
else
    echo "[*] CUA Driver MCP disabled"
fi

# ── Verify MCP registration ─────────────────────────────────────────────────

if $ENABLE_CUA; then
    case "${AGENT_CLI:-claude}" in
      codex*)
        echo "[*] codex CLI version (MCP preflight): $(codex --version 2>/dev/null || echo missing)"
        if grep -qF "[mcp_servers.desktop-control]" ~/.codex/config.toml 2>/dev/null \
                && grep -qF -- "--socket" ~/.codex/config.toml 2>/dev/null \
                && grep -qF -- "--no-overlay" ~/.codex/config.toml 2>/dev/null \
                && grep -qF "CUA_DRIVER_RS_MCP_FORCE_PROXY" ~/.codex/config.toml 2>/dev/null; then
            echo "[*] Codex MCP verified: desktop-control (qwen-cua-driver) registered"
        else
            echo "FATAL: canonical desktop-control MCP missing or incomplete in ~/.codex/config.toml"
            exit 2
        fi
        ;;
      *)
        if ! python3 "$SCRIPT_DIR/../runtime.py" verify-claude-mcp \
            "$HOME/.claude.json" --socket "${CUA_DAEMON_SOCK:-}"
        then
            echo "FATAL: canonical desktop-control MCP missing or incomplete in ~/.claude.json"
            exit 2
        fi
        echo "[*] Claude MCP verified: desktop-control (qwen-cua-driver) registered"
        ;;
    esac

    # Pre-warm CUA Driver MCP
    echo "[*] Pre-warming CUA Driver MCP..."
    if [ -z "${CUA_DAEMON_SOCK:-}" ] || [ ! -S "$CUA_DAEMON_SOCK" ]; then
        echo "  FATAL: canonical CUA daemon socket is unavailable"
        exit 2
    fi
    CUA_WARM_OUT=$(printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"warmup","version":"1.0"}}}\n' \
        | timeout 10 "$CUA_DRIVER_BIN" mcp --socket "$CUA_DAEMON_SOCK" --no-overlay 2>/dev/null | head -1 || true)
    if echo "$CUA_WARM_OUT" | grep -q '"result"' 2>/dev/null; then
        echo "  CUA Driver MCP: warm (initialize OK)"
    else
        echo "  FATAL: CUA Driver MCP pre-warm failed"
        exit 2
    fi
fi

# ── Run the shared invocation contract ───────────────────────────────────────

[ -z "$CLAUDE_CODE_DISABLE_THINKING" ] || export CLAUDE_CODE_DISABLE_THINKING
if [ "${CLAUDE_CODE_AUTO_COMPACT_WINDOW+set}" = set ]; then
    export CLAUDE_CODE_AUTO_COMPACT_WINDOW
fi
AGENT_CLI="${AGENT_CLI:-claude}"
case "$AGENT_CLI" in
  codex*)
    if ! $ENABLE_CUA; then
        echo "ERROR: Codex desktop recreation requires the desktop-control MCP" >&2
        exit 2
    fi
    CODEX_WANT="${SCAFFOLD_VERSION:-0.145.0}"
    CODEX_HAVE="$(codex --version 2>/dev/null | awk '{print $NF}' || true)"
    [ "$CODEX_HAVE" = "$CODEX_WANT" ] || {
        echo "ERROR: codex CLI version ${CODEX_HAVE:-missing}; expected ${CODEX_WANT}" >&2
        exit 2
    }
    echo "[*] codex CLI version: $CODEX_HAVE"
    [ -s "$HOME/.codex/config.toml" ] || {
        echo "ERROR: ~/.codex/config.toml missing — the controller did not deploy it" >&2
        exit 2
    }
    echo "[*] Starting codex (config: $HOME/.codex/config.toml)..."
    ;;
  *)
    echo "[*] Starting Claude Code (model: $MODEL)..."
    ;;
esac

INVOCATION_RUNNER="${RB_AGENT_INVOCATION_RUNNER:-$SCRIPT_DIR/agent_invocation.py}"
[ -r "$INVOCATION_RUNNER" ] || {
    echo "FATAL: shared agent invocation runner missing: $INVOCATION_RUNNER" >&2
    exit 2
}
if [ "$INTERACTIVE" = true ]; then
    echo "[*] Interactive mode (outside the benchmark invocation contract)"
    case "$AGENT_CLI" in codex*) exec codex ;; *) exec claude --model "$MODEL" ;; esac
fi
if [ -z "$PROMPT_FILE" ]; then
    [ -n "$PROMPT" ] || { echo "ERROR: --prompt-file is required" >&2; exit 2; }
    PROMPT_FILE="$(mktemp /tmp/rb-prompt.XXXXXX)"
    printf '%s' "$PROMPT" > "$PROMPT_FILE"
    TEMP_FILES+=("$PROMPT_FILE")
fi
[ -r "$PROMPT_FILE" ] || { echo "ERROR: unreadable prompt file: $PROMPT_FILE" >&2; exit 2; }
OUTPUT_FILE="${OUTPUT_FILE:-${TMPDIR:-/tmp}/rb-agent-trajectory.$$}"
STDERR_FILE="$(dirname "$OUTPUT_FILE")/stderr.log"
SPEC_FILE="${TMPDIR:-/tmp}/rb-agent-invocation.$$.json"
CREATE_ARGS=(create --output "$SPEC_FILE" --agent-cli "$AGENT_CLI" --model "$MODEL"
    --prompt-file "$PROMPT_FILE" --workspace "$PWD" --trajectory "$OUTPUT_FILE"
    --stderr "$STDERR_FILE" --timeout "${TIMEOUT_SEC:-72000}"
    --request-timeout-ms "$(( ${API_TIMEOUT:-1800} * 1000 ))"
    --tool-timeout-ms "${MCP_TOOL_TIMEOUT:-180000}" --run-name rb-recreation)
case "${CONTEXT_1M:-false}" in true|1|yes|on) CREATE_ARGS+=(--context-1m) ;; esac
[ -z "$CLAUDE_CODE_EFFORT_LEVEL" ] || CREATE_ARGS+=(--reasoning-effort "$CLAUDE_CODE_EFFORT_LEVEL")
[ -z "$MAX_TURNS" ] || CREATE_ARGS+=(--max-turns "$MAX_TURNS")
[ -z "$CC_SESSION_ID" ] || CREATE_ARGS+=(--session-id "$CC_SESSION_ID")
if [ "${RB_CAPTURE_TOOL_USE_SCREENSHOTS:-false}" = "true" ]; then
    CREATE_ARGS+=(
        --capture-tool-use-screenshots
        --screenshot-dir "${RB_TOOL_USE_SCREENSHOT_DIR:-$(dirname "$OUTPUT_FILE")/tool_use_screenshots}"
        --screenshot-command /usr/sbin/screencapture
        --screenshot-command=-x
        --screenshot-command '{output}'
    )
fi
if [[ "$AGENT_CLI" != codex* ]] && [ -s "$HOME/.claude.json" ]; then
    CREATE_ARGS+=(--mcp-config "$HOME/.claude.json")
fi
if [[ "$AGENT_CLI" != codex* ]]; then
    [ -z "$AGENTS_JSON" ] || CREATE_ARGS+=(--extra-arg=--agents --extra-arg "$AGENTS_JSON")
    if [ -n "$ALLOWED_TOOLS" ]; then
        CREATE_ARGS+=(--extra-arg=--allowedTools)
        for tool in $ALLOWED_TOOLS; do CREATE_ARGS+=(--extra-arg "$tool"); done
    fi
    if [ -n "$DISALLOWED_TOOLS" ]; then
        CREATE_ARGS+=(--extra-arg=--disallowedTools)
        for tool in $DISALLOWED_TOOLS; do CREATE_ARGS+=(--extra-arg "$tool"); done
    fi
fi
if [ -n "$CC_RESUME_ID" ]; then
    CREATE_ARGS+=(--session-mode resume --session-id "$CC_RESUME_ID")
fi

python3 "$INVOCATION_RUNNER" "${CREATE_ARGS[@]}"
echo "[*] Trajectory: $OUTPUT_FILE"
AGENT_RC=0
python3 "$INVOCATION_RUNNER" run --spec "$SPEC_FILE" || AGENT_RC=$?
rm -f "$SPEC_FILE"
case "$AGENT_RC" in
    0|124)   exit "$AGENT_RC" ;;
    130|143) exit 143 ;;
    *)       exit 2 ;;
esac
