#!/bin/bash
# Install and verify dependencies on a prepared macOS target.
# Called by platforms/macos/vm_runtime.py via SSH.
set -euo pipefail

log() { echo "[fix-deps] $*"; }

PYTHON3="$(command -v python3 || true)"
REQUIREMENTS_FILE="${RB_MACOS_REQUIREMENTS_FILE:-}"
DEVAGENT_USER="${DEVAGENT_USER:-devagent}"
[ -n "$PYTHON3" ] || { log "ERROR: python3 is not installed"; exit 1; }
[ -f "$REQUIREMENTS_FILE" ] || {
    log "ERROR: macOS requirements file not found: $REQUIREMENTS_FILE"
    exit 1
}

log "Installing declared Python requirements..."
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}" \
    "$PYTHON3" -m pip install --user --requirement "$REQUIREMENTS_FILE"
for module in pytest yaml ApplicationServices Cocoa Quartz; do
    "$PYTHON3" /tmp/import_probe.py "$module"
done

# Ensure Claude Code is available for the isolation account when selected.

log "Checking Claude Code availability..."

CLAUDE_BIN=$(which claude 2>/dev/null || echo "")
if [ -z "$CLAUDE_BIN" ]; then
    # Check common install locations
    for loc in /usr/local/bin/claude "$HOME/.local/bin/claude" "$HOME/.claude/bin/claude"; do
        if [ -x "$loc" ]; then
            CLAUDE_BIN="$loc"
            break
        fi
    done
fi

if [ -n "$CLAUDE_BIN" ]; then
    log "  Claude Code found at: $CLAUDE_BIN"
    # Ensure devagent can also access it
    DEVAGENT_HOME=$(dscl . -read "/Users/$DEVAGENT_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}' || true)
    DEVAGENT_HOME="${DEVAGENT_HOME:-/Users/$DEVAGENT_USER}"
    if [ -d "$DEVAGENT_HOME" ]; then
        sudo mkdir -p "$DEVAGENT_HOME/.local/bin" 2>/dev/null || true
        sudo ln -sf "$CLAUDE_BIN" "$DEVAGENT_HOME/.local/bin/claude" 2>/dev/null || true
    fi
else
    log "  WARN: Claude Code not found — may need to be installed separately"
fi

# Verify PATH includes common locations for the isolation account.

DEVAGENT_HOME=$(dscl . -read "/Users/$DEVAGENT_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}' || true)
DEVAGENT_HOME="${DEVAGENT_HOME:-/Users/$DEVAGENT_USER}"
if [ -d "$DEVAGENT_HOME" ]; then
    PROFILE="$DEVAGENT_HOME/.zprofile"
    if [ ! -f "$PROFILE" ] || ! grep -q ".local/bin" "$PROFILE" 2>/dev/null; then
        sudo tee -a "$PROFILE" > /dev/null << 'PATHFIX'
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
PATHFIX
        sudo chown "$DEVAGENT_USER:staff" "$PROFILE" 2>/dev/null || true
        log "  $DEVAGENT_USER PATH updated in .zprofile"
    fi
fi

log "All declared dependencies are available"
