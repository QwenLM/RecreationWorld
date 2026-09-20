#!/bin/bash
# Deploy qwen-cua-driver 0.7.3 in relative-coordinate mode on a macOS target.
# 0.7.3 note: relative mode keeps QUERY output in ABSOLUTE pixels (normalize_result is a
#   no-op); the model is told coords are 0-1000 purely via tool-schema descriptions
#   (rewrite_coord_desc). So the self-check (§7) verifies via tools/list wording, NOT via a
#   screenshot_width of 1000 (that would indicate an OLD pre-0.7.1 driver).
# Called by platforms/macos/vm_runtime.py via SSH.
#
# Usage:
#   bash deploy_cua_driver.sh [--app /path/to/QwenCuaDriver.app]
#
# If --app is given, installs that app bundle to /Applications.
# Otherwise checks if QwenCuaDriver.app is already pre-installed.
#
# NOTE: relative-coordinate mode is enabled via CUA_DRIVER_RS_COORDINATE_SPACE=1
#       in the devagent MCP env (see section 5 below).
set -euo pipefail

CUA_APP="/Applications/QwenCuaDriver.app"
CUA_BIN="$CUA_APP/Contents/MacOS/qwen-cua-driver"
SOCK_DIR="$HOME/Library/Caches/qwen-cua-driver"
SOCK="$SOCK_DIR/qwen-cua-driver.sock"
APP_SRC=""

# Relative-coordinate mode MUST be seeded into the DAEMON's env, not just the
# MCP front-end's .claude.json env. The `mcp` command proxies to the running
# daemon (com.qwencode.cua-driver identity), and the coordinate transforms
# (denormalize_args / normalize_result / rewrite_coord_desc) take effect in the
# daemon (main.rs seeds from env at startup). A setting only in the proxy env is
# a no-op on the proxy path — same failure mode the fork's `compat` had before it
# was forwarded. So export it for every `serve` launch below.
COORD_ENV="CUA_DRIVER_RS_COORDINATE_SPACE=${CUA_DRIVER_RS_COORDINATE_SPACE:-1} CUA_DRIVER_RS_COORDINATE_SCALE=${CUA_DRIVER_RS_COORDINATE_SCALE:-1000} CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS=${CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS:-86400} CUA_DRIVER_RS_UPDATE_CHECK=${CUA_DRIVER_RS_UPDATE_CHECK:-0}"

log() { echo "[cua-deploy] $*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app) APP_SRC="$2"; shift 2 ;;
        --binary) APP_SRC="$2"; shift 2 ;;  # legacy alias (now expects a .app bundle dir)
        *) log "Unknown arg: $1"; exit 1 ;;
    esac
done

# ── 1. Install app bundle ──────────────────────────────────────────────────
# The fork ships a CI-signed QwenCuaDriver.app (bundle id com.qwencode.cua-driver)
# with its own Info.plist, so we install it wholesale rather than fabricating one.

if [ -n "$APP_SRC" ]; then
    log "Installing qwen-cua-driver app bundle from $APP_SRC..."
    if [ ! -d "$APP_SRC" ]; then
        log "ERROR: --app must point to a QwenCuaDriver.app bundle dir: $APP_SRC"
        exit 1
    fi
    sudo rm -rf "$CUA_APP"
    sudo cp -R "$APP_SRC" "$CUA_APP"
    sudo chmod +x "$CUA_BIN"
    log "  App installed at $CUA_APP"
elif [ -x "$CUA_BIN" ]; then
    log "qwen-cua-driver already installed at $CUA_BIN"
else
    log "ERROR: qwen-cua-driver not found and no --app provided"
    exit 1
fi

# Symlink into PATH for convenience
mkdir -p "$HOME/.local/bin"
ln -sf "$CUA_BIN" "$HOME/.local/bin/qwen-cua-driver" 2>/dev/null || true

# ── 1b. Ad-hoc code sign the app bundle ───────────────────────────────────
# Without a valid code signature, macOS blocks NSWorkspace/Accessibility APIs
# (list_apps, get_accessibility_tree, launch_app fail with "Resource" errors).
# Ad-hoc signing (-s -) is sufficient for TCC to trust the binary.
log "Ad-hoc code signing QwenCuaDriver.app..."
sudo codesign --force --deep --sign - "$CUA_APP" 2>&1 || log "  WARN: codesign failed (may already be signed)"
log "  Code signature: $(codesign -dv "$CUA_APP" 2>&1 | head -3 || echo 'unknown')"

