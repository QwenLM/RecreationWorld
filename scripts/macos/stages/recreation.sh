#!/bin/bash
# recreation.sh — Dev Agent: recreate app from scratch
#
# The agent has NO source code, NO spec, NO documentation.
# Only the running reference app + CU + AX tools.
#
# Required env vars:
#   APP_NAME, WORK_DIR, MODEL, API_KEY, BASE_URL
# Optional:
#   SCAFFOLD_DIR, PIPELINE_DIR,
#   MAX_TURNS (optional), TIMEOUT (default 72000)
#
# Outputs:
#   /Users/Shared/workspace/recreation/ — stable agent workspace
#   (bound to the stage-owned recreation directory)
set -euo pipefail
# Infra failures (set -e triggered before reaching an explicit exit 2) must
# exit 2, not 1, so run_full.sh treats them as infra — not agent — failures.
# Cleared before agent execution starts (see [3/4] below).
trap 'echo "FATAL: Infra setup failed at line $LINENO"; exit 2' ERR
trap '' SIGPIPE
# Ensure common bin dirs are in PATH (SSH non-login shells have minimal PATH)
export PATH="$HOME/.local/bin:$HOME/local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
HARNESS_HOME="$HOME"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIPELINE_DIR="${PIPELINE_DIR:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
SCAFFOLD_DIR="${SCAFFOLD_DIR:-$PIPELINE_DIR/scripts/macos/scaffold}"

# ── CUA driver coordinate-space plumbing ──────────────────────────────────
# The daemon (serve) executes clicks + serves tools/list; its coordinate space
# is fixed at process start from CUA_DRIVER_RS_COORDINATE_SPACE. The `mcp`
# bridge (spawned by Claude Code with .claude.json env) only serves the
# initialize instructions. If the two disagree, the model gets contradictory
# guidance and clicks are mis-scaled. Two macOS pitfalls this handles:
#   1. `open -a` (LaunchServices) does NOT inherit shell env, and neither plain
#      `launchctl setenv` nor `launchctl asuser` reach the aqua-launched app
#      (asuser is "Operation not permitted" from an SSH session). VERIFIED on a
#      live macOS 14.7 target that `open --env KEY=VAL` does deliver -> use that.
#   2. Value must follow the run's param (main.sh exports it: 1=rel, 0=abs),
#      not a hardcoded 1.
CUA_COORD_SPACE="${CUA_DRIVER_RS_COORDINATE_SPACE:-1}"
CUA_COORD_SCALE="${CUA_DRIVER_RS_COORDINATE_SCALE:-1000}"

# Start the CUA daemon, delivering the coord env to it regardless of launch path.
# macOS: `open` does NOT inherit shell env and `launchctl setenv`/`asuser` do NOT
# reach the aqua-launched app (asuser is even "Operation not permitted" from an
# SSH session). Verified on macOS 14.7: `open --env KEY=VAL` does deliver and
# the daemon flips coord mode accordingly. So pass the env via `open --env`.
_start_cua_daemon() {
    local drv="$1" sock="$2"
    sudo pkill -x qwen-cua-driver 2>/dev/null || pkill -x qwen-cua-driver 2>/dev/null || true
    sleep 1
    if [ -d /Applications/QwenCuaDriver.app ]; then
        open --env "CUA_DRIVER_RS_COORDINATE_SPACE=$CUA_COORD_SPACE" \
             --env "CUA_DRIVER_RS_COORDINATE_SCALE=$CUA_COORD_SCALE" \
             --env "CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS=86400" \
             --env "CUA_DRIVER_RS_UPDATE_CHECK=0" \
             -n -g -a QwenCuaDriver --args serve --socket "$sock" || true
    else
        env CUA_DRIVER_RS_COORDINATE_SPACE="$CUA_COORD_SPACE" \
            CUA_DRIVER_RS_COORDINATE_SCALE="$CUA_COORD_SCALE" \
            CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS=86400 CUA_DRIVER_RS_UPDATE_CHECK=0 \
            nohup "$drv" serve --socket "$sock" > /tmp/cua_serve.log 2>&1 &
    fi
}

# Probe the daemon's ACTUAL coord mode via its tools/list x-description (the
# daemon serves tools/list; its x/y desc says "0–N normalized to window" in
# normalized mode, "screenshot pixels" in pixel mode). `ps eww` can't read a
# LaunchServices-spawned proc's env on macOS, so this socket probe is the
# reliable ground-truth. Echoes "1" | "0" | "<unknown>".
_cua_daemon_mode() {
    local drv="$1" sock="$2" out
    out=$(printf '%s\n' \
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"p","version":"1"}}}' \
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
        | timeout 25 "$drv" mcp --socket "$sock" 2>/dev/null || true)
    if   printf '%s' "$out" | grep -q 'normalized to window'; then echo 1
    elif printf '%s' "$out" | grep -q 'screenshot pixels';   then echo 0
    else echo "<unknown>"; fi
}

# D-gate: verify the daemon actually serves the intended coord space.
# - match     -> OK
# - wrong     -> kill daemon (retry restarts clean) + exit 2 (infra-retryable)
# - <unknown> -> fail closed: it is indistinguishable from an unreachable/stale daemon.
_assert_cua_coord_space() {
    local drv="$1" sock="$2" m="<unknown>" _probe_i
    # The Unix socket is created just before the MCP loop is ready to answer.
    # A freshly launchctl-started daemon can therefore be reachable by path but
    # still return no tools/list response for a fraction of a second.
    for _probe_i in 1 2 3 4 5; do
        m=$(_cua_daemon_mode "$drv" "$sock")
        [ "$m" != "<unknown>" ] && break
        sleep 1
    done
    case "$m" in
        "$CUA_COORD_SPACE")
            echo "  CUA daemon coord-space verified via tools/list: $m (want $CUA_COORD_SPACE)" ;;
        "<unknown>")
            echo "  FATAL: could not probe daemon coord-space (want $CUA_COORD_SPACE)."
            exit 2 ;;
        *)
            echo "  FATAL: CUA daemon coord-space mismatch — daemon=$m want=$CUA_COORD_SPACE."
            echo "         Killing daemon so the retry restarts in the correct space."
            sudo pkill -x qwen-cua-driver 2>/dev/null || pkill -x qwen-cua-driver 2>/dev/null || true
            exit 2 ;;
    esac
}

# EXIT trap: clean up on unexpected exit
recreation_cleanup() {
    [ -z "${MCP_PROBE_PATH:-}" ] || sudo rm -f "$MCP_PROBE_PATH" 2>/dev/null || true
}
trap recreation_cleanup EXIT

MAX_TURNS="${MAX_TURNS:-}"
TIMEOUT="${TIMEOUT:-72000}"
# The shared proxy owns transient per-request retry. Do not add a second client-side
# retry budget here; it multiplies attempts and differs from Codex's lifecycle.

# Load deterministic preparation result.
if [ -f "$WORK_DIR/prepare_result.env" ]; then
    source "$WORK_DIR/prepare_result.env"
fi

RECREATION_DIR="${RECREATION_DIR:-$HOME/Recreation/${APP_NAME}}"
DEVAGENT_USER="${DEVAGENT_USER:-}"
DEVAGENT_HOME="${DEVAGENT_HOME:-}"
DEVAGENT_RECREATION="${DEVAGENT_RECREATION:-}"

# Devagent isolation is part of the release contract, not an optional compatibility mode.
if [ -n "$DEVAGENT_USER" ] && id "$DEVAGENT_USER" &>/dev/null; then
    USE_DEVAGENT=true
    AGENT_WORKSPACE="/Users/Shared/workspace/recreation"
    AGENT_RECREATION="${DEVAGENT_RECREATION:-$AGENT_WORKSPACE}"
    AGENT_HOME="${DEVAGENT_HOME:-/Users/$DEVAGENT_USER}"
else
    echo "FATAL: devagent user not found. Recreation requires user isolation for anti-cheat."
    echo "       Create devagent before entering recreation. There is no unisolated release path."
    exit 2
fi

if [ "$AGENT_RECREATION" != "$AGENT_WORKSPACE" ] || [ -L "$AGENT_WORKSPACE" ] || [ ! -d "$AGENT_WORKSPACE" ]; then
    echo "FATAL: canonical workspace is not an isolated real directory: $AGENT_WORKSPACE"
    exit 2
fi

mkdir -p "$RECREATION_DIR"

