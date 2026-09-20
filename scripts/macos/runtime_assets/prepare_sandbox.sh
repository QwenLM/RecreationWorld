#!/bin/bash
# prepare_sandbox.sh — Prepare a macOS target for pipeline execution
#
# Called by the macOS runtime after connecting over SSH and before deploying the
# platform pipeline.
#
# Optional env: MACOS_PASS (login keychain password), MACOS_DEVAGENT_PASSWORD

set -euo pipefail

log() { echo "[prepare] $*"; }

MACOS_PASS="${MACOS_PASS:-}"
DEVAGENT_USER="${DEVAGENT_USER:-devagent}"
DEVAGENT_PASSWORD="${MACOS_DEVAGENT_PASSWORD:-}"

# ── 1. Verify GUI session ──────────────────────────────────────────────────

log "Verifying GUI session..."
CONSOLE_USER=$(stat -f '%Su' /dev/console 2>/dev/null || echo "unknown")
if [ "$CONSOLE_USER" = "root" ] || [ "$CONSOLE_USER" = "unknown" ]; then
    log "  WARNING: Console user is '$CONSOLE_USER', GUI may not be active"
else
    log "  Console user: $CONSOLE_USER"
fi
if pgrep -x WindowServer >/dev/null 2>&1; then
    log "  WindowServer: running"
else
    log "  WARNING: WindowServer not running — CUA screenshots will fail"
fi

# ── 2. Disable Gatekeeper ──────────────────────────────────────────────────

log "Disabling Gatekeeper..."
sudo spctl --master-disable 2>/dev/null || true
GATE_STATUS=$(spctl --status 2>&1 || true)
log "  Gatekeeper: $GATE_STATUS"

# ── 3. Keychain unlock ─────────────────────────────────────────────────────

log "Unlocking keychains..."
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
if [ -n "$MACOS_PASS" ] && [ -f "$KEYCHAIN" ]; then
    security unlock-keychain -p "$MACOS_PASS" "$KEYCHAIN" 2>/dev/null && log "  login keychain: unlocked" || log "  login keychain: unlock failed (non-fatal)"
    security set-keychain-settings "$KEYCHAIN" 2>/dev/null || true
fi
# Devagent keychain
DEVAGENT_KEYCHAIN="/Users/$DEVAGENT_USER/Library/Keychains/login.keychain-db"
if [ -n "$DEVAGENT_PASSWORD" ] && [ -f "$DEVAGENT_KEYCHAIN" ]; then
    sudo -u "$DEVAGENT_USER" security unlock-keychain -p "$DEVAGENT_PASSWORD" "$DEVAGENT_KEYCHAIN" 2>/dev/null && log "  isolation-account keychain: unlocked" || true
    sudo -u "$DEVAGENT_USER" security set-keychain-settings "$DEVAGENT_KEYCHAIN" 2>/dev/null || true
fi

# ── 4. Prevent sleep / screensaver ─────────────────────────────────────────

log "Configuring power management..."
sudo pmset -a sleep 0 displaysleep 0 disksleep 0 2>/dev/null || true
defaults -currentHost write com.apple.screensaver idleTime 0 2>/dev/null || true
log "  Sleep disabled, screensaver off"

# ── 5. Xcode setup ────────────────────────────────────────────────────────

log "Configuring Xcode..."
if command -v xcodebuild >/dev/null 2>&1; then
    sudo xcodebuild -license accept 2>/dev/null || true
    log "  Xcode license: accepted"
    sudo xcode-select -s /Applications/Xcode.app/Contents/Developer 2>/dev/null || true

    # Code signing bypass
    XCCONFIG="$HOME/Library/Developer/Xcode/NoCodeSign.xcconfig"
    mkdir -p "$(dirname "$XCCONFIG")"
    cat > "$XCCONFIG" <<'EOF'
CODE_SIGNING_ALLOWED = NO
CODE_SIGNING_REQUIRED = NO
CODE_SIGN_IDENTITY =
EOF
    log "  NoCodeSign.xcconfig: created"

    # Devagent copy
    DEVAGENT_XCCONFIG="/Users/$DEVAGENT_USER/Library/Developer/Xcode/NoCodeSign.xcconfig"
    sudo mkdir -p "$(dirname "$DEVAGENT_XCCONFIG")"
    sudo cp "$XCCONFIG" "$DEVAGENT_XCCONFIG"
    sudo chown "$DEVAGENT_USER:staff" "$DEVAGENT_XCCONFIG" 2>/dev/null || true
else
    log "  WARNING: xcodebuild not found"
fi

# ── 6. Suppress macOS popups / notifications ───────────────────────────────

log "Suppressing macOS popups..."
# Quarantine warnings
defaults write com.apple.LaunchServices LSQuarantine -bool false 2>/dev/null || true
sudo -u "$DEVAGENT_USER" defaults write com.apple.LaunchServices LSQuarantine -bool false 2>/dev/null || true