# ── 2. Grant TCC permissions (Accessibility + ScreenCapture) ───────────────

log "Granting TCC permissions..."

TCC_DB="/Library/Application Support/com.apple.TCC/TCC.db"
# Detect bundle ID from the installed .app, fallback to com.qwencode.cua-driver
BUNDLE_ID="com.qwencode.cua-driver"
if [ -f /Applications/QwenCuaDriver.app/Contents/Info.plist ]; then
    BUNDLE_ID=$(defaults read /Applications/QwenCuaDriver.app/Contents/Info.plist CFBundleIdentifier 2>/dev/null || echo "com.qwencode.cua-driver")
fi

# Get the current user's csreq (code signing requirement) — use empty for unsigned
CSREQ_BLOB=""

grant_tcc() {
    local service="$1"
    # Grant by bundle ID (client_type=0)
    sudo sqlite3 "$TCC_DB" \
        "DELETE FROM access WHERE service='$service' AND client='$BUNDLE_ID';" 2>/dev/null || true
    sudo sqlite3 "$TCC_DB" \
        "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, flags) VALUES ('$service', '$BUNDLE_ID', 0, 2, 0, 1, 0);" 2>/dev/null || true
    # Grant by binary path (client_type=1) for command-line launches.
    sudo sqlite3 "$TCC_DB" \
        "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, flags) VALUES ('$service', '$CUA_BIN', 1, 2, 0, 1, 0);" 2>/dev/null || true
}

for svc in kTCCServiceAccessibility kTCCServiceScreenCapture kTCCServiceAppleEvents kTCCServicePostEvent kTCCServiceListenEvent kTCCServiceSystemPolicyAllFiles kTCCServiceDeveloperTool kTCCServiceCalendar kTCCServiceAddressBook kTCCServiceContactsFull kTCCServiceContactsLimited kTCCServiceReminders kTCCServicePhotos kTCCServicePhotosAdd kTCCServiceCamera kTCCServiceMicrophone kTCCServiceBluetoothAlways kTCCServiceMediaLibrary kTCCServiceSpeechRecognition kTCCServiceMotion kTCCServiceLocation kTCCServiceFocusStatus kTCCServiceSystemPolicyDesktopFolder kTCCServiceSystemPolicyDocumentsFolder kTCCServiceSystemPolicyDownloadsFolder kTCCServiceSystemPolicyNetworkVolumes kTCCServiceSystemPolicyRemovableVolumes kTCCServiceFileProviderDomain kTCCServiceFileProviderPresence; do
    grant_tcc "$svc"
    log "  TCC granted: $svc"
done