echo "╔══════════════════════════════════════╗"
echo "║  Recreation                          ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  App:         $APP_NAME"
echo "  Model:       $MODEL"
echo "  Recreation:  $AGENT_WORKSPACE"
    if [ "$USE_DEVAGENT" = true ]; then
        echo "  Isolation:   devagent user ($DEVAGENT_USER)"
    else
        echo "  Isolation:   NONE (sudo accessible — anti-cheat BYPASSED)"
    fi
echo "  Max turns:   $MAX_TURNS"
echo "  Timeout:     ${TIMEOUT}s"
echo "  Started:     $(date)"
echo ""

# ── 1. Verify reference app is running ────────────────────────────────────

echo "[1/4] Verifying reference app..."

# preparation_result.env (sourced near the top) carries the authoritative in-place
# OFFICIAL_APP/APP_BINARY under Plan A. Capture them so the fragile hidden-dir
# reference_result.env load below cannot clobber them to empty (that caused the
# "Reference app is NOT running" recreation failures on Plan A).
_PREPARE_OFFICIAL_APP="${OFFICIAL_APP:-}"
_PREPARE_APP_BINARY="${APP_BINARY:-}"

# Load reference metadata from the root-only directory.
HIDDEN_PATH_FILE="/var/tmp/.pipeline_hidden_path"
if [ -f "$HIDDEN_PATH_FILE" ]; then
    HIDDEN_DIR_RUNTIME=$(sudo cat "$HIDDEN_PATH_FILE" 2>/dev/null || true)
fi
# Resolve the hidden directory only from the root-owned pointer.
if [ -z "${HIDDEN_DIR_RUNTIME:-}" ]; then
    echo "FATAL: hidden release-input directory is unavailable"
    exit 2
fi
if sudo test -f "$HIDDEN_DIR_RUNTIME/reference_result.env" 2>/dev/null; then
    if sudo bash -n "$HIDDEN_DIR_RUNTIME/reference_result.env" 2>/dev/null; then
        source <(sudo cat "$HIDDEN_DIR_RUNTIME/reference_result.env")
    else
        echo "FATAL: reference_result.env has syntax errors" >&2
        exit 2
    fi
else
    echo "FATAL: frozen reference metadata is unavailable"
    exit 2
fi

# Plan A fallback: if the (fragile, locked-hidden-dir) reference load left these empty,
# use the authoritative values preparation published in preparation_result.env — so we relaunch
# the correct in-place reference app and pgrep by its real binary name, instead of
# falling through to the locked source/DerivedData search.
OFFICIAL_APP="${OFFICIAL_APP:-$_PREPARE_OFFICIAL_APP}"
APP_BINARY="${APP_BINARY:-$_PREPARE_APP_BINARY}"
if [ -n "${OFFICIAL_APP:-}" ]; then
    echo "  Reference app resolved: $OFFICIAL_APP (binary: ${APP_BINARY:-?})"
fi

# ── Restore reference app from the hidden directory for the GUI account ──
# Hidden dir is root:wheel chmod 700 (set by preparation section 9b).
# LaunchServices runs as the login account and cannot access root-only directories.
# Copy the app and launch script to a login-account-owned temporary directory first.
RESTORE_DIR="/var/tmp/.pipeline_refapp_${APP_NAME}"
if [ -n "${HIDDEN_DIR_RUNTIME:-}" ]; then
    # Find .app in hidden dir (prefer OFFICIAL_APP basename, fallback to any .app)
    _RESTORE_APP=""
    if [ -n "${OFFICIAL_APP:-}" ]; then
        _HIDDEN_APP="${HIDDEN_DIR_RUNTIME}/$(basename "$OFFICIAL_APP")"
        if sudo test -d "$_HIDDEN_APP" 2>/dev/null; then
            _RESTORE_APP="$_HIDDEN_APP"
        fi
    fi
    if [ -z "$_RESTORE_APP" ]; then
        _RESTORE_APP=$(sudo find "$HIDDEN_DIR_RUNTIME" -maxdepth 1 -name "*.app" -type d 2>/dev/null | head -1 || true)
    fi

    if [ -n "$_RESTORE_APP" ]; then
        _RESTORE_DEST="$RESTORE_DIR/$(basename "$_RESTORE_APP")"
        if [ -d "$_RESTORE_DEST" ]; then
            # Already restored (by preparation) — reuse to avoid killing a running app
            echo "  RESTORE_DIR already has app — reusing (app may be running from here)"
        else
            sudo rm -rf "$RESTORE_DIR" 2>/dev/null || true
            sudo mkdir -p "$RESTORE_DIR"
            sudo cp -R "$_RESTORE_APP" "$RESTORE_DIR/"
            sudo cp "$HIDDEN_DIR_RUNTIME/launch.sh" "$RESTORE_DIR/" 2>/dev/null || true
            sudo chown -R "$(whoami):staff" "$RESTORE_DIR"
            chmod -R 700 "$RESTORE_DIR"
        fi
        OFFICIAL_APP="$_RESTORE_DEST"
        # Clear quarantine and re-sign for LaunchServices
        xattr -dr com.apple.quarantine "$OFFICIAL_APP" 2>/dev/null || true
        codesign -fs - --deep "$OFFICIAL_APP" 2>/dev/null || true
        echo "  Reference app restored to login-account-readable dir: $OFFICIAL_APP"
    elif sudo test -f "$HIDDEN_DIR_RUNTIME/launch.sh" 2>/dev/null; then
        # No .app but launch.sh exists — copy it to a readable directory.
        if [ -f "$RESTORE_DIR/launch.sh" ]; then
            echo "  RESTORE_DIR already has launch.sh — reusing"
        else
            sudo rm -rf "$RESTORE_DIR" 2>/dev/null || true
            sudo mkdir -p "$RESTORE_DIR"
            sudo cp -R "$HIDDEN_DIR_RUNTIME/." "$RESTORE_DIR/" 2>/dev/null || true
            sudo chown -R "$(whoami):staff" "$RESTORE_DIR"
            chmod -R 700 "$RESTORE_DIR"
            echo "  Hidden dir contents restored (launch.sh only, no .app bundle)"
        fi
    fi
fi

# Detect launch.sh for official app (prefer restored copy)
OFFICIAL_LAUNCH_SH=""
if [ -d "$RESTORE_DIR" ] && [ -f "$RESTORE_DIR/launch.sh" ]; then
    OFFICIAL_LAUNCH_SH="$RESTORE_DIR/launch.sh"
elif [ -n "${HIDDEN_DIR_RUNTIME:-}" ] && sudo test -f "$HIDDEN_DIR_RUNTIME/launch.sh" 2>/dev/null; then
    OFFICIAL_LAUNCH_SH="$HIDDEN_DIR_RUNTIME/launch.sh"
elif [ -f "$WORK_DIR/source/launch.sh" ]; then
    OFFICIAL_LAUNCH_SH="$WORK_DIR/source/launch.sh"
fi

# Force Chromium/Electron accessibility on the RECREATED app (the reference app's
# launch.sh was already injected in reference). Electron content AX is empty unless the
# app is launched with --force-renderer-accessibility; inject it into the recreated
# launch.sh so eval scoring (+ conftest per-test relaunch) sees the full tree.
# No-op for native/Gecko. Verified on live sandboxes.
is_electron_bundle() {
    local bundle="$1" fw="$1/Contents/Frameworks"
    [ -n "$bundle" ] && [ -d "$fw" ] || return 1
    [ -d "$fw/Electron Framework.framework" ] || [ -d "$fw/Chromium Embedded Framework.framework" ]
}
inject_force_renderer_accessibility() {
    local lsh="$1" bundle="$2"
    [ -f "$lsh" ] || return 0
    is_electron_bundle "$bundle" || return 0
    grep -q -- '--force-renderer-accessibility' "$lsh" && return 0
    # Insert the flag right before the forwarded "$@" on the LAST non-comment line
    # that has it — the real launch line, whatever its form:
    #   exec "$BIN" "$@" | exec "$BIN" --no-sandbox "$@" | exec open -W "$APP" --args "$@" | "$BIN" "$@"
    cp "$lsh" "${lsh}.axbak" 2>/dev/null || return 0
    perl -0pi -e '
        my @L = split /\n/, $_, -1;
        for (my $i=$#L; $i>=0; $i--) {
            next if $L[$i] =~ /^\s*#/;
            next unless $L[$i] =~ /"\$\@"/;
            next unless $L[$i] =~ /\bexec\b|\bopen\b|\$BIN|\$APP/;
            $L[$i] =~ s/("\$\@")/--force-renderer-accessibility $1/;
            last;
        }
        $_ = join("\n", @L);
    ' "$lsh"
    # SAFETY: revert to the original if injection produced invalid bash, so the
    # worst case is EXACTLY the pre-fix launch.sh — never a broken launcher.
    if ! bash -n "$lsh" 2>/dev/null; then
        mv "${lsh}.axbak" "$lsh" 2>/dev/null || true
        echo "  [AX] WARN: injection produced invalid recreated launch.sh — reverted (pre-fix, no flag)"
        return 0
    fi
    rm -f "${lsh}.axbak" 2>/dev/null || true
    if grep -q -- '--force-renderer-accessibility' "$lsh"; then
        echo "  [AX] injected --force-renderer-accessibility into recreated launch.sh (Electron)"
    else
        echo "  [AX] WARN: Electron recreation but no \"\$@\" launch line; web AX may be empty"
    fi
}