# App Store auto-update
defaults write com.apple.commerce AutoUpdate -bool false 2>/dev/null || true
defaults write com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false 2>/dev/null || true
defaults write com.apple.SoftwareUpdate AutomaticDownload -bool false 2>/dev/null || true

# Minimize notification banners
defaults write com.apple.notificationcenterui bannerTime 1 2>/dev/null || true

# Unload notification center (reduces screen clutter)
launchctl unload -w /System/Library/LaunchAgents/com.apple.notificationcenterui.plist 2>/dev/null || true

# Finder: no extension change warnings, no empty trash warnings
defaults write com.apple.finder FXEnableExtensionChangeWarning -bool false 2>/dev/null || true
defaults write com.apple.finder WarnOnEmptyTrash -bool false 2>/dev/null || true

# Crash reporter: no dialogs
defaults write com.apple.CrashReporter DialogType none 2>/dev/null || true
sudo -u "$DEVAGENT_USER" defaults write com.apple.CrashReporter DialogType none 2>/dev/null || true

# Kill popup-generating agents
killall UserNotificationCenter 2>/dev/null || true
killall CoreServicesUIAgent 2>/dev/null || true

log "  Popups suppressed"

# ── 7. Git config ──────────────────────────────────────────────────────────

log "Configuring git..."
git config --global user.name "RecreationBench" 2>/dev/null || true
git config --global user.email "bench@recreation.test" 2>/dev/null || true
git config --global http.postBuffer 524288000 2>/dev/null || true
git config --global core.compression 0 2>/dev/null || true
# Same for devagent
sudo -u "$DEVAGENT_USER" git config --global user.name "RecreationBench" 2>/dev/null || true
sudo -u "$DEVAGENT_USER" git config --global user.email "bench@recreation.test" 2>/dev/null || true
sudo -u "$DEVAGENT_USER" git config --global http.postBuffer 524288000 2>/dev/null || true
log "  Git configured"

# ── 8. Devagent user check ─────────────────────────────────────────────────

if ! id "$DEVAGENT_USER" &>/dev/null; then
    log "Creating $DEVAGENT_USER user..."
    if [ -z "$DEVAGENT_PASSWORD" ]; then
        log "ERROR: MACOS_DEVAGENT_PASSWORD is required when $DEVAGENT_USER does not exist"
        exit 1
    fi
    sudo sysadminctl -addUser "$DEVAGENT_USER" -password "$DEVAGENT_PASSWORD" 2>/dev/null || true
    sudo mkdir -p "/Users/$DEVAGENT_USER/.claude" "/Users/$DEVAGENT_USER/Recreation"
    sudo chown -R "$DEVAGENT_USER:staff" "/Users/$DEVAGENT_USER"
    log "  $DEVAGENT_USER user created"
else
    log "  $DEVAGENT_USER user exists (uid=$(id -u "$DEVAGENT_USER"))"
fi

# The recreation principal must remain unprivileged even when a prepared environment
# is reused, so account creation alone is insufficient.
if id -Gn "$DEVAGENT_USER" | tr ' ' '\n' | grep -qx admin; then
    log "  Removing $DEVAGENT_USER from admin group..."
    sudo dseditgroup -o edit -d "$DEVAGENT_USER" -t user admin
fi
if id -Gn "$DEVAGENT_USER" | tr ' ' '\n' | grep -qx admin; then
    log "ERROR: $DEVAGENT_USER is still an administrator"
    exit 1
fi
log "  $DEVAGENT_USER is a standard user"

# ── 9. Claude Code version check ─────────────────────────────────────────

REQUIRED_CC_VERSION="2.1.177"
CURRENT_CC_VERSION=$(claude --version 2>/dev/null | awk '{print $1}' || echo "")
if [ "$CURRENT_CC_VERSION" != "$REQUIRED_CC_VERSION" ]; then
    log "Upgrading Claude Code: ${CURRENT_CC_VERSION:-not found} -> $REQUIRED_CC_VERSION"
    npm install -g "@anthropic-ai/claude-code@${REQUIRED_CC_VERSION}" || {
        log "ERROR: Claude Code upgrade failed"
        exit 1
    }
    log "  Claude Code upgraded to $REQUIRED_CC_VERSION"
else
    log "  Claude Code $REQUIRED_CC_VERSION already installed"
fi
CURRENT_CC_VERSION=$(claude --version 2>/dev/null | awk '{print $1}' || echo "")
if [ "$CURRENT_CC_VERSION" != "$REQUIRED_CC_VERSION" ]; then
    log "ERROR: Claude Code version ${CURRENT_CC_VERSION:-missing}; expected $REQUIRED_CC_VERSION"
    exit 1
fi

log "Sandbox preparation complete"