# Also grant for the isolated agent account.
DEVAGENT_USER="${DEVAGENT_USER:-devagent}"
DEVAGENT_HOME=$(dscl . -read "/Users/$DEVAGENT_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}' || true)
DEVAGENT_HOME="${DEVAGENT_HOME:-/Users/$DEVAGENT_USER}"
if [ -d "$DEVAGENT_HOME" ]; then
    DEVAGENT_APPS=("com.anthropic.claude-code" "/usr/local/bin/claude")
    for client in "${DEVAGENT_APPS[@]}"; do
        for svc in kTCCServiceAccessibility kTCCServiceScreenCapture kTCCServiceAppleEvents kTCCServicePostEvent kTCCServiceListenEvent kTCCServiceSystemPolicyAllFiles kTCCServiceDeveloperTool kTCCServiceCalendar kTCCServiceAddressBook kTCCServiceContactsFull kTCCServiceContactsLimited kTCCServiceReminders kTCCServicePhotos kTCCServicePhotosAdd kTCCServiceCamera kTCCServiceMicrophone kTCCServiceBluetoothAlways kTCCServiceMediaLibrary kTCCServiceSpeechRecognition kTCCServiceMotion kTCCServiceLocation kTCCServiceFocusStatus kTCCServiceSystemPolicyDesktopFolder kTCCServiceSystemPolicyDocumentsFolder kTCCServiceSystemPolicyDownloadsFolder kTCCServiceSystemPolicyNetworkVolumes kTCCServiceSystemPolicyRemovableVolumes kTCCServiceFileProviderDomain kTCCServiceFileProviderPresence; do
            sudo sqlite3 "$TCC_DB" \
                "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, flags) VALUES ('$svc', '$client', 0, 2, 0, 1, 0);" 2>/dev/null || true
        done
    done
    log "  TCC granted for $DEVAGENT_USER"
fi

# ── 3. Start CUA Driver daemon ────────────────────────────────────────────

log "Starting CUA Driver daemon..."

# Kill any existing daemon
pkill -f "qwen-cua-driver.*serve" 2>/dev/null || true
pkill -x qwen-cua-driver 2>/dev/null || true
sleep 1

# Clean stale socket
rm -f "$SOCK" 2>/dev/null || true
mkdir -p "$SOCK_DIR"

# Remove quarantine and register with LaunchServices
sudo xattr -rd com.apple.quarantine "$CUA_APP" 2>/dev/null || true
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$CUA_APP" 2>/dev/null || true

# In some remotely accessed macOS sessions, all processes run in session 0.
# The cua-driver Rust binary doesn't initialize NSApplication, so
# NSWorkspace/AXUIElement APIs fail with EAGAIN (os error 35) unless
# the daemon runs in the user's launchd domain context.
# Fix: `launchctl asuser <uid>` connects the daemon to the user's
# Mach services (LaunchServices, Accessibility).

GUI_UID=$(id -u)
AGENT_LABEL="com.qwencode.cua-driver"
AGENT_PLIST="$HOME/Library/LaunchAgents/${AGENT_LABEL}.plist"

# Pre-create daemon log
touch /tmp/cua-driver-daemon.log

# Remove any prior daemon
sudo launchctl bootout "gui/$GUI_UID/$AGENT_LABEL" 2>/dev/null || true
launchctl remove "$AGENT_LABEL" 2>/dev/null || true

STARTED=false

# 0.7.3 renamed the DEFAULT socket to ~/Library/Caches/qwen-cua-driver/qwen-cua-driver.sock,
# but this script (and the MCP client below) expect $SOCK. Pin serve to $SOCK explicitly on
# every launch method so daemon + client + wait-loop all agree regardless of driver version.
mkdir -p "$SOCK_DIR" 2>/dev/null || true

# Method 1: launchctl asuser — run in user's launchd domain
# This gives the daemon proper Mach service connections even in session 0.
if ! $STARTED; then
    log "  Starting daemon via launchctl asuser $GUI_UID..."
    sudo launchctl asuser "$GUI_UID" \
        bash -c "export HOME=$HOME; export $COORD_ENV; nohup $CUA_BIN serve --socket $SOCK > /tmp/cua-driver-daemon.log 2>&1 &" 2>&1 || true
    sleep 2
    if pgrep -f "qwen-cua-driver.*serve" >/dev/null 2>&1; then
        log "  Daemon started via asuser"
        STARTED=true
    else
        log "  WARN: asuser launch failed"
    fi
fi

# Method 2: launchctl bootstrap gui/<uid> + kickstart
if ! $STARTED; then
    log "  Trying launchctl bootstrap + kickstart..."
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$AGENT_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${CUA_BIN}</string>
        <string>serve</string>
        <string>--socket</string>
        <string>${SOCK}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CUA_DRIVER_RS_COORDINATE_SPACE</key>
        <string>${CUA_DRIVER_RS_COORDINATE_SPACE:-1}</string>
        <key>CUA_DRIVER_RS_COORDINATE_SCALE</key>
        <string>1000</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/cua-driver-daemon.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/cua-driver-daemon.log</string>
</dict>
</plist>
PLIST
    if sudo launchctl bootstrap "gui/$GUI_UID" "$AGENT_PLIST" 2>/dev/null; then
        sudo launchctl kickstart -kp "gui/$GUI_UID/$AGENT_LABEL" 2>/dev/null || true
        sleep 1
        if pgrep -f "qwen-cua-driver.*serve" >/dev/null 2>&1; then
            log "  Daemon started via bootstrap+kickstart"
            STARTED=true
        fi
    fi
fi

# Method 3: open -a fallback
if ! $STARTED; then
    # `open` (LaunchServices) does NOT inherit shell env, and launchctl
    # setenv/asuser do NOT reach the aqua-launched app (verified on a live 14.7
    # target: asuser errors, plain setenv lands in the wrong domain -> daemon stays
    # pixel mode). `open --env KEY=VAL` DOES deliver -> use that.
    open --env "CUA_DRIVER_RS_COORDINATE_SPACE=${CUA_DRIVER_RS_COORDINATE_SPACE:-1}" \
         --env "CUA_DRIVER_RS_COORDINATE_SCALE=${CUA_DRIVER_RS_COORDINATE_SCALE:-1000}" \
         --env "CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS=${CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS:-86400}" \
         --env "CUA_DRIVER_RS_UPDATE_CHECK=${CUA_DRIVER_RS_UPDATE_CHECK:-0}" \
         -n -g -a QwenCuaDriver --args serve --socket "$SOCK" 2>/dev/null || \
    open --env "CUA_DRIVER_RS_COORDINATE_SPACE=${CUA_DRIVER_RS_COORDINATE_SPACE:-1}" \
         --env "CUA_DRIVER_RS_COORDINATE_SCALE=${CUA_DRIVER_RS_COORDINATE_SCALE:-1000}" \
         --env "CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS=${CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS:-86400}" \
         --env "CUA_DRIVER_RS_UPDATE_CHECK=${CUA_DRIVER_RS_UPDATE_CHECK:-0}" \
         -n -g -a "$CUA_APP" --args serve --socket "$SOCK" 2>/dev/null || true
    sleep 1
    if pgrep -f "qwen-cua-driver.*serve" >/dev/null 2>&1; then
        log "  Daemon started via open -a --env"
        STARTED=true
    fi
fi

# Method 4: direct nohup as last resort
if ! $STARTED; then
    env $COORD_ENV nohup "$CUA_BIN" serve --socket "$SOCK" > /tmp/cua-driver-daemon.log 2>&1 &
    log "  Daemon started via nohup (last resort)"
    STARTED=true
fi

# Wait for socket
log "  Waiting for daemon socket..."
for i in $(seq 1 30); do
    if [ -S "$SOCK" ]; then
        log "  Socket ready after ${i}s"
        break
    fi
    sleep 1
done

if [ ! -S "$SOCK" ]; then
    log "ERROR: Daemon socket not created after 30s"
    log "  Checking process..."
    ps aux | grep -i cua || true
    cat /tmp/cua-driver.log 2>/dev/null | tail -20 || true
    exit 1
fi

# ── 4. Make the daemon socket reachable by the recreation principal ────────

# The login account owns the privileged daemon while the isolation account owns the
# MCP bridge. Both are members of staff, so group access is sufficient; world-writable
# 0777 let any local process drive the desktop. The runtime stage owns agent config.
sudo chgrp staff "$HOME/Library" "$HOME/Library/Caches" "$SOCK_DIR" "$SOCK"
sudo chmod 710 "$HOME/Library" "$HOME/Library/Caches" "$SOCK_DIR"
sudo chmod 660 "$SOCK"
log "  Socket permissions set (staff group only)"

# ── 5. Verify ──────────────────────────────────────────────────────────────

log "Verifying qwen-cua-driver..."
DAEMON_PID=$(pgrep -f "qwen-cua-driver" | head -1 || true)
if [ -n "$DAEMON_PID" ]; then
    DAEMON_SESS=$(ps -p $DAEMON_PID -o sess= 2>/dev/null || echo unknown)
    log "  Daemon PID: $DAEMON_PID"
    log "  Socket: $SOCK ($(ls -la "$SOCK" 2>/dev/null | awk '{print $1}'))"
    log "  Daemon session: $DAEMON_SESS"
    log "  Daemon env (LaunchServices): $(ps -p $DAEMON_PID -Eww 2>/dev/null | grep -o 'Apple_PubSub[^ ]*' | head -1 || echo 'not set')"
    log "  Daemon log (last 5 lines):"
    tail -5 /tmp/cua-driver-daemon.log 2>/dev/null | while read -r line; do log "    $line"; done || true
    timeout 5 "$CUA_BIN" doctor 2>&1 | head -20 | while read -r line; do log "  [doctor] $line"; done || true
    # Test if NSWorkspace works AT ALL from this environment.
    log "  NSWorkspace direct test (Python):"
    timeout 5 python3 /tmp/nsworkspace_probe.py 2>&1 \
        | while read -r line; do log "    $line"; done || true
    log "  launchctl service info:"
    sudo launchctl print "gui/$GUI_UID/$AGENT_LABEL" 2>&1 | head -10 | while read -r line; do log "    $line"; done || true

    # ── 7. Relative-coordinate mode self-check ──────────────────────────────
    # The coordinate transforms take effect in the DAEMON (seeded from env at
    # startup). Verify two ways:
    #   (a) root cause — the daemon process carries CUA_DRIVER_RS_COORDINATE_SPACE=1
    #   (b) functional — in 0.7.3 relative mode, query results stay ABSOLUTE pixels
    #       (normalize_result is a no-op); the ONLY functional signal is the tool
    #       SCHEMA: rewrite_coord_desc turns coord fields into "0-1000 normalized
    #       to window/screen ...". So call tools/list and look for that wording.
    #       (get_desktop_state screenshot_width should now be PIXELS, not 1000 —
    #        a 1000 would mean an OLD pre-0.7.1 driver.)
    log "Relative-coordinate self-check:"
    if ps -p "$DAEMON_PID" -Eww 2>/dev/null | tr ' ' '\n' | grep -qx 'CUA_DRIVER_RS_COORDINATE_SPACE=1'; then
        log "  [coord] (a) OK: daemon env carries CUA_DRIVER_RS_COORDINATE_SPACE=1"
    else
        log "  [coord] (a) WARN: could not see COORDINATE_SPACE=1 in daemon env (ps -Eww may be restricted) — see (b)"
    fi
    CUA_BIN="$CUA_BIN" SOCK="$SOCK" timeout 30 \
        python3 /tmp/cua_coordinate_probe.py 2>&1 \
        | while read -r line; do log "  $line"; done || true

    # Pin ASSERTION, not an installer. macOS gets its driver from an app bundle (pre-installed
    # or --app), so there is nothing here to "install version X" with — but the version is a
    # scoring-relevant variable (a 12-point difference has been measured between versions), and
    # windows/linux now pin theirs explicitly. Refusing a mismatch converts "silently whatever the
    # image shipped" into a loud failure, which is the same guarantee without a fake installer.
    _rb_cua_ref="${RB_CUA_DRIVER_REF:-}"
    _rb_cua_version=""
    case "$_rb_cua_ref" in
        qwen:*) _rb_cua_version="${_rb_cua_ref#qwen:}" ;;
        qwen-*) _rb_cua_version="${_rb_cua_ref#qwen-}" ;;
        "") ;;
        *)
            log "ERROR: macOS requires qwen cua-driver, got RB_CUA_DRIVER_REF=${_rb_cua_ref}"
            exit 1
            ;;
    esac
    _rb_cua_version="${_rb_cua_version%:norm}"
    _rb_cua_version="${_rb_cua_version%+norm}"
    if [ -n "$_rb_cua_version" ]; then
        _got="$("$CUA_BIN" --version 2>/dev/null | tr -d '\r' | head -1)"
        if ! printf '%s' "$_got" | grep -qw -- "$_rb_cua_version"; then
            log "ERROR: cua-driver version mismatch — pinned ${_rb_cua_version}, installed '${_got:-unknown}'."
            log "       The driver version changes scores, so this run would not be comparable."
            log "       Rebuild the macOS image with the pinned bundle, pass --app for it, or"
            log "       change rb_cua_driver_ref if the pin itself should move."
            exit 1
        fi
        log "  CUA Driver version verified: ${_got}"
    fi

    log "  CUA Driver deployment complete"
    exit 0
else
    log "ERROR: CUA Driver daemon not running"
    tail -20 /tmp/cua-driver-daemon.log 2>/dev/null || true
    log "  launchctl blame:"
    sudo launchctl blame "gui/$GUI_UID/$AGENT_LABEL" 2>&1 || true
    exit 1
fi