# Helper: launch official app via launch.sh if available, fallback to open
launch_official_app() {
    if [ -n "$OFFICIAL_LAUNCH_SH" ]; then
        bash "$SCRIPT_DIR/../tools/run_launch.sh" "$OFFICIAL_LAUNCH_SH" &
        sleep 5
        if pgrep "${APP_BINARY:0:15}" > /dev/null 2>&1 || pgrep -i "$APP_BINARY" > /dev/null 2>&1; then
            return 0
        fi
        echo "  WARNING: launch.sh did not start the app, falling back to open"
    fi
    if [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}" ]; then
        open "$OFFICIAL_APP" 2>/dev/null || true
        sleep 5
        return 0
    fi
    return 1
}

# Derive actual executable name from bundle (handles apps with spaces like "Background Music")
_REFERENCE_APP_BINARY="${APP_BINARY:-}"
APP_BINARY=""
if [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}/Contents/Info.plist" ] 2>/dev/null || [ -f "${OFFICIAL_APP:-}/Contents/Info.plist" ]; then
    APP_BINARY=$(defaults read "$OFFICIAL_APP/Contents/Info.plist" CFBundleExecutable 2>/dev/null || echo "")
fi
if [ -z "$APP_BINARY" ] && [ -n "${HIDDEN_DIR_RUNTIME:-}" ] && [ -n "${OFFICIAL_APP:-}" ]; then
    HIDDEN_APP_BUNDLE="${HIDDEN_DIR_RUNTIME}/$(basename "$OFFICIAL_APP")"
    if sudo test -f "$HIDDEN_APP_BUNDLE/Contents/Info.plist" 2>/dev/null; then
        APP_BINARY=$(sudo defaults read "$HIDDEN_APP_BUNDLE/Contents/Info.plist" CFBundleExecutable 2>/dev/null || echo "")
    fi
fi
APP_BINARY="${APP_BINARY:-${_REFERENCE_APP_BINARY:-$APP_NAME}}"
# Also derive the .app bundle name for find searches
APP_BUNDLE_NAME="$(basename "${OFFICIAL_APP:-.}")"
[ "$APP_BUNDLE_NAME" = "." ] && APP_BUNDLE_NAME="${APP_NAME}.app"

# Clear quarantine on reference app to prevent "unidentified developer" dialogs
if [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}" ]; then
    xattr -dr com.apple.quarantine "$OFFICIAL_APP" 2>/dev/null || true
    codesign -fs - --deep "$OFFICIAL_APP" 2>/dev/null || true
fi

if ! pgrep "${APP_BINARY:0:15}" > /dev/null 2>&1; then
    # Try case-insensitive search using actual binary name, then APP_NAME
    FOUND_PID=$(pgrep -i "$APP_BINARY" 2>/dev/null | head -1 || true)
    if [ -z "$FOUND_PID" ] && [ "$APP_BINARY" != "$APP_NAME" ]; then
        FOUND_PID=$(pgrep -i "$APP_NAME" 2>/dev/null | head -1 || true)
    fi
    if [ -z "$FOUND_PID" ]; then
        # If preparation hid OFFICIAL_APP, restore it to a login-account-only location.
        if [ -n "${OFFICIAL_APP:-}" ] && [ ! -d "${OFFICIAL_APP:-}" ] && [ -n "${HIDDEN_DIR_RUNTIME:-}" ]; then
            HIDDEN_APP="${HIDDEN_DIR_RUNTIME}/$(basename "$OFFICIAL_APP")"
            if sudo test -d "$HIDDEN_APP" 2>/dev/null; then
                RESTORE_DIR="/var/tmp/.pipeline_refapp_${APP_NAME}"
                sudo rm -rf "$RESTORE_DIR" 2>/dev/null || true
                sudo mkdir -p "$RESTORE_DIR"
                sudo cp -R "$HIDDEN_APP" "$RESTORE_DIR/"
                sudo chown -R "$(whoami):staff" "$RESTORE_DIR"
                chmod -R 700 "$RESTORE_DIR"
                OFFICIAL_APP="$RESTORE_DIR/$(basename "$OFFICIAL_APP")"
                echo "  OFFICIAL_APP restored from hidden dir (login account only): $OFFICIAL_APP"
            fi
        fi
        # Try to relaunch from OFFICIAL_APP
        if [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}" ]; then
            echo "  Reference app not running — relaunching from $OFFICIAL_APP"
            xattr -dr com.apple.quarantine "$OFFICIAL_APP" 2>/dev/null || true
            launch_official_app
            if ! pgrep "${APP_BINARY:0:15}" > /dev/null 2>&1 && ! pgrep -i "$APP_BINARY" > /dev/null 2>&1; then
                echo "ERROR: Reference app failed to relaunch"
                exit 2
            fi
        else
            # Last resort: search DerivedData and pipeline source for .app
            echo "  Searching for reference app in DerivedData..."
            FOUND_APP=$(find "$HOME/Library/Developer/Xcode/DerivedData" -name "$APP_BUNDLE_NAME" -type d 2>/dev/null | head -1 || true)
            if [ -z "$FOUND_APP" ]; then
                FOUND_APP=$(find "$HOME/Library/Developer/Xcode/DerivedData" -iname "${APP_NAME}.app" -type d 2>/dev/null | head -1 || true)
            fi
            if [ -z "$FOUND_APP" ]; then
                FOUND_APP=$(find "$HOME/pipeline_work/${APP_NAME}/source" -name "*.app" -type d 2>/dev/null | head -1 || true)
            fi
            if [ -z "$FOUND_APP" ]; then
                FOUND_APP=$(find "$HOME/pipeline_work/${APP_NAME}" -name "*.app" -type d 2>/dev/null | head -1 || true)
            fi
            if [ -n "$FOUND_APP" ]; then
                echo "  Found app: $FOUND_APP — launching"
                OFFICIAL_APP="$FOUND_APP"
                launch_official_app
            elif [ -n "$OFFICIAL_LAUNCH_SH" ]; then
                echo "  No .app found — trying launch.sh..."
                launch_official_app || true
                if pgrep "${APP_BINARY:0:15}" > /dev/null 2>&1 || pgrep -i "$APP_NAME" > /dev/null 2>&1; then
                    echo "  Reference app launched via launch.sh (no .app bundle)"
                else
                    echo "ERROR: Reference app not running — all launch attempts failed"
                    echo "  Cannot recreate without a running reference app"
                    exit 2
                fi
            else
                echo "ERROR: Reference app not running and no .app or launch.sh found"
                exit 2
            fi
        fi
    fi
fi
if pgrep "${APP_BINARY:0:15}" > /dev/null 2>&1 || pgrep -i "$APP_NAME" > /dev/null 2>&1; then
    echo "  Reference app is running"
else
    echo "ERROR: Reference app is NOT running after all launch attempts"
    echo "  Cannot proceed with recreation — reference app is required"
    exit 2
fi

# Resolve the live reference PID once for both the strict MCP preflight and the optional
# prompt hint. Merely finding some application process is not enough: the recreation
# principal must be able to read THIS process's accessibility tree and pixels.
REF_PID=$(pgrep -i "$APP_BINARY" 2>/dev/null | head -1 || true)
if [ -z "$REF_PID" ] && [ "$APP_BINARY" != "$APP_NAME" ]; then
    REF_PID=$(pgrep -i "$APP_NAME" 2>/dev/null | head -1 || true)
fi
if [ -z "$REF_PID" ] || ! kill -0 "$REF_PID" 2>/dev/null; then
    echo "FATAL: could not resolve the live reference PID for MCP preflight"
    exit 2
fi
echo "  Reference PID: $REF_PID"

# ── 2. Setup Claude Code permissions ──────────────────────────────────────

