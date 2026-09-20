#!/bin/bash
set -e
trap 'rc=$?; echo "ERROR: stage_recreation failed at line $LINENO (rc=$rc)" >&2; exit 2' ERR
REC_DIR="__REC_DIR__"
BASE="__BASE__"
RB_CODE_DIR="__RB_CODE_DIR__"
MODEL="__MODEL__"
CLAUDE_MODEL="__CLAUDE_MODEL__"
API_KEY="__API_KEY__"
AUTH_TOKEN="__AUTH_TOKEN__"
BASE_URL="__BASE_URL__"
SETTINGS="__SETTINGS__"
RUN_ID="__RUN_ID__"
AGENT_CLI="__AGENT_CLI__"
RB_PHASE="${1:-all}"
# RB_PREFLIGHT is used by BOTH the agent phase (cua preflight log) and the root
# phase; define it up-front (REC_DIR is set above) so the agent phase doesn't
# `echo ... >> "$RB_PREFLIGHT"` with an empty path -> "No such file or directory",
# which under `set -e` + the ERR trap kills every recreation job at the preflight.
RB_PREFLIGHT="$REC_DIR/recreation_preflight.log"

# ════════════════════════════════════════════════════════════════════
# Phase: agent — runs as user, launches reference app + Claude Code
# ════════════════════════════════════════════════════════════════════
if [ "$RB_PHASE" = "agent" ]; then
    export HOME=/home/user
    export USER=user
    unset INSTANCE_ID TASK_ID RB_UNIFIED_PREFIX RB_UNIFIED_REFERENCE_PATH
    unset RB_UNIFIED_TESTS_PATH RB_UNIFIED_INSTANCE_PATH
    [ -f /tmp/.rb_bootstrap_env ] && . /tmp/.rb_bootstrap_env 2>/dev/null || true
    export DISPLAY="${DISPLAY:-:99}"
    export GTK_MODULES=gail:atk-bridge
    export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
    export QT_ACCESSIBILITY=1
    export ELECTRON_ENABLE_ACCESSIBILITY=1
    export RUST_BACKTRACE=1
    # 1M context window: the [1m] suffix makes Claude Code send the
    # context-1m beta header (client-side 1M window for auto-compaction math).
    case "$AGENT_CLI" in
        codex*) CLIENT_MODEL="$MODEL" ;;
        *) CLIENT_MODEL="$CLAUDE_MODEL" ;;
    esac
    if [ "$AGENT_CLI" != "codex" ] && [ "${RB_CONTEXT_1M:-false}" = "true" ]; then
        case "$CLIENT_MODEL" in *'[1m]') ;; *) CLIENT_MODEL="${CLIENT_MODEL}[1m]";; esac
    fi
    export ANTHROPIC_API_KEY="$API_KEY"
    export ANTHROPIC_AUTH_TOKEN="$AUTH_TOKEN"
    export ANTHROPIC_BASE_URL="$BASE_URL"
    export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
    export no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
    export ANTHROPIC_MODEL="$CLIENT_MODEL"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLIENT_MODEL"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="$CLIENT_MODEL"

    source /tmp/rb_runtime_helpers.sh
    RB_PREFLIGHT="$REC_DIR/recreation_preflight.log"

    # --- Reference app already launched as ref_user by root phase ---
    REF_PID=$(cat /tmp/.rb_ref_pid 2>/dev/null || echo "")
    if [ -z "$REF_PID" ] || ! ps -p "$REF_PID" >/dev/null 2>&1; then
        echo 'ERROR: Reference app (ref_user) not running'
        exit 2
    fi
    echo "Reference app running as ref_user (PID=$REF_PID)"
    if cua_mcp_preflight "Recreation reference" "$REF_PID"; then
        echo "cua_preflight=pass" >> "$RB_PREFLIGHT"
    else
        echo "cua_preflight=fail" >> "$RB_PREFLIGHT"
        if [ "${RB_CUA_PREFLIGHT_MODE:-strict}" = "warn" ]; then
            echo "WARNING: CUA preflight failed; continuing because RB_CUA_PREFLIGHT_MODE=warn"
        else
            exit 2
        fi
    fi

    # --- Model proxy (loopback) ---
    AGENT_BASE_URL="$ANTHROPIC_BASE_URL"
    CODEX_PROXY_BASE_URL=""
    MODEL_PROXY_PID=""
    AGENT_NATIVE_RC=0
    if [ "$AGENT_CLI" = "codex" ]; then
        # codex's model proxy runs as ROOT (started in the root phase before the su
        # drop) so its egress bypasses the per-uid isolation. The isolated user's
        # connection to a remote gateway — direct or relayed — can get intermittent
        # RST/"Connection refused" when its load balancer rotates IPs and
        # the egress allowlist is IP-pinned at setup; codex exhausts its 5 reconnects
        # → rc=1. Root egress is unrestricted → reliable. codex talks to the loopback
        # proxy (127.0.0.1:19778, loopback always allowed). VM-validated 2026-07-23:
        # root-proxy 3/3 clean rc=0; isolated-user-proxy = intermittent RST.
        CODEX_PROXY_BASE_URL="http://127.0.0.1:19778"
    elif [[ "$ANTHROPIC_BASE_URL" != http://127.0.0.1* && "$ANTHROPIC_BASE_URL" != http://localhost* ]]; then
        export UPSTREAM_BASE="$ANTHROPIC_BASE_URL"
        export LOCAL_MODEL_PROXY_PORT=19778
        python3 /tmp/rb_model_proxy.py > /tmp/rb_model_proxy.out 2> /tmp/rb_model_proxy.err &
        MODEL_PROXY_PID=$!
        AGENT_BASE_URL="http://127.0.0.1:$LOCAL_MODEL_PROXY_PORT"
        sleep 1
        echo "Model proxy: $AGENT_BASE_URL -> $UPSTREAM_BASE"
    fi
    export ANTHROPIC_BASE_URL="$AGENT_BASE_URL"

    if [ x${ANTHROPIC_AUTH_TOKEN:-} = x ]; then
        unset ANTHROPIC_AUTH_TOKEN
    fi

    mkdir -p "$HOME/.claude"
    echo $SETTINGS | base64 -d > "$HOME/.claude/settings.json"
    echo __MCP_CONFIG_B64__ | base64 -d > "$HOME/mcp_config.json"
    # Echoed HERE, not 34 lines earlier where it used to sit: there the file did not
    # exist yet, so `cat` failed into an empty string and this diagnostic printed nothing
    # for its whole life. It is the only place a run shows which server/args/daemon env the
    # agent was actually given -- passed via --mcp-config, so it is not in ~/.claude.json.
    echo "MCP config: $(cat "$HOME/mcp_config.json" 2>/dev/null)"
    XDG_CONFIG_HOME=/home/user/.config
    XDG_CACHE_HOME=/home/user/.cache
    XDG_DATA_HOME=/home/user/.local/share
    mkdir -p $XDG_CONFIG_HOME $XDG_CACHE_HOME $XDG_DATA_HOME
    export XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME

    cd /workspace/recreation
    : > /workspace/output/trajectory.jsonl

    if [ "$AGENT_CLI" = "codex" ]; then
        # ── Codex CLI setup; invocation/retry policy is shared below ──
        echo "Using Codex CLI agent"
        mkdir -p "$HOME/.codex"
        echo __CODEX_CONFIG_B64__ | base64 -d > "$HOME/.codex/config.toml"
        # Point Codex at the loopback model relay (started above) rather than sending
        # Responses requests directly from the network-restricted account.
        if [ -n "${CODEX_PROXY_BASE_URL:-}" ]; then
            sed -i "s#^base_url = .*#base_url = \"$CODEX_PROXY_BASE_URL\"#" "$HOME/.codex/config.toml"
            echo "codex: base_url -> loopback model proxy ($CODEX_PROXY_BASE_URL)"
        fi
        # codex spawns config.toml MCP servers with a SANITIZED env (~7 vars: HOME,
        # LANG, PATH, PWD, SHLVL, TERM, _) — no DISPLAY/DBUS/AT-SPI. So the cua-driver
        # desktop-control server would see 0 windows / 0 a11y elements, unlike claude
        # whose MCP server inherits the full agent env. Replicate that env in the codex
        # config so every codex run can actually observe the reference app.
        grep -qF "[mcp_servers.desktop-control]" "$HOME/.codex/config.toml" || {
            echo "FATAL: Codex config does not contain desktop-control MCP" >&2
            exit 2
        }
        [ -n "${AT_SPI_BUS_ADDRESS:-}" ] || export AT_SPI_BUS_ADDRESS="$(xprop -root AT_SPI_BUS 2>/dev/null | sed -E 's/.*= "([^"]*)".*/\1/')"
        RB_MCP_ENV=""
        for _k in DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS AT_SPI_BUS_ADDRESS XDG_RUNTIME_DIR XDG_DATA_DIRS GTK_MODULES QT_ACCESSIBILITY QT_LINUX_ACCESSIBILITY_ALWAYS_ON ELECTRON_ENABLE_ACCESSIBILITY GSK_RENDERER LIBGL_ALWAYS_SOFTWARE; do
            # Most of these desktop variables are optional.  ``printenv`` returns 1
            # when one is absent, which would otherwise trip the stage's ``set -e``
            # before Codex gets a chance to start.
            _v="$(printenv "$_k" 2>/dev/null || true)"
            [ -n "$_v" ] && RB_MCP_ENV="${RB_MCP_ENV}${RB_MCP_ENV:+, }$_k = \"$_v\""
        done
        echo "env = { $RB_MCP_ENV }" >> "$HOME/.codex/config.toml"
        echo "codex: verified desktop-control MCP and injected runtime env (DISPLAY=$DISPLAY AT_SPI_BUS_ADDRESS=${AT_SPI_BUS_ADDRESS:+set})"
        export OPENAI_API_KEY="$API_KEY"
        export CODEX_THREAD_ID="rb-recreation-${RUN_ID:-codex}"

        # Codex requires a git repo
        if [ ! -d .git ]; then
            git init -q
            git config user.email "rb@bench" && git config user.name "rb"
            git add -A && git commit -q -m "init" --allow-empty 2>/dev/null || true
        fi

    else
        # ── Claude Code invocation ──
        log_claude_info
        # Per-request retry is centralized in the model proxy. A second client-side loop
        # would compound the attempt budget and make Claude/Codex incomparable.
        export CLAUDE_CODE_MAX_RETRIES=0
        export MAX_STRUCTURED_OUTPUT_RETRIES=0
        export CLAUDE_CODE_MAX_OUTPUT_TOKENS=${RB_MAX_OUTPUT_TOKENS:-128000}
        # One recoverable model request gets 30 minutes on every platform. This is separate
        # from the 20h agent wall clock and the 180s per-MCP-call bound below.
        export API_TIMEOUT_MS=${RB_API_TIMEOUT_MS:-1800000}
        # Bound every MCP tool call: an unbounded hung get_window_state has eaten
        # most transient tool failures, but an unbounded call can consume the job budget. The agent survives
        # a timeout and reports it, so this costs nothing when nothing hangs.
        __MCP_TOOL_TIMEOUT_EXPORT__
        echo "MCP_TOOL_TIMEOUT=${MCP_TOOL_TIMEOUT:-UNSET}"
        # Auto-compaction trigger: Claude Code compacts when context nears this
        # many tokens (effective trigger sits ~15-33k below, depending on effort).
        if [ "${RB_AUTO_COMPACT_WINDOW:-0}" -gt 0 ] 2>/dev/null; then
            export CLAUDE_CODE_AUTO_COMPACT_WINDOW="$RB_AUTO_COMPACT_WINDOW"
        fi
        # Extra request-body JSON: Claude Code merges CLAUDE_CODE_EXTRA_BODY into
        # every /v1/messages body. Whether custom fields reach the model depends on
        # the configured compatible gateway.
        if [ -n "${RB_EXTRA_BODY:-}" ]; then
            export CLAUDE_CODE_EXTRA_BODY="$RB_EXTRA_BODY"
            echo "RB_EXTRA_BODY set: CLAUDE_CODE_EXTRA_BODY injected (${#RB_EXTRA_BODY} chars)"
        fi
        echo "RB_COMPACT_DEBUG model=$MODEL auto_compact_window=${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-unset} effort=${RB_THINKING_EFFORT:-high}"
    fi

    # The shared runner owns prompt stdin, canonical argv, stream files and the one
    # absolute wall-clock budget. Platform code above owns only Linux setup/isolation.
    set +e
    trap - ERR
    python3 /tmp/rb_agent_invocation.py run --spec /tmp/rb_invocation.json
    AGENT_NATIVE_RC=$?
    set -e
    trap 'rc=$?; echo "ERROR: stage_recreation failed at line $LINENO (rc=$rc)" >&2; exit 2' ERR
    if [ "$AGENT_CLI" = codex ] && [ ! -f /workspace/recreation/build.sh ]; then
        echo "=== CODEX STDERR (no build.sh) ==="
        tail -c 30000 /workspace/output/stderr.log 2>/dev/null || echo "(no stderr.log)"
        echo "=== CODEX TRAJECTORY tail ==="
        tail -c 20000 /workspace/output/trajectory.jsonl 2>/dev/null || echo "(no trajectory.jsonl)"
        echo "=== END CODEX DIAGNOSTICS ==="
    fi

    # Agent has finished; from here only the contract verify (below) decides success.
    # The post-run harvest/reporting writes heavily to the SSH stdout channel and can
    # take a SIGPIPE (exit 141) under `set -e`, which would mis-mark an already-complete
    # recreation as Failed. Make wrap-up non-fatal; STATUS is set explicitly by verify.
    set +e
    trap - ERR

    strip_trajectory_base64
    audit_reference_reads
    # Unified transcript+stream collector (core/trajectory.py; non-fatal because set +e
    # is active above). The module is transferred as a normal file before the agent starts.
__COLLECT_SESSIONS__
    # Write the shared artifact descriptor at the canonical app root. It is rendered by the pod
    # from core/recreation_artifact.py so there is no second definition of the JSON on the VM.
__WRITE_REC_MANIFEST__
    echo "=== CUA-DRIVER MCP LOG ==="
    cat /tmp/cua-driver-mcp.log 2>/dev/null | tail -10
    echo "=== END CUA ==="
    echo "=== INPUT PROXY LOG ==="
    cat /tmp/rb_input_proxy.log 2>/dev/null | tail -40
    echo "=== END INPUT PROXY ==="

    STATUS=0
    python3 /tmp/rb_recreation_contract.py /workspace/recreation || STATUS=1
    # The agent principal is intentionally denied access to RB_CODE_DIR. Use the
    # isolated copies shipped below for both execution and terminal parsing; importing
    # from RB_CODE_DIR here makes every terminal verdict fall back to infra_error.
    AGENT_VERDICT=$(PYTHONPATH=/tmp python3 /tmp/rb_agent_invocation.py terminal \
        --trajectory /workspace/output/trajectory.jsonl --native-rc "$AGENT_NATIVE_RC" \
        --field status 2>/dev/null || echo infra_error)
    AGENT_VERDICT_RC=$(PYTHONPATH=/tmp python3 /tmp/rb_agent_invocation.py terminal \
        --trajectory /workspace/output/trajectory.jsonl --native-rc "$AGENT_NATIVE_RC" \
        --field exit-code 2>/dev/null || echo 2)
    if [ -n "$MODEL_PROXY_PID" ]; then kill $MODEL_PROXY_PID 2>/dev/null || true; fi

    echo "Recreation done. Files: $(find /workspace/recreation -type f 2>/dev/null | wc -l)"
    echo "Agent verdict: $AGENT_VERDICT (native rc=$AGENT_NATIVE_RC)"
    # External termination always wins.  A terminal API/stream error does not:
    # the model may already have produced a complete, syntax-valid candidate
    # before its final request failed.  In that case the evaluator is the
    # authoritative judge and discarding the candidate creates a false infra
    # retry.  Only propagate infra when the artifact contract also failed.
    [ "$AGENT_VERDICT_RC" = 143 ] && exit 143
    if [ "$STATUS" -eq 0 ]; then
        if [ "$AGENT_VERDICT_RC" = 2 ]; then
            echo "WARN: terminal agent error occurred after a usable recreation was produced; continuing to eval"
        fi
        exit 0
    fi
    [ "$AGENT_VERDICT_RC" = 2 ] && exit 2
    exit 1
fi

# Elevate to root (needed for useradd, iptables, chmod, /etc/hosts)
if [ "$(id -u)" -ne 0 ]; then
    if sudo -n true 2>/dev/null; then
        exec sudo -E bash "$0" "$@"
    elif [ -n "${RB_SUDO_PASSWORD:-}" ]; then
        echo "$RB_SUDO_PASSWORD" | sudo -S -E bash "$0" "$@"
        exit $?
    else
        echo "ERROR: recreation stage needs root but no sudo access"
        exit 2
    fi
fi

restore_system_guards() {
    sed -i '/# rb-recreation-network-block$/d' /etc/hosts 2>/dev/null || true
    iptables -F OUTPUT 2>/dev/null || true
    ip6tables -F OUTPUT 2>/dev/null || true
}

# Linux shares the desktop session's `user` account with the recreation agent, so killing the
# whole UID would also tear down the GUI session.  Record the pre-existing processes (PID plus
# kernel start time), then kill only processes created during this agent invocation.  Detached
# children cannot escape this accounting by changing their process group or parent.
snapshot_desktop_user_processes() {
    local _uid _proc _pid _stat _rest _state
    _uid="$(id -u user)" || return 1
    for _proc in /proc/[0-9]*; do
        [ "$(stat -c '%u' "$_proc" 2>/dev/null || true)" = "$_uid" ] || continue
        _pid="${_proc##*/}"
        _stat="$(cat "$_proc/stat" 2>/dev/null || true)"
        [ -n "$_stat" ] || continue
        _rest="${_stat##*) }"
        set -- $_rest
        [ "$#" -ge 20 ] || continue
        # A dead child can remain visible in /proc as Z until its parent (often
        # the desktop session's PID 1) reaps it.  It cannot execute or mutate
        # the workspace, and SIGKILL cannot remove it.  Exclude only zombies;
        # D/S/R/T/etc. processes remain part of the isolation boundary.
        _state="$1"
        [ "$_state" = Z ] && continue
        shift 19
        printf '%s %s\n' "$_pid" "$1"
    done | sort -n
}

stop_new_desktop_user_processes() {
    local _baseline="$1" _current="/tmp/rb_user_processes_after.$$"
    local _attempt _pid _started _remaining=""
    for _attempt in 1 2 3 4; do
        snapshot_desktop_user_processes > "$_current" || return 1
        while read -r _pid _started; do
            [ -n "$_pid" ] || continue
            grep -qxF "$_pid $_started" "$_baseline" || kill -KILL "$_pid" 2>/dev/null || true
        done < "$_current"
        sleep 1
    done
    snapshot_desktop_user_processes > "$_current" || return 1
    while read -r _pid _started; do
        [ -n "$_pid" ] || continue
        if ! grep -qxF "$_pid $_started" "$_baseline"; then
            _remaining="${_remaining}${_remaining:+ }$_pid"
        fi
    done < "$_current"
    rm -f "$_current"
    if [ -n "$_remaining" ]; then
        echo "ERROR: agent-created processes survived shutdown: $_remaining" >&2
        return 1
    fi
    return 0
}
RB_AGENT_STARTED=0
RB_AGENT_BOUNDARY_CLOSED=0
finish_root_phase() {
    restore_system_guards
    # permission_setup writes atomically through mkstemp (0600). The report has
    # no credentials and is a declared artifact; publish it for the SFTP
    # collector when the root phase exits, after the agent has stopped.
    chmod a+r "$REC_DIR/permission_report.json" 2>/dev/null || true
    # The recreation agent and the SSH controller currently share the desktop
    # account.  Its sudo grant is removed before the agent starts, then restored
    # only after every agent-created process has stopped and both writable trees
    # have been frozen root-owned.  Later trusted verification/eval stages need
    # this grant to read the root-only RB runtime and prepare their workspaces.
    if [ "$RB_AGENT_STARTED" -eq 1 ] && [ "$RB_AGENT_BOUNDARY_CLOSED" -ne 1 ]; then
        echo "WARNING: controller sudo remains disabled because agent cleanup did not complete" >&2
        return 0
    fi
    # permission_setup makes every protected tree root-owned 0700 while the
    # agent is live.  Once that principal and all of its children are gone,
    # trusted eval still needs to import the runtime and read the frozen
    # reference/test inputs.  Reopen them read-only without changing ownership
    # or granting write access to the desktop account.
    if [ "$RB_AGENT_STARTED" -eq 1 ]; then
        for _eval_root in "$RB_CODE_DIR" "$BASE/reference" "$BASE/tests"; do
            [ -d "$_eval_root" ] || {
                echo "ERROR: protected eval input is missing: $_eval_root" >&2
                return 1
            }
            chown -R root:root "$_eval_root" || return 1
            chmod -R u+rwX,go+rX "$_eval_root" || return 1
            chmod -R go-w "$_eval_root" || return 1
        done
        echo "Protected runtime and frozen eval inputs reopened read-only"
    fi
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        case "$SUDO_USER" in
            *[!A-Za-z0-9_-]*)
                echo "ERROR: unsafe controller user name: $SUDO_USER" >&2
                return 1
                ;;
        esac
        if ! id "$SUDO_USER" >/dev/null 2>&1; then
            echo "ERROR: controller user no longer exists: $SUDO_USER" >&2
            return 1
        fi
        printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$SUDO_USER" \
            > "/etc/sudoers.d/rb-$SUDO_USER"
        chmod 440 "/etc/sudoers.d/rb-$SUDO_USER"
        if command -v visudo >/dev/null 2>&1; then
            visudo -cf "/etc/sudoers.d/rb-$SUDO_USER" >/dev/null
        fi
        echo "Controller sudo restored after agent shutdown: $SUDO_USER"
    fi
}
trap finish_root_phase EXIT
restore_system_guards

# ── Runtime cleanup ──
# Note: Do NOT pkill -u user here — the SSH invocation subshell runs as user
# and would be killed, breaking the pipeline's process tracking.
# Stale processes are harmless; they'll be cleaned up when the sandbox is destroyed.
sleep 1
# Clear shell history and preparation logs before demotion.
rm -f /root/.bash_history /home/user/.bash_history /home/ref_user/.bash_history
rm -f /root/.python_history /home/user/.python_history
# Clear RunCommand invocation scripts (may contain secrets) — keep dir for PID tracking
find /tmp/rb_invocations -name "script.sh" -exec truncate -s 0 {} \; 2>/dev/null || true
rm -f /tmp/reference_*.log /tmp/eval_*.log
# Clear app lock files
rm -f /tmp/.*-lockfile /tmp/.org.chromium.* /tmp/.com.google.* 2>/dev/null || true
# Fix bootstrap env permissions
chmod a+r /tmp/.rb_bootstrap_env 2>/dev/null || true

echo "=== Recreation with $MODEL ==="
RB_PREFLIGHT="$REC_DIR/recreation_preflight.log"
echo "started=$(date -Iseconds)" > "$RB_PREFLIGHT"

if [ -f "$REC_DIR/recreation/build.sh" ]; then
    if python3 "$REC_DIR/recreation_contract.py" "$REC_DIR/recreation" \
        --blocked-log "$REC_DIR/blocked_commands.log" \
        --reject-literal-sudo --quiet
    then
        echo "Recreation already done, skipping"
        exit 0
    fi
    echo "Existing recreation violates current contract; archiving and regenerating"
    mv "$REC_DIR/recreation" "$REC_DIR/recreation.invalid.$(date +%Y%m%d%H%M%S)" 2>/dev/null || rm -rf "$REC_DIR/recreation"
fi

export RECREATION_PROMPT=$(cat $REC_DIR/prompt.txt)

# ── Set up the fixed agent workspace and harness-only aliases ──
mkdir -p /workspace
rm -rf /workspace/output /workspace/recreation /workspace/reference_meta /workspace/frozen_fixtures
# The recreation workspace must be a real directory.  A symlink would let the agent resolve the
# supposedly anonymous path back to the stage storage path.  The trusted root phase snapshots it
# into $REC_DIR only after the agent has stopped.
mkdir -p /workspace/recreation/src /workspace/recreation/bin
mkdir -p /workspace/output
ln -sfn "$BASE/reference" /workspace/reference_meta
ln -sfn "$BASE/tests/fixtures" /workspace/frozen_fixtures
rm -rf /workspace/repo /workspace/install /workspace/fixtures

# ── Source bootstrap env (Xvfb, D-Bus, AT-SPI, openbox already running) ──
[ -f /tmp/.rb_bootstrap_env ] && . /tmp/.rb_bootstrap_env
export DISPLAY="${DISPLAY:-:99}"

# Disable the idle-screensaver: on a native GNOME :0 (dev VMs) its input grab
# silently eats real (XTest/uinput) clicks so the reference app looks "frozen"
# This is a no-op on headless Xvfb/Openbox targets.
_UIDU=$(id -u user 2>/dev/null || echo 1000)
_SID=$(loginctl 2>/dev/null | awk '$3=="user"{print $1; exit}')
[ -n "$_SID" ] && loginctl unlock-session "$_SID" 2>/dev/null || true
su - user -c "DISPLAY=$DISPLAY XAUTHORITY=${XAUTHORITY:-} bash -c 'xset s off; xset s noblank'" 2>/dev/null || true
su - user -c "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$_UIDU/bus gsettings set org.gnome.desktop.screensaver idle-activation-enabled false 2>/dev/null; DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$_UIDU/bus gsettings set org.gnome.desktop.session idle-delay 'uint32 0' 2>/dev/null" 2>/dev/null || true

# Previous failed/retried recreation runs may have left global guards behind.
# Root still needs network before agent isolation to rebuild the reference.
restore_system_guards

# ── Model env vars ──
export ANTHROPIC_API_KEY="$API_KEY"
export ANTHROPIC_AUTH_TOKEN="$AUTH_TOKEN"
export ANTHROPIC_BASE_URL="$BASE_URL"
export ANTHROPIC_MODEL="$CLAUDE_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLAUDE_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$CLAUDE_MODEL"

export GTK_MODULES=gail:atk-bridge
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
export QT_ACCESSIBILITY=1
export ELECTRON_ENABLE_ACCESSIBILITY=1
# Reference recipes run with network access. Deployments may select local mirrors without
# baking a provider-specific registry into the public runtime. Puppeteer is only a development
# dependency in the frozen desktop apps; Chromium is already present in the VM image.
export npm_config_registry="${RB_NPM_REGISTRY:-https://registry.npmjs.org}"
[ -n "${RB_NODE_DIST_MIRROR:-}" ] && export npm_config_disturl="$RB_NODE_DIST_MIRROR"
[ -n "${RB_ELECTRON_MIRROR:-}" ] && export ELECTRON_MIRROR="$RB_ELECTRON_MIRROR"
[ -n "${RB_ELECTRON_BUILDER_MIRROR:-}" ] && export ELECTRON_BUILDER_BINARIES_MIRROR="$RB_ELECTRON_BUILDER_MIRROR"
# A 50-way release starts many dependency-heavy Electron reference builds together.  The
# package managers' default socket pools turn that into hundreds of concurrent mirror
# downloads, which has produced repeatable ETIMEDOUT failures even though the mirror itself
# is healthy.  Bound each job's fan-out and let individual fetches survive a transient slow
# period; the outer three-attempt reference-build loop remains the final safety net.
export npm_config_maxsockets="${RB_NPM_MAX_SOCKETS:-4}"
export npm_config_fetch_retries="${RB_NPM_FETCH_RETRIES:-10}"
export npm_config_fetch_retry_factor="${RB_NPM_FETCH_RETRY_FACTOR:-2}"
export npm_config_fetch_retry_mintimeout="${RB_NPM_FETCH_RETRY_MIN_TIMEOUT_MS:-20000}"
export npm_config_fetch_retry_maxtimeout="${RB_NPM_FETCH_RETRY_MAX_TIMEOUT_MS:-120000}"
export npm_config_fetch_timeout="${RB_NPM_FETCH_TIMEOUT_MS:-600000}"
# pnpm consumes npm_config_* keys as its own kebab-case settings.
export npm_config_network_concurrency="${RB_PNPM_NETWORK_CONCURRENCY:-4}"
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
source "$REC_DIR/rb_runtime_helpers.sh"

# The pipeline owns source acquisition. build.sh receives /workspace/repo already at the
# pinned commit with submodules materialised, and performs no git operations of its own.
SOURCE_LOG=$(mktemp)
if ! python3 "$RB_CODE_DIR/scripts/core/reference_source.py" \
        --descriptor "$BASE/instance.json" \
        --source-dir /workspace/repo \
        --archive "$BASE/reference/reference.tar.gz" >"$SOURCE_LOG" 2>&1; then
    cat "$SOURCE_LOG"
    rm -f "$SOURCE_LOG"
    exit 2
fi
cat "$SOURCE_LOG"
SOURCE_MODE=$(sed -n 's/^SOURCE_MODE=//p' "$SOURCE_LOG" | tail -1)
rm -f "$SOURCE_LOG"
case "$SOURCE_MODE" in
    clone|frozen_fallback) echo "ref_repo_mode=$SOURCE_MODE" >> "$RB_PREFLIGHT" ;;
    *) echo "ERROR: shared source helper returned no valid source mode"; exit 2 ;;
esac
cd /workspace/repo
if [ -f /workspace/reference_meta/build.sh ]; then
        echo 'Building reference from build.sh...'
        cp /workspace/reference_meta/build.sh /tmp/rb_reference_build.sh
        chmod +x /tmp/rb_reference_build.sh

        : > /tmp/reference_build.log
        REF_BUILD_OK=0
        for attempt in 1 2 3; do
            attempt_log="/tmp/reference_build.attempt-${attempt}.log"
            echo "Reference build attempt ${attempt}/3..." | tee -a /tmp/reference_build.log
            # Keep one stalled package fetch from consuming the entire outer
            # reference-materialization deadline before retries can run.
            if timeout --signal=TERM --kill-after=30s \
                    "${RB_REFERENCE_BUILD_ATTEMPT_TIMEOUT_SEC:-1500}" \
                    bash /tmp/rb_reference_build.sh > "$attempt_log" 2>&1; then
                cat "$attempt_log" >> /tmp/reference_build.log
                REF_BUILD_OK=1
                break
            else
                build_rc=$?
                cat "$attempt_log" >> /tmp/reference_build.log
                echo "Reference build attempt ${attempt}/3 failed (exit ${build_rc})" | tee -a /tmp/reference_build.log
                tail -40 "$attempt_log" 2>/dev/null || true
                if [ "$attempt" -lt 3 ]; then
                    # The in-region npm mirror is substantially more reliable for
                    # Electron downloads, but it does not mirror every historical
                    # package tarball.  On a definitive mirror 404, retry through
                    # the canonical npm registry and force lockfile-resolved mirror
                    # URLs to follow that registry as well.  Do not use
                    # replace-registry-host=always: npm applies that to git dependencies too,
                    # turning git+ssh://git@github.com/org/repo into the invalid
                    # ssh://git@registry.npmjs.org/org/repo form.  Rewrite only known registry
                    # mirror URLs in lockfiles and keep GitHub dependencies on HTTPS.
                    # Other build failures retain the preferred mirror and ordinary retry
                    # behaviour.
                    if { [ "$build_rc" -eq 124 ] || grep -Eq \
                            'npm (ERR!|error).*(E404|ETIMEDOUT|ECONNRESET|ENOTFOUND|EAI_AGAIN)|404 Not Found|ERR_PNPM_.*(FETCH|REQUEST)|FetchError: request to https://(registry|cdn)\.npmmirror\.com/' \
                            "$attempt_log"; } \
                            && [ "$npm_config_registry" != "${RB_NPM_FALLBACK_REGISTRY:-https://registry.npmjs.org}" ]; then
                        export npm_config_registry="${RB_NPM_FALLBACK_REGISTRY:-https://registry.npmjs.org}"
                        export npm_config_replace_registry_host=npmjs
                        # npm's registry and Electron's binary mirror are independent.
                        # A timed-out multipart Electron download leaves a corrupt archive
                        # in root's cache; merely switching npm then makes every later
                        # attempt reuse that archive ("zip: not a valid zip file").  When
                        # the failed attempt touched Electron, switch its binary mirrors too
                        # and discard only the disposable download/package output.
                        if grep -Eqi \
                                'electron-v[^ ]*\.zip|/mirrors/electron/|ERR_ELECTRON_BUILDER|zip: not a valid zip file' \
                                "$attempt_log"; then
                            export ELECTRON_MIRROR="${RB_ELECTRON_FALLBACK_MIRROR:-https://github.com/electron/electron/releases/download/v}"
                            export ELECTRON_BUILDER_BINARIES_MIRROR="${RB_ELECTRON_BUILDER_FALLBACK_MIRROR:-https://github.com/electron-userland/electron-builder-binaries/releases/download/}"
                            find /root/.cache/electron /root/.cache/electron-builder \
                                -mindepth 1 -delete 2>/dev/null || true
                            find /workspace/repo/dist/linux-unpacked \
                                -mindepth 0 -delete 2>/dev/null || true
                            echo "Electron mirror/download failed; cleared partial cache and switched to the canonical release hosts" \
                                | tee -a /tmp/reference_build.log
                        fi
                        # Rewrite only project lockfiles.  A timed-out package manager can
                        # still be tearing down transient files below node_modules; walking
                        # those files made find/sed return non-zero under `set -e` and aborted
                        # the whole reference build after attempt 1 instead of retrying.
                        find /workspace/repo \
                            -path '*/node_modules' -prune -o \
                            -type f \( \
                                -name package-lock.json -o -name npm-shrinkwrap.json -o \
                                -name pnpm-lock.yaml -o -name yarn.lock \
                            \) -exec sed -i \
                                -e 's#https://registry\.npmmirror\.com/#https://registry.npmjs.org/#g' \
                                -e 's#https://registry\.npm\.taobao\.org/#https://registry.npmjs.org/#g' \
                                -e 's#https://registry\.nlark\.com/#https://registry.npmjs.org/#g' \
                                {} + || echo "WARN: one or more lockfiles changed during registry rewrite"
                        git config --global url."https://github.com/".insteadOf \
                            ssh://git@github.com/
                        git config --global --add url."https://github.com/".insteadOf \
                            git@github.com:
                        echo "package mirror failed; retrying with $npm_config_registry" | tee -a /tmp/reference_build.log
                    fi
                    sleep $((attempt * 10))
                fi
            fi
        done

        if [ "$REF_BUILD_OK" != 1 ]; then
            echo 'ERROR: reference build.sh failed'
            echo "ref_build=fail" >> "$RB_PREFLIGHT"
            # RETAIN before printing.  `tail -80` reaches the error line in practice
            # (measured: 7 of 7 archived failures), but three of those had 0-3 lines of
            # margin, and more importantly the failing tool routinely defers the ROOT
            # CAUSE to a file that dies with the VM: npm prints
            # "All providers failed for chrome ..." with an EMPTY reason and points at
            # ~/.npm/_logs/*-debug-0.log; meson points at meson-logs/meson-log.txt.  So
            # a build that fails on network egress is indistinguishable from one that
            # fails on a bad dependency. Keep these trusted preparation logs in the
            # task-owned stage rather than exposing them through the agent runtime directory.
            mkdir -p "$REC_DIR/reference_build_logs"
            cp /tmp/reference_build.log "$REC_DIR/reference_build_logs/" 2>/dev/null || true
            for extra in /home/*/.npm/_logs/*debug*.log /root/.npm/_logs/*debug*.log; do
                [ -f "$extra" ] && cp "$extra" "$REC_DIR/reference_build_logs/" 2>/dev/null || true
            done
            find /workspace/repo -maxdepth 4 \( -name 'meson-log.txt' -o -name 'CMakeError.log' \
                -o -name 'CMakeOutput.log' -o -name 'config.log' \) -exec \
                cp --parents {} "$REC_DIR/reference_build_logs/" \; 2>/dev/null || true
            tail -80 /tmp/reference_build.log
            exit 2
        fi
        tail -20 /tmp/reference_build.log
        if [ -x /workspace/install/venv/bin/pip ] && [ -d /workspace/repo ]; then
            if grep -Rqs '/workspace/repo' /workspace/install/venv/lib/*/site-packages /workspace/install/venv/bin 2>/dev/null; then
                echo 'Materializing editable Python install into reference venv...'
                /workspace/install/venv/bin/pip install --no-deps /workspace/repo > /tmp/reference_materialize_python.log 2>&1 || {
                    echo 'WARNING: failed to materialize editable Python install'
                }
                tail -20 /tmp/reference_materialize_python.log 2>/dev/null || true
            fi
        fi
else
    echo 'ERROR: frozen reference/build.sh missing'; exit 2
fi

    # --- Find reference binary ---
    BINARY=""
    if [ -f /workspace/reference_meta/build_result.json ]; then
        BINARY=$(python3 "$RB_CODE_DIR/scripts/linux/runtime/runtime_metadata.py" \
            executable /workspace/reference_meta/build_result.json 2>/dev/null)
    fi
    if [ -z "$BINARY" ] || [ ! -f "$BINARY" ]; then
        BINARY=$(find /workspace/install -type f -executable \
            -not -name '*.sh' -not -name '*.py' -not -name '*.so' -not -name '*.so.*' \
            -not -name '*.node' -not -name '*.js' -not -name 'chrome*' \
            2>/dev/null | head -1)
    fi

    # --- Verify reference build ---
    if [ ! -d /workspace/install ]; then
        echo 'ERROR: /workspace/install not found after reference build'
        echo "ref_build=pass ref_verify=fail_no_install" >> "$RB_PREFLIGHT"
        exit 2
    fi
    if [ -z "$BINARY" ]; then
        echo 'ERROR: No reference binary found'
        echo "ref_build=pass ref_verify=fail_no_binary" >> "$RB_PREFLIGHT"
        exit 2
    fi
    if [ ! -f /workspace/reference_meta/launch.sh ]; then
        echo 'ERROR: No launch.sh found'
        echo "ref_build=pass ref_verify=fail_no_launch" >> "$RB_PREFLIGHT"
        exit 2
    fi
    APP_NAME=$(basename "$BINARY")
    echo "Reference binary: $APP_NAME"
    echo "ref_build=pass ref_verify=pass binary=$APP_NAME" >> "$RB_PREFLIGHT"

    # Copy launch.sh into install tree so ref_user can access it
    cp /workspace/reference_meta/launch.sh /workspace/install/launch.sh
    chmod +x /workspace/install/launch.sh

    # A reference-check eval uses this exact frozen build as its candidate. Stop
    # before creating the recreation principal, launching the agent, or hiding
    # the frozen inputs.  The eval phase runs its isolated GUI stack, tests, and
    # app as ref_user.  Giving that principal ownership here is important for
    # apps backed by Chromium/QtWebEngine: launching the reference as root makes
    # their sandbox abort before any testcase can observe the UI.  Keeping the
    # runner and app on the same uid also avoids a cross-user AT-SPI boundary in
    # the reference-only check.
    if [ "${RB_REFERENCE_BUILD_ONLY:-0}" = "1" ]; then
        if ! id ref_user >/dev/null 2>&1; then
            echo 'ERROR: ref_user is missing; cannot run reference eval as the app owner'
            exit 1
        fi
        chown -R ref_user:ref_user /workspace/install /workspace/repo
        echo "Reference candidate materialized at /workspace/install"
        exit 0
    fi

    # Copy only public fixtures into the agent workspace.
    rm -rf /workspace/fixtures
    mkdir -p /workspace/fixtures
    cp -r /workspace/frozen_fixtures/* /workspace/fixtures/ 2>/dev/null || true
    echo "Fixtures: $(find /workspace/fixtures -type f 2>/dev/null | wc -l) files"

    # Hide source tree — agent must reverse-engineer from observation, not source.
    # Don't delete: Electron apps keep their binary in node_modules/ inside the repo.
    if [ -d /workspace/repo ]; then
        chown -R ref_user:ref_user /workspace/repo 2>/dev/null || true
        chmod -R 700 /workspace/repo 2>/dev/null || true
        chmod 711 /workspace/repo 2>/dev/null || true
    fi
    cd /workspace

    # --- Isolation: iptables egress block for user ---
    AGENT_UID=$(id -u user)
    if command -v iptables >/dev/null 2>&1 && command -v ip6tables >/dev/null 2>&1; then
        iptables -A OUTPUT -o lo -j ACCEPT || { echo 'ERROR: could not allow loopback'; exit 2; }
        iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT || {
            echo 'ERROR: could not install established-connection firewall rule'; exit 2;
        }
        IFS=, read -r MODEL_HOST MODEL_PORT < <(
            python3 "$RB_CODE_DIR/scripts/linux/runtime/runtime_metadata.py" \
                endpoint "$ANTHROPIC_BASE_URL"
        )
        [ -n "$MODEL_HOST" ] && [ -n "$MODEL_PORT" ] || {
            echo 'ERROR: model endpoint is invalid; cannot construct the recreation allowlist'; exit 2;
        }
        MODEL_IPS=$(getent ahostsv4 "$MODEL_HOST" 2>/dev/null | awk '{print $1}' | sort -u)
        [ -n "$MODEL_IPS" ] || {
            echo "ERROR: model endpoint $MODEL_HOST did not resolve; refusing an unsealed recreation"; exit 2;
        }
        for ip in $MODEL_IPS; do
            iptables -A OUTPUT -p tcp -d "$ip" --dport "$MODEL_PORT" -m owner --uid-owner "$AGENT_UID" -j ACCEPT || {
                echo "ERROR: could not allow model endpoint $ip:$MODEL_PORT"; exit 2;
            }
        done
        iptables -A OUTPUT -m owner --uid-owner "$AGENT_UID" -j REJECT || {
            echo 'ERROR: could not install IPv4 recreation deny rule'; exit 2;
        }
        ip6tables -A OUTPUT -m owner --uid-owner "$AGENT_UID" -j REJECT || {
            echo 'ERROR: could not install IPv6 recreation deny rule'; exit 2;
        }
        echo "Network isolation: user ($AGENT_UID) allowed $MODEL_HOST:$MODEL_PORT + loopback"
    else
        echo 'ERROR: iptables/ip6tables unavailable; refusing an unsealed recreation'
        exit 2
    fi

    # ── Prepare workspace for user execution ──
    mkdir -p /workspace/recreation/src /workspace/recreation/bin
    for dir in /home /home/user /home/user/rb_pipeline "$BASE" "$REC_DIR"; do
        [ -d "$dir" ] && chmod o+x "$dir" 2>/dev/null || true
    done
    chown -R user:user "$REC_DIR" /workspace/recreation /home/user 2>/dev/null || true
    chmod a+r /tmp/.rb_bootstrap_env 2>/dev/null || true

    # Pre-compile GSettings schemas before locking down permissions
    if [ -d /workspace/install ]; then
        find /workspace/install -path '*/glib-2.0/schemas' -type d 2>/dev/null | while read sd; do
            glib-compile-schemas "$sd" 2>/dev/null || true
        done
    fi

    # ── Dual-user isolation: reference owned by ref_user ──
    if id ref_user >/dev/null 2>&1 && [ -d /workspace/install ]; then
        chown -R ref_user:ref_user /workspace/install
        chmod -R 700 /workspace/install
        chmod 711 /workspace/install

        # X11 access for ref_user
        xhost +SI:localuser:ref_user 2>/dev/null || true

        # Cross-user D-Bus + AT-SPI: share sockets with ref_user
        USER_UID=$(id -u user)
        chmod a+rx "/run/user/$USER_UID" 2>/dev/null || true
        chmod a+rw "/run/user/$USER_UID/bus" 2>/dev/null || true
        chmod a+rx "/run/user/$USER_UID/at-spi" 2>/dev/null || true
        chmod a+rw "/run/user/$USER_UID/at-spi/bus_0" 2>/dev/null || true

        # Restart AT-SPI bus with ANONYMOUS auth config (bootstrap added it)
        pkill -u user -f "at-spi-bus-launcher" 2>/dev/null || true
        pkill -f "dbus-daemon.*accessibility" 2>/dev/null || true
        sleep 1
        # D-Bus activation restarts it when queried
        su - user -c "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$USER_UID/bus \
            dbus-send --session --dest=org.a11y.Bus --print-reply \
            /org/a11y/bus org.a11y.Bus.GetAddress" >/dev/null 2>&1 || true
        sleep 1
        # Restart registryd on new bus
        pkill -f "at-spi2-registryd" 2>/dev/null || true
        su - user -c "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$USER_UID/bus \
            /usr/libexec/at-spi2-registryd --use-gnome-session &" 2>/dev/null
        sleep 1
        echo "AT-SPI bus restarted with cross-user ANONYMOUS auth"

        # Reload session-bus policy. Authentication mechanisms are startup-only;
        # vm_bootstrap writes session-local.conf before any bus startup.
        SESSION_BUS_PID=$(pgrep -u user -f "dbus-daemon.*--session" | head -1)
        if [ -n "$SESSION_BUS_PID" ]; then
            kill -HUP "$SESSION_BUS_PID" 2>/dev/null || true
            echo "Session bus policy reloaded (PID=$SESSION_BUS_PID)"
        fi

        # Qt ignores AT_SPI_BUS_ADDRESS and discovers AT-SPI through the session
        # bus. Record whether the isolated reference user can actually do that.
        if su - ref_user -c "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$USER_UID/bus \
                dbus-send --session --dest=org.a11y.Bus --print-reply \
                /org/a11y/bus org.a11y.Bus.GetAddress" 2>/dev/null | grep -q string; then
            echo "cross-user session bus: ref_user can reach org.a11y.Bus"
            echo "crossuser_session_bus=pass" >> "$RB_PREFLIGHT"
        else
            echo "WARNING: ref_user cannot reach org.a11y.Bus; Qt reference trees will be incomplete" >&2
            echo "crossuser_session_bus=fail" >> "$RB_PREFLIGHT"
        fi

        # Re-apply socket permissions after AT-SPI restart
        chmod a+rx "/run/user/$USER_UID/at-spi" 2>/dev/null || true
        find "/run/user/$USER_UID/at-spi" -type s -exec chmod a+rw {} \; 2>/dev/null || true

        # Get AT-SPI bus address (query as user who owns the session)
        ATSPI_ADDR=$(su - user -c "
            export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$USER_UID/bus
            dbus-send --session --dest=org.a11y.Bus --print-reply \
                /org/a11y/bus org.a11y.Bus.GetAddress 2>/dev/null | \
                grep string | head -1 | sed 's/.*string \"\(.*\)\"/\1/'
        " 2>/dev/null || echo "")
        if [ -z "$ATSPI_ADDR" ] && [ -S "/run/user/$USER_UID/at-spi/bus_0" ]; then
            ATSPI_ADDR="unix:path=/run/user/$USER_UID/at-spi/bus_0"
        fi
        echo "AT-SPI bus: ${ATSPI_ADDR:-not found}"

        # Install the isolated launcher shipped in the complete RB runtime tree.
        # Set both AT_SPI_BUS_ADDRESS (for GTK) and DBUS_SESSION_BUS_ADDRESS
        # (for Qt/Electron) through positional data rather than generated code.
        install -m 0755 \
            "$RB_CODE_DIR/scripts/linux/runtime/reference_launcher.sh" \
            /tmp/rb_launch_isolated.sh
        printf -v REF_LAUNCH_COMMAND 'bash %q %q %q %q %q' \
            /tmp/rb_launch_isolated.sh "$USER_UID" "$ATSPI_ADDR" \
            "${DISPLAY:-:99}" "${XAUTHORITY:-}"

        # ── Comprehensive AT-SPI cross-user diagnostic ──
        RB_CODE_DIR="$RB_CODE_DIR" \
            bash "$RB_CODE_DIR/scripts/linux/runtime/atspi_diagnostic.sh"
        echo "=== AT-SPI DIAG ==="
        cat /tmp/rb_atspi_diag.txt 2>/dev/null || echo "no diag output"
        echo "=== END ==="

        # Launch reference app as ref_user
        nohup su - ref_user -c "$REF_LAUNCH_COMMAND" \
            >/tmp/rb_ref_launch.log 2>&1 &
        REF_BG_PID=$!
        sleep 3
        if kill -0 "$REF_BG_PID" 2>/dev/null; then
            echo "$REF_BG_PID" > /tmp/.rb_ref_pid
            chmod a+r /tmp/.rb_ref_pid
            echo "Reference app launched as ref_user (PID=$REF_BG_PID)"
            echo "ref_launch=pass pid=$REF_BG_PID" >> "$RB_PREFLIGHT"
        else
            echo "ERROR: Reference app (ref_user) failed to stay running"
            echo "--- ref_user launch log ---"
            cat /tmp/rb_ref_launch.log 2>/dev/null | tail -30
            echo "---"
            echo "ref_launch=fail" >> "$RB_PREFLIGHT"
            exit 2
        fi
    else
        echo "ERROR: ref_user is missing; refusing recreation without source isolation"
        exit 2
    fi

    # Remove sudo access for user (prevent agent from bypassing isolation)
    rm -f /etc/sudoers.d/rb-user /etc/sudoers.d/rb-* 2>/dev/null || true
    for privileged_group in sudo wheel admin docker adm; do
        getent group "$privileged_group" >/dev/null 2>&1 || continue
        gpasswd -d user "$privileged_group" >/dev/null 2>&1 || true
    done

    # ── Agent isolation: one shared boundary, applied immediately before launch ──
    # The frozen inputs were needed to build and launch the reference, so applying this earlier
    # would break preparation. From this point on the recreation agent only needs the fixed
    # workspace and runtime directories.
    rm -f /workspace/reference_meta /workspace/frozen_fixtures /tmp/rb_reference_build.sh

    # Move harness scripts out of agent workspace to /tmp (agent can't see them)
    # The root script embeds task paths and the real run id.  The agent phase does not use them;
    # rewrite those assignments in its private entrypoint so inspecting argv/env/script cannot
    # recover the task identity after the stage-output link is removed.
    sed \
        -e 's|^REC_DIR=.*|REC_DIR="/workspace/output"|' \
        -e 's|^BASE=.*|BASE="/workspace/hidden"|' \
        -e 's|^RUN_ID=.*|RUN_ID="anonymous"|' \
        "$REC_DIR/stage_recreation.sh" > /tmp/rb_agent_entry.sh
    cp "$REC_DIR/agent_invocation.py" /tmp/rb_agent_invocation.py
    cp "$REC_DIR/tool_use_capture.py" /tmp/tool_use_capture.py
    cp "$REC_DIR/desktop_capture.py" /tmp/desktop_capture.py
    cp "$REC_DIR/trajectory.py" /tmp/trajectory.py
    cp "$REC_DIR/model_proxy.py" /tmp/rb_model_proxy.py
    cp "$REC_DIR/invocation.json" /tmp/rb_invocation.json
    cp "$REC_DIR/rb_runtime_helpers.sh" /tmp/rb_runtime_helpers.sh
    cp "$REC_DIR/trajectory_summary.py" /tmp/trajectory_summary.py
    cp "$REC_DIR/trajectory_audit.py" /tmp/trajectory_audit.py
    cp "$REC_DIR/cua_mcp_preflight.py" /tmp/cua_mcp_preflight.py
    cp "$REC_DIR/streaming_model_proxy.py" /tmp/rb_streaming_model_proxy.py
    cp "$REC_DIR/recreation_contract.py" /tmp/rb_recreation_contract.py
    cp "$REC_DIR/prompt.txt" /tmp/rb_prompt.txt
    chmod 755 /tmp/rb_agent_entry.sh /tmp/rb_agent_invocation.py /tmp/rb_runtime_helpers.sh
    chmod 644 /tmp/rb_invocation.json /tmp/rb_prompt.txt /tmp/trajectory.py \
        /tmp/rb_model_proxy.py /tmp/trajectory_summary.py /tmp/trajectory_audit.py \
        /tmp/cua_mcp_preflight.py /tmp/rb_streaming_model_proxy.py \
        /tmp/rb_recreation_contract.py \
        /tmp/tool_use_capture.py /tmp/desktop_capture.py
    rm -f "$REC_DIR/stage_recreation.sh" "$REC_DIR/rb_runtime_helpers.sh" \
        "$REC_DIR/trajectory_summary.py" "$REC_DIR/trajectory_audit.py" \
        "$REC_DIR/cua_mcp_preflight.py" "$REC_DIR/streaming_model_proxy.py" \
        "$REC_DIR/recreation_contract.py" \
        "$REC_DIR/model_proxy.py" "$REC_DIR/prompt.txt"

    PERM_WORK=/tmp/rb_permission/runtime
    mkdir -p "$PERM_WORK"
    if ! PYTHONPATH=/tmp/rb_permission python3 /tmp/rb_permission/core/permission_probe.py \
            --platform linux \
            --run-id "$RUN_ID" \
            --user user \
            --protected "$BASE/reference" \
            --protected "$BASE/tests" \
            --protected "$RB_CODE_DIR" \
            --protected "$REC_DIR" \
            --writable /workspace/output \
            --writable /workspace/recreation \
            --spec-out "$PERM_WORK/spec.json" \
            --report "$REC_DIR/permission_report.json" \
            --env-out "$PERM_WORK/permission_env.sh"; then
        echo "ERROR: unified permission setup did not attest; refusing to launch the agent"
        exit 2
    fi

    # Codex: start a dedicated STREAMING model proxy as ROOT here (before the su
    # drop). Two reasons it must be root + streaming:
    #  (1) ROOT egress bypasses the per-uid isolation applied above — the isolated
    #      user's direct gateway connection can get intermittent RST when the
    #      load balancer rotates IPs after the egress allowlist is resolved.
    #  (2) STREAMING (forward+flush chunks as they arrive) — rb_model_proxy buffers
    #      the whole response (resp.read()), starving codex's SSE reader so its
    #      ~5-min stream-idle-timeout fires on every long reasoning turn → codex
    #      Reconnecting 1/5 → rc=1 after ~25min. Claude Code tolerates the buffered
    #      proxy; codex (gpt-5.x heavy reasoning, long turns) does not.
    # codex (uid 1000) reaches it over loopback (always allowed). Detached (setsid,
    # </dev/null) so it survives `exec su`. VM-validated: root streaming proxy → rc=0.
    if [ "$AGENT_CLI" = "codex" ]; then
        # The deployment adapter resolves the concrete provider and supplies one
        # OpenAI-compatible endpoint. Codex always reaches it through this root-owned
        # loopback relay; the runtime has no sidecar/direct routing mode of its own.
        CODEX_UPSTREAM="__CODEX_ENDPOINT_URL__"
        if [ -z "$CODEX_UPSTREAM" ]; then
            echo "ERROR: codex model endpoint is missing" >&2
            exit 2
        fi
        UPSTREAM_BASE="$CODEX_UPSTREAM" PORT=19778 setsid \
            python3 /tmp/rb_streaming_model_proxy.py \
            > /tmp/rb_codex_proxy.out 2> /tmp/rb_codex_proxy.err < /dev/null &
        sleep 2
        echo "Codex streaming relay (root): http://127.0.0.1:19778 -> $CODEX_UPSTREAM (probe=$(curl -s -o /dev/null -w '%{http_code}' -m 8 -X POST http://127.0.0.1:19778/responses -H 'content-type: application/json' -d '{}' 2>/dev/null))"
    fi

    RB_AGENT_BASELINE=/tmp/rb_user_processes_before_agent
    snapshot_desktop_user_processes > "$RB_AGENT_BASELINE" || {
        echo "ERROR: could not record the desktop-user process boundary" >&2
        exit 2
    }
    chmod 600 "$RB_AGENT_BASELINE"

    echo "Root setup complete. Dropping to user for recreation agent..."
    AGENT_RC=0
    RB_AGENT_STARTED=1
    su -m user -c "bash /tmp/rb_agent_entry.sh agent" || AGENT_RC=$?
    if stop_new_desktop_user_processes "$RB_AGENT_BASELINE"; then
        RB_AGENT_BOUNDARY_CLOSED=1
    else
        AGENT_RC=2
    fi
    rm -f "$RB_AGENT_BASELINE"

    # Remove the agent principal's write authority before copying. Any process which somehow
    # escaped the lifecycle check cannot race the trusted snapshot through a new path open.
    chown -R root:root /workspace/recreation /workspace/output
    chmod -R u+rwX,go-rwx /workspace/recreation /workspace/output

    # Freeze the task-name-free authoring workspace into the run-owned artifact tree.  Do this
    # even for a non-zero agent exit so partial output remains available for diagnosis.
    SNAPSHOT_DIR="$REC_DIR/.recreation.snapshot.$$"
    rm -rf "$SNAPSHOT_DIR"
    mkdir -p "$SNAPSHOT_DIR"
    if cp -a /workspace/recreation/. "$SNAPSHOT_DIR/"; then
        rm -rf "$REC_DIR/recreation"
        mv "$SNAPSHOT_DIR" "$REC_DIR/recreation" || AGENT_RC=2
    else
        echo "ERROR: could not snapshot canonical recreation workspace" >&2
        rm -rf "$SNAPSHOT_DIR"
        AGENT_RC=2
    fi
    cp -a /workspace/output/. "$REC_DIR/" || {
        echo "ERROR: could not collect recreation runtime output" >&2
        AGENT_RC=2
    }
    # The SSH collector needs read/traverse, never write. Keeping the root-owned stage immutable
    # also prevents the shared desktop account from changing the frozen artifact before eval.
    chown -R root:root "$REC_DIR"
    chmod -R u+rwX,go+rX "$REC_DIR"
    chmod go-w "$REC_DIR"
    # Do not exec here: replacing this root shell skips its EXIT trap and leaves
    # the recreation-only network guards active for the subsequent eval phase.
    finish_root_phase
    trap - EXIT
    exit "$AGENT_RC"
