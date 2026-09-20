#!/usr/bin/env bash

RB_RUNTIME_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ensure_gui_session() {
    export DISPLAY="${DISPLAY:-:99}"
    local display_num="${DISPLAY#:}"
    display_num="${display_num%%.*}"

    if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        echo "GUI session: restarting Xvfb on $DISPLAY" >&2
        pkill -f "Xvfb $DISPLAY" 2>/dev/null || true
        pkill -x openbox 2>/dev/null || true
        pkill -x at-spi2-registryd 2>/dev/null || true
        rm -f "/tmp/.X${display_num}-lock" "/tmp/.X11-unix/X${display_num}" 2>/dev/null || true
        Xvfb "$DISPLAY" -screen 0 1920x1080x24 -ac +extension GLX +render -noreset \
            >/tmp/rb_xvfb.log 2>&1 &
        for _ in $(seq 1 50); do
            xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
            sleep 0.2
        done
    fi
    if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        echo "ERROR: X display $DISPLAY is not available" >&2
        return 1
    fi

    # flatpak --socket=session-bus can only forward a unix:path socket into the
    # sandbox; dbus-launch's default abstract socket is unreachable inside, so
    # flatpak Qt/Electron apps abort ("Not connected to D-Bus" / zypak "Assertion
    # failed: bus"). GTK tolerates a missing bus, Qt/Electron do not. Ensure the
    # session bus is a path socket (prefer the real per-user bus, else spawn one).
    _bus_works() { dbus-send --session --dest=org.freedesktop.DBus \
        --type=method_call / org.freedesktop.DBus.ListNames >/dev/null 2>&1; }
    case "${DBUS_SESSION_BUS_ADDRESS:-}" in
        unix:path=*) _bus_works && _have_bus=1 || _have_bus=0 ;;
        *) _have_bus=0 ;;
    esac
    if [ "${_have_bus:-0}" -ne 1 ]; then
        _user_bus="/run/user/$(id -u)/bus"
        if [ -S "$_user_bus" ] && DBUS_SESSION_BUS_ADDRESS="unix:path=$_user_bus" _bus_works; then
            export DBUS_SESSION_BUS_ADDRESS="unix:path=$_user_bus"
        else
            _own_bus="${XDG_RUNTIME_DIR:-/tmp}/rb-session-bus"
            rm -f "$_own_bus" 2>/dev/null || true
            if dbus-daemon --session --address="unix:path=$_own_bus" --fork 2>/dev/null; then
                export DBUS_SESSION_BUS_ADDRESS="unix:path=$_own_bus"
            else
                eval "$(dbus-launch --sh-syntax)"
                export DBUS_SESSION_BUS_ADDRESS
            fi
        fi
    fi
    echo "[gui] DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-<unset>}" >&2

    /usr/libexec/at-spi2-registryd >/tmp/atspi_registryd.log 2>&1 &
    if ! pgrep -x openbox >/dev/null 2>&1; then
        openbox >/tmp/openbox.log 2>&1 &
        sleep 1
    fi

    export GTK_MODULES=gail:atk-bridge
    export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
    export QT_ACCESSIBILITY=1
    export ELECTRON_ENABLE_ACCESSIBILITY=1
    {
        printf 'export DISPLAY=%q\n' "$DISPLAY"
        printf 'export DBUS_SESSION_BUS_ADDRESS=%q\n' "${DBUS_SESSION_BUS_ADDRESS:-}"
        printf '%s\n' 'export GTK_MODULES=gail:atk-bridge'
        printf '%s\n' 'export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1'
        printf '%s\n' 'export QT_ACCESSIBILITY=1'
        printf '%s\n' 'export ELECTRON_ENABLE_ACCESSIBILITY=1'
    } > /tmp/.rb_bootstrap_env 2>/dev/null || true
}

launch_app() {
    local launch_sh="$1"
    shift
    ensure_gui_session || return 1
    rm -f /tmp/.*-lockfile /tmp/.org.chromium.* 2>/dev/null || true
    bash "$launch_sh" "$@" >&2 &
    local pid=$!
    sleep 3
    if kill -0 "$pid" 2>/dev/null; then
        echo "$pid"
    else
        return 1
    fi
}

strip_trajectory_base64() {
    local traj="${1:-/workspace/output/trajectory.jsonl}"
    [ -f "$traj" ] || return 0
    local size=$(stat -c%s "$traj" 2>/dev/null || echo 0)
    echo "=== Trajectory Summary (base64 stripped) ==="
    echo "  file: $traj ($size bytes)"
    python3 "$RB_RUNTIME_HELPER_DIR/trajectory_summary.py" "$traj" \
        2>/dev/null || true
    echo "==========================================="
}

audit_reference_reads() {
    local traj="${1:-/workspace/output/trajectory.jsonl}"
    [ -f "$traj" ] || return 0
    python3 "$RB_RUNTIME_HELPER_DIR/trajectory_audit.py" "$traj" \
        2>/dev/null || true
}

log_claude_info() {
    echo "=== Claude Code Info ==="
    echo "  claude version: $(claude --version 2>/dev/null || echo unknown)"
    echo "  model: ${MODEL:-unknown}"
    echo "  user: $(whoami)"
    echo "  display: ${DISPLAY:-unset}"
    echo "  home: ${HOME:-unset}"
    echo "  anthropic_base_url: ${ANTHROPIC_BASE_URL:-unset}"
    echo "========================"
}

cua_mcp_preflight() {
    local label="${1:-desktop-control}"
    local pid_hint="${2:-}"

    if ! command -v cua-driver >/dev/null 2>&1; then
        echo "ERROR: cua-driver not found for MCP preflight"
        return 1
    fi

    local preflight="$RB_RUNTIME_HELPER_DIR/cua_mcp_preflight.py"
    if [ ! -r "$preflight" ]; then
        echo "ERROR: CUA MCP preflight helper is missing: $preflight" >&2
        return 1
    fi
    RB_PREFLIGHT_LABEL="$label" RB_PREFLIGHT_PID="$pid_hint" \
        RB_CUA_MCP_COMMAND="cua-driver mcp --no-overlay" python3 "$preflight"
}