echo "[2/4] Setting up Claude Code permissions..."
if [ "$USE_DEVAGENT" = true ]; then
    sudo mkdir -p "$AGENT_RECREATION/.claude"
    sudo chown -R "$DEVAGENT_USER:staff" "$AGENT_RECREATION"
else
    mkdir -p "$AGENT_RECREATION/.claude"
fi
# Settings come from the ONE shared contract (core/agent_config.py), aligned to linux. The literal
# that was here allowed "desktop-control:*" -- a FOURTH spelling of the MCP permission and not a
# valid form at all (it is neither mcp__desktop-control nor mcp__desktop-control__*) -- and denied
# Agent without Task. The heredoc below is a byte-identical fallback for the case where rb's core
# is not importable on this target, because a settings file that fails to appear means Claude prompts
# for permission and a `-p` run dies.
_RB_SCRIPTS="${PIPELINE_DIR:-$(cd "$(dirname "$0")/../../.." && pwd)}/scripts"
if ! PYTHONPATH="$_RB_SCRIPTS" python3 -m core.agent_config settings \
        --mcp-server desktop-control --scope desktop_session \
        > "/tmp/_claude_settings.json" 2>/dev/null; then
    echo "  FATAL: shared core.agent_config is unavailable; refusing a private fallback"
    exit 2
fi

# Preparation deliberately makes PIPELINE_DIR owner-only before the untrusted
# user starts. The strict MCP probe is self-contained, so stage just that one
# trusted file at a root-owned readable path instead of reopening the pipeline
# tree to devagent.
MCP_PROBE_PATH="/var/tmp/.rb_mcp_probe_$$.py"
sudo cp "$_RB_SCRIPTS/core/mcp_probe.py" "$MCP_PROBE_PATH"
sudo chown root:wheel "$MCP_PROBE_PATH"
sudo chmod 755 "$MCP_PROBE_PATH"

if [ "$USE_DEVAGENT" = true ]; then
    sudo cp "/tmp/_claude_settings.json" "$AGENT_RECREATION/.claude/settings.json"
    sudo mkdir -p "$AGENT_HOME/.claude"
    sudo cp "/tmp/_claude_settings.json" "$AGENT_HOME/.claude/settings.json"

    # Start CUA Driver daemon as the login account with Window Server access.
    CUA_DRIVER_PATH=$(which qwen-cua-driver 2>/dev/null || echo "/usr/local/bin/qwen-cua-driver")
    CUA_DAEMON_SOCK="$HOME/Library/Caches/qwen-cua-driver/qwen-cua-driver.sock"

    # qwen-cua-driver 0.7.3's `status` does not reliably recognize the
    # controller-managed daemon started with an explicit socket. Probe the
    # actual MCP endpoint instead so a healthy GUI/TCC-bound daemon survives
    # the setup -> recreation boundary.
    CUA_EXISTING_MODE="<unknown>"
    if [ -S "$CUA_DAEMON_SOCK" ]; then
        CUA_EXISTING_MODE=$(_cua_daemon_mode "$CUA_DRIVER_PATH" "$CUA_DAEMON_SOCK")
    fi
    if [ "$CUA_EXISTING_MODE" = "$CUA_COORD_SPACE" ]; then
        echo "  CUA Driver daemon already running (socket MCP healthy)"
    else
        _start_cua_daemon "$CUA_DRIVER_PATH" "$CUA_DAEMON_SOCK"
        for _i in $(seq 1 10); do [ -S "$CUA_DAEMON_SOCK" ] && break; sleep 1; done
        if [ ! -S "$CUA_DAEMON_SOCK" ]; then
            echo "  FATAL: CUA Driver daemon failed to start"
            exit 2
        fi
        echo "  CUA Driver daemon started"
    fi
    _assert_cua_coord_space "$CUA_DRIVER_PATH" "$CUA_DAEMON_SOCK"
    sudo chgrp staff "$CUA_DAEMON_SOCK" \
        "$(dirname "$CUA_DAEMON_SOCK")" \
        "$(dirname "$(dirname "$CUA_DAEMON_SOCK")")" \
        "$(dirname "$(dirname "$(dirname "$CUA_DAEMON_SOCK")")")"
    sudo chmod 710 "$(dirname "$CUA_DAEMON_SOCK")" \
        "$(dirname "$(dirname "$CUA_DAEMON_SOCK")")" \
        "$(dirname "$(dirname "$(dirname "$CUA_DAEMON_SOCK")")")"
    sudo chmod 660 "$CUA_DAEMON_SOCK"

    # Apply max screenshot dimension (default 1920 = native FHD)
    if [ -n "${MAX_SCREENSHOT_DIM:-}" ]; then
        if "$CUA_DRIVER_PATH" call set_config "{\"max_image_dimension\": $MAX_SCREENSHOT_DIM}" 2>/dev/null; then
            echo "  CUA Driver max_image_dimension=$MAX_SCREENSHOT_DIM"
        else
            echo "  FATAL: failed to set CUA Driver max_image_dimension"
            exit 2
        fi
    fi

    # Grant TCC permissions for QwenCuaDriver daemon
    CUA_BUNDLE_ID="com.qwencode.cua-driver"
    if [ -f /Applications/QwenCuaDriver.app/Contents/Info.plist ]; then
        CUA_BUNDLE_ID=$(defaults read /Applications/QwenCuaDriver.app/Contents/Info.plist CFBundleIdentifier 2>/dev/null || echo "com.qwencode.cua-driver")
    fi
    CUA_BIN_PATH=$(which qwen-cua-driver 2>/dev/null || echo "/Applications/QwenCuaDriver.app/Contents/MacOS/qwen-cua-driver")
    for tcc_svc in kTCCServiceAccessibility kTCCServiceScreenCapture kTCCServiceAppleEvents kTCCServicePostEvent kTCCServiceListenEvent kTCCServiceSystemPolicyAllFiles kTCCServiceDeveloperTool kTCCServiceCalendar kTCCServiceAddressBook kTCCServiceContactsFull kTCCServiceContactsLimited kTCCServiceReminders kTCCServicePhotos kTCCServicePhotosAdd kTCCServiceCamera kTCCServiceMicrophone kTCCServiceBluetoothAlways kTCCServiceMediaLibrary kTCCServiceSpeechRecognition kTCCServiceMotion kTCCServiceLocation kTCCServiceFocusStatus kTCCServiceSystemPolicyDesktopFolder kTCCServiceSystemPolicyDocumentsFolder kTCCServiceSystemPolicyDownloadsFolder kTCCServiceSystemPolicyNetworkVolumes kTCCServiceSystemPolicyRemovableVolumes kTCCServiceFileProviderDomain kTCCServiceFileProviderPresence; do
        sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
            "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, flags) VALUES ('$tcc_svc', '$CUA_BUNDLE_ID', 0, 2, 0, 1, 0)" 2>/dev/null
        sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
            "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, flags) VALUES ('$tcc_svc', '$CUA_BIN_PATH', 1, 2, 0, 1, 0)" 2>/dev/null
    done

    # Grant system-level TCC for SSH/shell process (osascript accessibility check needs this)
    for tcc_client in "/usr/libexec/sshd-keygen-wrapper" "com.apple.Terminal"; do
        for tcc_svc in kTCCServiceAccessibility kTCCServiceScreenCapture kTCCServiceAppleEvents kTCCServicePostEvent kTCCServiceListenEvent kTCCServiceSystemPolicyAllFiles kTCCServiceDeveloperTool kTCCServiceCalendar kTCCServiceAddressBook kTCCServiceContactsFull kTCCServiceContactsLimited kTCCServiceReminders kTCCServicePhotos kTCCServicePhotosAdd kTCCServiceCamera kTCCServiceMicrophone kTCCServiceBluetoothAlways kTCCServiceMediaLibrary kTCCServiceSpeechRecognition kTCCServiceMotion kTCCServiceLocation kTCCServiceFocusStatus kTCCServiceSystemPolicyDesktopFolder kTCCServiceSystemPolicyDocumentsFolder kTCCServiceSystemPolicyDownloadsFolder kTCCServiceSystemPolicyNetworkVolumes kTCCServiceSystemPolicyRemovableVolumes kTCCServiceFileProviderDomain kTCCServiceFileProviderPresence; do
            sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
                "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, flags) VALUES ('$tcc_svc', '$tcc_client', 0, 2, 0, 1, 0)" 2>/dev/null || true
        done
    done

    # The controller granted TCC before starting the already function-tested GUI
    # daemon. Do not restart tccd here: doing so invalidates that live daemon's
    # WindowServer/TCC context immediately before the strict MCP probe.

    # Register Claude's MCP server from the same contract used by the other
    # platforms. The old literal omitted SESSION_IDLE_TTL_SECS and could drift
    # independently from the daemon environment. Codex's TOML is generated by
    # platforms.macos.vm_runtime.deploy_codex_config from this contract as well.
    PYTHONPATH="$_RB_SCRIPTS" python3 "$PIPELINE_DIR/scripts/macos/runtime.py" \
        mcp-config --driver "$CUA_DRIVER_PATH" --socket "$CUA_DAEMON_SOCK" \
        --coordinate-space "$CUA_COORD_SPACE" --coordinate-scale "$CUA_COORD_SCALE" \
        > "/tmp/_claude_json.json"
    sudo cp "/tmp/_claude_json.json" "$AGENT_HOME/.claude.json"
    sudo chown "$DEVAGENT_USER:staff" "$AGENT_HOME/.claude.json"
    rm -f "/tmp/_claude_json.json"

    sudo chown -R "$DEVAGENT_USER:staff" "$AGENT_RECREATION/.claude" "$AGENT_HOME/.claude"
else
    cp "/tmp/_claude_settings.json" "$AGENT_RECREATION/.claude/settings.json"
    cp "/tmp/_claude_settings.json" "$HOME/.claude/settings.json" 2>/dev/null || true
fi
rm -f "/tmp/_claude_settings.json"

# ── 2b. Verify devagent MCP access ──────────────────────────────────────

_verify_devagent_mcp() {
    if ! sudo -u "$DEVAGENT_USER" test -x "$CUA_DRIVER_PATH" 2>/dev/null; then
        echo "  ERROR: devagent cannot execute $CUA_DRIVER_PATH"
        return 1
    fi
    case "${AGENT_CLI:-claude}" in
        codex*)
            if ! sudo -u "$DEVAGENT_USER" grep -qF "[mcp_servers.desktop-control]" \
                    "$AGENT_HOME/.codex/config.toml" 2>/dev/null; then
                echo "  ERROR: devagent Codex config missing desktop-control MCP"
                return 1
            fi
            ;;
        *)
            if ! sudo -u "$DEVAGENT_USER" grep -q "desktop-control" \
                    "$AGENT_HOME/.claude.json" 2>/dev/null; then
                echo "  ERROR: devagent Claude config missing desktop-control MCP"
                return 1
            fi
            ;;
    esac
    if ! sudo -u "$DEVAGENT_USER" -H env \
            "CUA_DRIVER_RS_COORDINATE_SPACE=$CUA_COORD_SPACE" \
            "CUA_DRIVER_RS_COORDINATE_SCALE=$CUA_COORD_SCALE" \
            "CUA_DRIVER_RS_MCP_FORCE_PROXY=1" \
            "CUA_DRIVER_RS_UPDATE_CHECK=0" \
            python3 "$MCP_PROBE_PATH" \
                --driver "$CUA_DRIVER_PATH" --socket "$CUA_DAEMON_SOCK" \
                --kind desktop --pid "$REF_PID" --include-descendants \
                --allow-windowless \
                --timeout 30 --attempts 3; then
        echo "  ERROR: devagent cannot observe the live reference through desktop-control MCP"
        return 1
    fi
    return 0
}

if [ "$USE_DEVAGENT" = true ]; then
    echo "[2b/4] Verifying devagent MCP access..."

    if _verify_devagent_mcp; then
        echo "  devagent MCP verified: live reference AX tree and screenshot are observable"
    else
        # Daemon may have gone idle (5-min timeout) during long build stage — restart and retry
        echo "  MCP verification failed — restarting CUA Driver daemon and retrying..."
        _start_cua_daemon "$CUA_DRIVER_PATH" "$CUA_DAEMON_SOCK"
        for _i in $(seq 1 10); do [ -S "$CUA_DAEMON_SOCK" ] && break; sleep 1; done
        if [ ! -S "$CUA_DAEMON_SOCK" ]; then
            echo "  FATAL: CUA Driver daemon failed to restart"
            exit 2
        fi
        sudo chgrp staff "$CUA_DAEMON_SOCK" \
            "$(dirname "$CUA_DAEMON_SOCK")" \
            "$(dirname "$(dirname "$CUA_DAEMON_SOCK")")" \
            "$(dirname "$(dirname "$(dirname "$CUA_DAEMON_SOCK")")")"
        sudo chmod 710 "$(dirname "$CUA_DAEMON_SOCK")" \
            "$(dirname "$(dirname "$CUA_DAEMON_SOCK")")" \
            "$(dirname "$(dirname "$(dirname "$CUA_DAEMON_SOCK")")")"
        sudo chmod 660 "$CUA_DAEMON_SOCK"
        if [ -n "${MAX_SCREENSHOT_DIM:-}" ]; then
            if "$CUA_DRIVER_PATH" call set_config "{\"max_image_dimension\": $MAX_SCREENSHOT_DIM}" 2>/dev/null; then
                echo "  CUA Driver max_image_dimension=$MAX_SCREENSHOT_DIM (re-applied)"
            else
                echo "  FATAL: failed to re-apply CUA Driver max_image_dimension"
                exit 2
            fi
        fi
        echo "  CUA Driver daemon restarted — retrying MCP verification..."
        _assert_cua_coord_space "$CUA_DRIVER_PATH" "$CUA_DAEMON_SOCK"

        if _verify_devagent_mcp; then
            echo "  devagent MCP verified after daemon restart"
        elif [ "${RB_CUA_PREFLIGHT_MODE:-strict}" = "warn" ]; then
            echo "  WARNING: devagent MCP verification FAILED after daemon restart"
            echo "  Continuing because RB_CUA_PREFLIGHT_MODE=warn"
        else
            echo "  FATAL: devagent MCP verification FAILED after daemon restart"
            echo "  Agent will not have observation tools — aborting recreation"
            exit 2
        fi
    fi
fi

# ── 3. Build and run prompt ───────────────────────────────────────────────

# Keep the infra ERR trap active through launcher preparation and artifact
# collection.  The agent's own status is captured explicitly below; only a
# verified, terminal model run without a usable artifact returns 1.

echo "[3/4] Starting recreation agent..."

# One release prompt, rendered by the shared five-platform template.
echo "[3/4] Recreation instruction: unified"
PROMPT=$(PYTHONPATH="$PIPELINE_DIR/scripts" \
    python3 "$PIPELINE_DIR/scripts/macos/runtime.py" render-prompt)

AGENT_EXTRA_ARGS=()
[ -n "${EFFORT:-}" ] && AGENT_EXTRA_ARGS+=(--effort "$EFFORT")
[ "${ENABLE_CUA_DRIVER:-true}" = "false" ] && AGENT_EXTRA_ARGS+=(--no-cua-driver)

CUA_DRIVER_FLAG=""
[ "${ENABLE_CUA_DRIVER:-true}" = "false" ] && CUA_DRIVER_FLAG="--no-cua-driver"

AGENT_LOG="$WORK_DIR/logs/recreation_agent.log"

# Write prompt to temp file (avoids single-quote injection in bash -c string,
# and keeps the whole prompt OUT of the process argv — passed to run.sh via
# --prompt-file, not --prompt "$(cat ...)").
PROMPT_FILE="/tmp/.pipeline_prompt_$$"
printf '%s' "$PROMPT" > "$PROMPT_FILE"
chmod 600 "$PROMPT_FILE"
INVOCATION_RUNNER="/tmp/rb_agent_invocation.py"
cp "$PIPELINE_DIR/scripts/core/agent_invocation.py" "$INVOCATION_RUNNER"
cp "$PIPELINE_DIR/scripts/core/tool_use_capture.py" /tmp/tool_use_capture.py
chmod 755 "$INVOCATION_RUNNER"
chmod 644 /tmp/tool_use_capture.py

# Preserve the submitted model identifier until the shared invocation boundary.
# Shell ``##*.`` is not provider-prefix removal: it also corrupts version dots in
# bare names (``grok-4.6`` -> ``6``, ``gemini-3.7-flash`` -> ``7-flash``) and
# provider-qualified names (for example ``vendor.model-4.2`` -> ``2``). The proxy
# owns upstream routing, and the shared runner owns CLI-specific decoration such
# as Claude Code's ``[1m]`` suffix and Codex's exact ``openai.`` prefix removal.
case "${AGENT_CLI:-claude}" in
    codex*) CLIENT_MODEL="$MODEL" ;;
    *) CLIENT_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}" ;;
esac
CRED_PROXY_DUMMY_TOKEN="proxy"

if [ "$USE_DEVAGENT" = true ]; then
    echo "  Running as $DEVAGENT_USER (no sudo, isolated)"

    # The isolation account cannot traverse a mode-700 login home to reach scaffold/.
    # Lock down everything under PIPELINE_DIR, then selectively open scaffold/
    SCAFFOLD_HOME="$(dirname "$PIPELINE_DIR")"
    sudo chmod -R go-rwx "$PIPELINE_DIR" 2>/dev/null || true
    # Traversal must be re-opened on EVERY component of the path to scaffold/. The recursive
    # go-rwx above strips o+x from scripts/ and scripts/macos/ as well, and those two were
    # missing here -- so devagent could reach neither, and bash reports an unreachable PARENT
    # as "Permission denied" on the script itself. The scaffold also imports the extracted
    # macOS runtime helper during its MCP preflight, so expose that one trusted source file
    # read-only while keeping the rest of the pipeline private.
    sudo chmod a+x "$SCAFFOLD_HOME" "$PIPELINE_DIR" \
        "$PIPELINE_DIR/scripts" "$PIPELINE_DIR/scripts/macos" 2>/dev/null || true
    sudo chmod -R a+rX "$PIPELINE_DIR/scripts/macos/scaffold" 2>/dev/null || true
    sudo chmod a+r "$PIPELINE_DIR/scripts/macos/runtime.py" 2>/dev/null || true

    # Prove it before launching, as devagent itself: `test -x` checks the exec bit AND every
    # parent's traversal in one go. Failing here names the cause; failing later looks like the
    # agent produced nothing.
    if ! sudo -u "$DEVAGENT_USER" test -x "$SCAFFOLD_DIR/run.sh" 2>/dev/null; then
        echo "  ERROR: $DEVAGENT_USER cannot execute $SCAFFOLD_DIR/run.sh" >&2
        echo "  (exec bit or a parent directory's traversal -- check every component from" >&2
        echo "   $SCAFFOLD_HOME down, not just the scaffold dir)" >&2
        sudo -u "$DEVAGENT_USER" ls -ld "$SCAFFOLD_HOME" "$PIPELINE_DIR" \
            "$PIPELINE_DIR/scripts" "$PIPELINE_DIR/scripts/macos" "$SCAFFOLD_DIR" 2>&1 | \
            sed 's/^/    /' >&2 || true
        exit 2
    fi
    if ! sudo -u "$DEVAGENT_USER" test -r "$PIPELINE_DIR/scripts/macos/runtime.py" 2>/dev/null; then
        echo "  ERROR: $DEVAGENT_USER cannot read the macOS runtime helper" >&2
        exit 2
    fi

    DEVAGENT_TRAJ="$AGENT_HOME/trajectory.jsonl"
    if [ "${RB_CAPTURE_TOOL_USE_SCREENSHOTS:-false}" = "true" ]; then
        sudo rm -rf "$AGENT_HOME/tool_use_screenshots"
        sudo -u "$DEVAGENT_USER" mkdir -p "$AGENT_HOME/tool_use_screenshots"
    fi

    # Prompt file must be readable by devagent but not world-readable.
    sudo chown "$DEVAGENT_USER" "$PROMPT_FILE" 2>/dev/null || true

    # ── recreation network LOCKDOWN (only model proxy port) — DEFENSIVE re-assert ──
    # The recreation GLOBAL default-deny (NET_PHASE=recreation) is now applied at the
    # END of preparation setup.sh (§10), so a held target can be inspected in the exact
    # recreation network state, and a normal run enters recreation already locked down. We
    # re-assert it here (idempotent — pfctl -f replaces the ruleset) to GUARANTEE the
    # lockdown when recreation is entered WITHOUT preparation having run in this invocation
    # (resume_from=recreation), and to re-confirm it right before the agent launches.
    # NET_PHASE=recreation = GLOBAL block (root/daemons/NSURLSession too), leaving only
    # the model proxy channel (BASE_URL) + SSH + DNS; devagent reaches the model solely
    # via the loopback proxy (127.0.0.1, skip lo0). eval ax_eval re-applies full.
    # Fail-closed: if pf can't be locked down, abort as infra (exit 2) rather than run
    # recreation with the reference app still able to reach the net.
    echo "  [recreation] Ensuring recreation network lockdown (only model proxy port)..."
    if ! NET_PHASE=recreation BASE_URL="$BASE_URL" DEVAGENT_USER="$DEVAGENT_USER" \
         WORK_DIR="$WORK_DIR" BLOCK_HARNESS_NET=true \
         bash "$SCRIPT_DIR/restrict_network.sh"; then
        echo "  ERROR: could not apply recreation network lockdown — refusing to run recreation with the reference app online"
        exit 2
    fi

    # Both agents use the deployment-owned endpoint; no credential-bearing
    # gateway is reachable from the benchmark target.
    AGENT_PROXY_BASE_URL="$BASE_URL"
    case "${AGENT_CLI:-claude}" in
        codex*)
            echo "  Codex model channel: $AGENT_PROXY_BASE_URL"
            ;;
        *)
            echo "  Claude model channel: $AGENT_PROXY_BASE_URL"
            ;;
    esac

    # Build compact env exports before bash -c to avoid set -u crash on unset vars
    _COMPACT_EXPORTS=""
    _COMPACT_EXPORTS="${_COMPACT_EXPORTS}export CONTEXT_1M='${CONTEXT_1M:-false}';"
    if [ "${CLAUDE_CODE_AUTO_COMPACT_WINDOW+set}" = "set" ]; then
        _COMPACT_EXPORTS="${_COMPACT_EXPORTS}export CLAUDE_CODE_AUTO_COMPACT_WINDOW='${CLAUDE_CODE_AUTO_COMPACT_WINDOW}';"
    fi
    if [ -n "${MAX_TOKENS_LIMIT:-}" ]; then
        # sudo -u starts a clean devagent environment. Translate the common
        # five-platform knob to the variable consumed by Claude Code here,
        # otherwise macOS silently falls back to the CLI default output cap.
        _COMPACT_EXPORTS="${_COMPACT_EXPORTS}export CLAUDE_CODE_MAX_OUTPUT_TOKENS='${MAX_TOKENS_LIMIT}';"
    fi

    # Run the agent as the isolation account while preserving the GUI session.
    # NOTE: only NON-SECRET args cross the devagent boundary — loopback base-url,
    # dummy api-key, bare model name, and the prompt via --prompt-file (never on
    # argv). Upstream routing/thinking/output limits are owned by the deployment endpoint.
    # `sudo -u` resets the environment. Export the daemon's coordinate values so run.sh can
    # verify the canonical config without inventing defaults of its own.

    # Dependency sharing (chmod a+rwX on the login account's tool caches) is
    # done in preparation setup.sh §8d — by the time we launch devagent here those caches
    # are already readable+writable, so devagent can reuse the pre-installed toolchain
    # + build-populated crate/module caches offline. We only set the env below.
    # macOS starts per-user launchd helpers (for example distnoted, lsd and cfprefsd) the
    # first time the isolated account is used.  They predate the rollout and launchd
    # immediately respawns them after a UID-wide pkill, so "the UID has no processes" is
    # neither attainable nor the boundary we need.  Snapshot the trusted baseline now and
    # later require every *new live* process to be gone.  Zombies have no executable context
    # or writable file descriptors and are reaped by their parent, so they are not blockers.
    _DEVAGENT_BASELINE_PIDS=" $(sudo pgrep -u "$DEVAGENT_USER" 2>/dev/null | tr '\n' ' ' || true)"
    _DEVAGENT_BASELINE_PIDS="${_DEVAGENT_BASELINE_PIDS} "
    devagent_new_live_pids() {
        local _pid _state _ppid _comm
        for _pid in $(sudo pgrep -u "$DEVAGENT_USER" 2>/dev/null || true); do
            case "$_DEVAGENT_BASELINE_PIDS" in
                *" $_pid "*) continue ;;
            esac
            _state="$(sudo ps -p "$_pid" -o state= 2>/dev/null | tr -d '[:space:]' | cut -c1)"
            [ -n "$_state" ] || continue
            [ "$_state" = "Z" ] && continue
            # Spotlight workers are launchd-owned per-user helpers.  They may first appear while
            # the rollout writes source files, and launchd immediately replaces them with a new
            # PID after SIGKILL.  Treat only the exact SIP-protected Apple binary reparented to
            # launchd as trusted; arbitrary PPID-1 or same-basename processes remain blockers.
            _ppid="$(sudo ps -p "$_pid" -o ppid= 2>/dev/null | tr -d '[:space:]')"
            _comm="$(sudo ps -p "$_pid" -o comm= 2>/dev/null | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
            if [ "$_ppid" = "1" ] && [ "$_comm" = "/System/Library/Frameworks/CoreServices.framework/Frameworks/Metadata.framework/Versions/A/Support/mdworker_shared" ]; then
                continue
            fi
            printf '%s\n' "$_pid"
        done
    }

    sudo -u "$DEVAGENT_USER" -H bash -c "
        export HOME='$AGENT_HOME'
        export CUA_DAEMON_SOCK='$CUA_DAEMON_SOCK'
        export CUA_DRIVER_RS_COORDINATE_SPACE='$CUA_COORD_SPACE'
        export CUA_DRIVER_RS_COORDINATE_SCALE='$CUA_COORD_SCALE'
        # sudo resets the controller/target environment at this boundary. Without these two
        # explicit exports run.sh silently falls back to AGENT_CLI=claude even when the
        # controller and .runtime_env selected codex.
        export AGENT_CLI='${AGENT_CLI:-claude}'
        export SCAFFOLD_VERSION='${SCAFFOLD_VERSION:-}'
        export RB_AGENT_INVOCATION_RUNNER='$INVOCATION_RUNNER'
        export RB_CAPTURE_TOOL_USE_SCREENSHOTS='${RB_CAPTURE_TOOL_USE_SCREENSHOTS:-false}'
        export RB_TOOL_USE_SCREENSHOT_DIR='$AGENT_HOME/tool_use_screenshots'
        # Both proxy paths accept a non-secret client token. sudo resets the controller
        # environment here, so restore only the dummy value.
        export OPENAI_API_KEY='$CRED_PROXY_DUMMY_TOKEN'
        # sudo starts a fresh environment; carry the shared 30-minute model-request clock
        # explicitly or scaffold/run.sh falls back independently.
        export API_TIMEOUT='${API_TIMEOUT:-1800}'
        # sudo starts a fresh environment. Keep the target-loopback model tunnel out
        # of any proxy inherited by the agent runtime.
        unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
        export NO_PROXY='127.0.0.1,localhost'
        export no_proxy='127.0.0.1,localhost'
        # Bound every MCP tool call. Unset means unbounded on this claude generation (verified
        # 2.1.237: a 600s stub hang was NOT bounded), and one hung get_window_state has
        # eaten 4-5h of a 6h budget. The agent survives a timeout and reports it.
        export MCP_TOOL_TIMEOUT=${RB_MCP_TOOL_TIMEOUT:-180000}
        echo "MCP_TOOL_TIMEOUT=${MCP_TOOL_TIMEOUT:-UNSET}"
        $_COMPACT_EXPORTS
        cd '$AGENT_WORKSPACE'
        # Point the isolation account at the login account's shared toolchains/caches.
        # RESETS env, so set here; OFFLINE flags make cargo/go use only the cache (no net).
        export CARGO_HOME='$HARNESS_HOME/.cargo'
        export RUSTUP_HOME='$HARNESS_HOME/.rustup'
        export BUN_INSTALL='$HARNESS_HOME/.bun'
        export GOPATH='$HARNESS_HOME/go'
        export CARGO_NET_OFFLINE='true'
        export GOPROXY='off'
        export PATH='$HARNESS_HOME/.cargo/bin:$HARNESS_HOME/.bun/bin:$HARNESS_HOME/go/bin:/usr/local/bin:$HARNESS_HOME/.local/bin:$HARNESS_HOME/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Library/Apple/usr/bin'
        '$SCAFFOLD_DIR/run.sh' \
            --model '$CLIENT_MODEL' \
            --api-key '$CRED_PROXY_DUMMY_TOKEN' \
            --base-url '$AGENT_PROXY_BASE_URL' \
            --prompt-file '$PROMPT_FILE' \
            ${MAX_TURNS:+--max-turns '$MAX_TURNS'} \
            --timeout '$TIMEOUT' \
            --output-file '$DEVAGENT_TRAJ' \
            ${EFFORT:+--effort '$EFFORT'} \
            ${CUA_DRIVER_FLAG:+$CUA_DRIVER_FLAG}
    " &
    AGENT_PID=$!
    AGENT_EXIT=0
    wait "$AGENT_PID" || AGENT_EXIT=$?

    # The foreground CLI may leave build servers or MCP children behind.  Terminate every live
    # process created after the trusted baseline and fail closed if one survives before copying
    # agent-owned bytes into the trusted result tree.  Do not kill the baseline launchd helpers:
    # macOS respawns them immediately, which made every short rollout fail this gate.
    for _stop_attempt in 1 2 3; do
        _remaining="$(devagent_new_live_pids)"
        if [ -z "$_remaining" ]; then
            break
        fi
        for _pid in $_remaining; do
            sudo kill -KILL "$_pid" 2>/dev/null || true
        done
        sleep 1
    done
    _remaining="$(devagent_new_live_pids)"
    if [ -n "$_remaining" ]; then
        echo "FATAL: rollout-created devagent processes survived shutdown; refusing to snapshot: ${_remaining//$'\n'/ }"
        for _pid in $_remaining; do
            sudo ps -p "$_pid" -o pid=,ppid=,state=,etime=,comm= 2>/dev/null || true
        done
        exit 2
    fi

    # Restore scaffold/ and PIPELINE_DIR permissions
    sudo chmod -R o-rwx "$PIPELINE_DIR/scripts/macos/scaffold" 2>/dev/null || true
    sudo chmod go-rwx "$PIPELINE_DIR/scripts/macos/runtime.py" 2>/dev/null || true
    sudo chmod 700 "$PIPELINE_DIR" 2>/dev/null || true

    sudo rm -f "$PROMPT_FILE"  # chowned to devagent above; /tmp sticky needs sudo

    # Copy trajectory back (stream-json format, aligned with Linux)
    sudo cp "$DEVAGENT_TRAJ" "$WORK_DIR/logs/recreation_trajectory.jsonl" 2>/dev/null || true
    sudo cp "$AGENT_HOME/stderr.log" "$WORK_DIR/logs/stderr.log" 2>/dev/null || true
    if [ -d "$AGENT_HOME/tool_use_screenshots" ]; then
        sudo cp -R "$AGENT_HOME/tool_use_screenshots" "$WORK_DIR/logs/" 2>/dev/null || true
    fi

    # Revoke devagent's write authority, copy into a private sibling, then publish by rename.  The
    # evaluator owns the snapshot and devagent has no group/world access even though both local
    # accounts normally belong to staff.
    sudo chown -R root:wheel "$AGENT_RECREATION"
    sudo chmod -R u+rwX,go-rwx "$AGENT_RECREATION"
    SNAPSHOT_DIR="${RECREATION_DIR}.snapshot.$$"
    sudo rm -rf "$SNAPSHOT_DIR"
    sudo mkdir -p "$SNAPSHOT_DIR"
    sudo chmod 700 "$SNAPSHOT_DIR"
    if ! sudo cp -R "$AGENT_RECREATION/." "$SNAPSHOT_DIR/"; then
        echo "FATAL: could not snapshot canonical recreation workspace"
        sudo rm -rf "$SNAPSHOT_DIR"
        exit 2
    fi
    sudo chown -R "$(whoami):staff" "$SNAPSHOT_DIR"
    sudo chmod -R u+rwX,go-rwx "$SNAPSHOT_DIR"
    sudo rm -rf "$RECREATION_DIR"
    sudo mv "$SNAPSHOT_DIR" "$RECREATION_DIR"
else
    cd "$RECREATION_DIR"
    export RB_AGENT_INVOCATION_RUNNER="$INVOCATION_RUNNER"
    export API_TIMEOUT="${API_TIMEOUT:-1800}"
    export MCP_TOOL_TIMEOUT=${RB_MCP_TOOL_TIMEOUT:-180000}
    echo "MCP_TOOL_TIMEOUT=${MCP_TOOL_TIMEOUT:-UNSET}"
    "$SCAFFOLD_DIR/run.sh" \
        --model "$CLIENT_MODEL" \
        --api-key "$API_KEY" \
        --base-url "$BASE_URL" \
        --prompt-file "$PROMPT_FILE" \
        ${MAX_TURNS:+--max-turns "$MAX_TURNS"} \
        --timeout "$TIMEOUT" \
        --output-file "$WORK_DIR/logs/recreation_trajectory.jsonl" \
        "${AGENT_EXTRA_ARGS[@]+"${AGENT_EXTRA_ARGS[@]}"}" &
    AGENT_PID=$!
    AGENT_EXIT=0
    wait "$AGENT_PID" || AGENT_EXIT=$?
fi

rm -f "$PROMPT_FILE" "$INVOCATION_RUNNER" /tmp/tool_use_capture.py
AGENT_VERDICT=$(PYTHONPATH="$PIPELINE_DIR/scripts" python3 -m core.agent_invocation terminal \
    --trajectory "$WORK_DIR/logs/recreation_trajectory.jsonl" \
    --native-rc "${AGENT_EXIT:-2}" --field status 2>/dev/null || echo infra_error)
AGENT_VERDICT_RC=$(PYTHONPATH="$PIPELINE_DIR/scripts" python3 -m core.agent_invocation terminal \
    --trajectory "$WORK_DIR/logs/recreation_trajectory.jsonl" \
    --native-rc "${AGENT_EXIT:-2}" --field exit-code 2>/dev/null || echo 2)

# Preserve external termination immediately.  Other terminal errors are
# resolved after inspecting the frozen artifact: a final API/stream failure can
# happen after the model has already produced a runnable app, and that candidate
# must be evaluated rather than discarded as a false infra retry.
echo "Agent verdict: $AGENT_VERDICT (native rc=${AGENT_EXIT:-2})"
[ "$AGENT_VERDICT_RC" = 143 ] && exit 143

# ── 4. Find recreated app ────────────────────────────────────────────────

echo "[4/4] Finding recreated app..."

RECREATED_APP=""
# Search the login account's copy first so RECREATED_APP is launchable by LaunchServices.
# The isolation-account path may exist but is not necessarily launchable by the GUI account.
SEARCH_DIRS=("$RECREATION_DIR/build" "$RECREATION_DIR")

for search_dir in "${SEARCH_DIRS[@]}"; do
    if [ -d "$search_dir" ]; then
        FOUND=$(find "$search_dir" -name "*.app" -type d 2>/dev/null | head -1 || true)
        if [ -n "$FOUND" ]; then
            RECREATED_APP="$FOUND"
            break
        fi
    fi
done

# macOS ships its recreation inside results.tar.gz, so record that archive boundary with the same
# shared descriptor used by the directory-based platforms (core/recreation_artifact.py).
_REC_ENTRY=""
[ -n "$RECREATED_APP" ] && _REC_ENTRY="$(basename "$RECREATED_APP")"
PYTHONPATH="${PIPELINE_DIR:-$(cd "$(dirname "$0")/../../.." && pwd)}/scripts" \
    python3 -m core.recreation_artifact write \
    --dest "$RECREATION_DIR" --platform macos --root . \
    ${_REC_ENTRY:+--entry "$_REC_ENTRY"} 2>&1 || \
    echo "  WARN: recreation manifest not written (non-fatal)"

if [ -z "$RECREATED_APP" ]; then
    # No .app found — check for launch.sh (Python/CMake/non-Xcode apps)
    REC_LAUNCH_SH=""
    for _search in "$RECREATION_DIR/launch.sh" "$RECREATION_DIR/src/launch.sh"; do
        if [ -f "$_search" ]; then
            REC_LAUNCH_SH="$_search"
            break
        fi
    done
    if [ -n "$REC_LAUNCH_SH" ]; then
        echo "  No .app bundle found — launch.sh exists: $REC_LAUNCH_SH"
        echo "  Recreation agent produced a non-.app GUI app (Python/CMake/etc.)"
        echo "RECREATED_APP=" > "$WORK_DIR/recreation_result.env"
    else
        echo "ERROR: No .app found and no launch.sh in recreation directory"
        echo "RECREATED_APP=" > "$WORK_DIR/recreation_result.env"
        echo "RECREATION_DIR=\"$RECREATION_DIR\"" >> "$WORK_DIR/recreation_result.env"
        [ "$AGENT_VERDICT_RC" = 2 ] && exit 2
        exit 1
    fi
else
    echo "  Recreated app: $RECREATED_APP"
    echo "RECREATED_APP=\"$RECREATED_APP\"" > "$WORK_DIR/recreation_result.env"
fi

echo "RECREATION_DIR=\"$RECREATION_DIR\"" >> "$WORK_DIR/recreation_result.env"

# Check for recreation build.sh and launch.sh
REC_BUILD_SH="$RECREATION_DIR/build.sh"
REC_LAUNCH_SH_PATH=""
for _search in "$RECREATION_DIR/launch.sh" "$RECREATION_DIR/src/launch.sh"; do
    [ -f "$_search" ] && REC_LAUNCH_SH_PATH="$_search" && break
done
if [ -f "$REC_BUILD_SH" ]; then
    echo "  Recreation build.sh: found"
    echo "REC_BUILD_SH=\"$REC_BUILD_SH\"" >> "$WORK_DIR/recreation_result.env"
else
    echo "  WARNING: No recreation build.sh found"
fi
if [ -n "$REC_LAUNCH_SH_PATH" ]; then
    echo "  Recreation launch.sh: found"
    echo "REC_LAUNCH_SH=\"$REC_LAUNCH_SH_PATH\"" >> "$WORK_DIR/recreation_result.env"
fi

# Enable web-class AX on the recreated app for eval scoring (Electron only)
inject_force_renderer_accessibility "$REC_LAUNCH_SH_PATH" "${RECREATED_APP:-}"

# ── Recreation output verification (aligned from Linux pipeline.py verify_recreation) ──
echo ""
echo "━━━ Recreation Output Verification ━━━"
REC_VERIFY_FAIL=0

# Detect a REAL sudo *command*, ignoring the word "sudo" inside comments and
# quoted/echoed help strings (e.g. echo "run: sudo xcode-select -s ..."), which
# is not an actual sudo invocation. A naive grep would false-fail such scripts.
# Strip quoted spans + comments, then require sudo at a command position.
has_real_sudo() {
    sed -E 's/"[^"]*"//g; s/'\''[^'\'']*'\''//g; s/#.*//' "$1" \
        | grep -Eq '(^|[^[:alnum:]_])sudo[[:space:]]'
}

# 1. build.sh or launch.sh exists
if [ ! -f "$RECREATION_DIR/build.sh" ] && [ ! -f "$RECREATION_DIR/launch.sh" ]; then
    echo "  FAIL: No build.sh or launch.sh found in recreation"
    REC_VERIFY_FAIL=1
else
    # Check whichever script exists
    for _script in "$RECREATION_DIR/build.sh" "$RECREATION_DIR/launch.sh"; do
        [ -f "$_script" ] || continue
        _sname=$(basename "$_script")

        # 2. No sudo (real command only; ignores comments + echoed help strings)
        if has_real_sudo "$_script"; then
            echo "  FAIL: $_sname contains a real sudo command"
            REC_VERIFY_FAIL=1
        fi

        # 3. No reference to hidden binary path (anti-cheat)
        HIDDEN_PATH=$(sudo cat /var/tmp/.pipeline_hidden_path 2>/dev/null || echo "")
        if [ -n "$HIDDEN_PATH" ] && grep -q "$HIDDEN_PATH" "$_script" 2>/dev/null; then
            echo "  FAIL: $_sname references hidden binary path (cheating detected)"
            REC_VERIFY_FAIL=1
        fi

        # 4. bash -n syntax check
        if ! bash -n "$_script" 2>/dev/null; then
            echo "  FAIL: $_sname has syntax errors"
            REC_VERIFY_FAIL=1
        fi
    done
fi

# 5. Source file count > 0
SRC_COUNT=$(find "$RECREATION_DIR" -type f \( \
    -name "*.swift" -o -name "*.m" -o -name "*.mm" \
    -o -name "*.c" -o -name "*.cpp" -o -name "*.h" \
    -o -name "*.rs" -o -name "*.py" -o -name "*.js" -o -name "*.ts" \
    \) 2>/dev/null | wc -l | tr -d ' ')
if [ "$SRC_COUNT" -eq 0 ] 2>/dev/null; then
    echo "  FAIL: No source files found (src_count=0)"
    REC_VERIFY_FAIL=1
else
    echo "  Source files: $SRC_COUNT"
fi

if [ "$REC_VERIFY_FAIL" -eq 0 ]; then
    echo "  All recreation output checks passed ✓"
else
    echo "  WARNING: Recreation output verification has failures"
    echo "  Eval (eval) will still run but scores may be affected."
fi

if [ "$AGENT_VERDICT_RC" = 2 ]; then
    echo "  WARN: terminal agent error occurred after a runnable recreation was produced; continuing to eval"
fi

echo ""
echo "Recreation complete."
echo "Finished: $(date)"
