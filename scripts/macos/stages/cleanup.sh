#!/bin/bash
# cleanup.sh — Restore the target to clean state for the next app
#
# Preserves results in /var/tmp/pipeline_results/ for collection by the controller.
# Ensures the next app faces a pristine target with no leftover processes, network blocks,
# hidden files, or stale caches from the previous app.
#
# Required env vars:
#   APP_NAME, WORK_DIR
# Optional:
#   RECREATED_APP, HIDDEN_DIR, OFFICIAL_APP
set -o pipefail
trap '' SIGPIPE
export PATH="$HOME/local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIPELINE_DIR="${PIPELINE_DIR:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"

# Load previous stage results (validate syntax before sourcing)
_try_source() { [ -f "$1" ] && bash -n "$1" 2>/dev/null && source "$1"; }
_try_source "$WORK_DIR/prepare_result.env"
_try_source "$WORK_DIR/eval_result.env"
_try_source "$WORK_DIR/reference_result.env"

HIDDEN_PATH_FILE="/var/tmp/.pipeline_hidden_path"
if [ -f "$HIDDEN_PATH_FILE" ]; then
    HIDDEN_DIR=$(sudo cat "$HIDDEN_PATH_FILE" 2>/dev/null || true)
fi

RECREATION_DIR="${RECREATION_DIR:-$HOME/Recreation/${APP_NAME}}"
DEVAGENT_USER="${DEVAGENT_USER:-devagent}"
DEVAGENT_HOME="${DEVAGENT_HOME:-/Users/$DEVAGENT_USER}"

echo "========================================"
echo "  Cleanup — $APP_NAME"
echo "========================================"
echo "  Started: $(date)"
echo ""

# ── 1. Kill ALL app and agent processes ──────────────────────────────────

echo "[1/9] Killing processes..."

# Kill everything under devagent
sudo pkill -9 -u "$DEVAGENT_USER" 2>/dev/null || true

# Kill recreated app
if [ -n "${RECREATED_APP:-}" ] && [ -d "${RECREATED_APP:-}" ]; then
    REC_BINARY=$(defaults read "$RECREATED_APP/Contents/Info.plist" CFBundleExecutable 2>/dev/null || basename "$RECREATED_APP" .app)
    pkill -9 -x "${REC_BINARY:0:15}" 2>/dev/null || true
fi

# Kill reference app
REF_BINARY="${APP_BINARY:-$APP_NAME}"
if [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}" ]; then
    REF_BINARY=$(defaults read "$OFFICIAL_APP/Contents/Info.plist" CFBundleExecutable 2>/dev/null || echo "$APP_NAME")
elif [ -n "${HIDDEN_DIR:-}" ] && [ -d "${HIDDEN_DIR:-}" ]; then
    HIDDEN_APP=$(sudo find "$HIDDEN_DIR" -name "*.app" -type d -maxdepth 1 2>/dev/null | head -1 || true)
    [ -n "$HIDDEN_APP" ] && REF_BINARY=$(sudo defaults read "$HIDDEN_APP/Contents/Info.plist" CFBundleExecutable 2>/dev/null || echo "$APP_NAME")
fi
pkill -9 -x "${REF_BINARY:0:15}" 2>/dev/null || true

# Kill orphan desktop-control MCP servers.
pkill -9 -f "qwen-cua-driver" 2>/dev/null || true

sleep 1

# Kill leftover GUI apps from this or prior runs (System Settings, Preview, etc.)
DESKTOP_KILLED=""
for gui_app in $(osascript -e 'tell application "System Events" to get name of every process whose background only is false' 2>/dev/null | tr ',' '\n' | sed 's/^ *//;s/ *$//'); do
    case "$gui_app" in
        Finder|Terminal|loginwindow|"") continue ;;
    esac
    killall "$gui_app" 2>/dev/null && DESKTOP_KILLED="$DESKTOP_KILLED $gui_app"
done
[ -n "$DESKTOP_KILLED" ] && echo "  Closed leftover GUI apps:$DESKTOP_KILLED"
osascript -e 'tell application "Finder" to close every window' 2>/dev/null || true
killall NotificationCenter 2>/dev/null || true

echo "  done"

# ── 2. Restore network ──────────────────────────────────────────────────

echo "[2/9] Restoring network..."
sudo pfctl -d 2>/dev/null || true
sudo pfctl -f /etc/pf.conf 2>/dev/null || true

# Restore /etc/hosts
if [ -f /etc/hosts.pipeline_backup ]; then
    sudo mv /etc/hosts.pipeline_backup /etc/hosts 2>/dev/null || true
else
    sudo sed -i "" '/^127\.0\.0\.1.*\(github\|pypi\|gitlab\|bitbucket\|npmjs\|raw\.githubusercontent\|registry\.npmjs\|crates\.io\|cocoapods\|rubygems\|packagist\|maven\)/d' /etc/hosts 2>/dev/null || true
fi
echo "  done"

# ── 3. Restore blocked tools ────────────────────────────────────────────

echo "[3/9] Restoring tools..."
# Remove git shadow wrapper, restore real git
if [ -f /usr/local/bin/git.real ]; then
    sudo mv /usr/local/bin/git.real /usr/local/bin/git 2>/dev/null || true
fi
if [ -f /usr/bin/git.real ]; then
    sudo mv /usr/bin/git.real /usr/bin/git 2>/dev/null || true
fi

for tool in curl wget pip pip3 npm npx yarn brew; do
    if [ -f "/usr/local/bin/$tool" ] && grep -q "BLOCKED:" "/usr/local/bin/$tool" 2>/dev/null; then
        sudo rm -f "/usr/local/bin/$tool"
    fi
    for dir in /usr/local/bin /opt/homebrew/bin; do
        [ -f "$dir/$tool.blocked" ] && sudo mv "$dir/$tool.blocked" "$dir/$tool" 2>/dev/null || true
    done
done
echo "  done"

# ── 4. Restore hidden files ─────────────────────────────────────────────

echo "[4/9] Restoring hidden files..."
if [ -n "${HIDDEN_DIR:-}" ] && [ -d "${HIDDEN_DIR:-}" ]; then
    HIDDEN_APP=$(sudo find "$HIDDEN_DIR" -name "*.app" -type d -maxdepth 1 2>/dev/null | head -1 || true)
    if [ -n "$HIDDEN_APP" ] && [ -d "$HIDDEN_APP/Contents/MacOS" ]; then
        sudo find "$HIDDEN_APP/Contents/MacOS" -type f -exec chmod 755 {} \; 2>/dev/null || true
    fi
    if [ -n "${OFFICIAL_APP:-}" ]; then
        REF_PARENT=$(dirname "$OFFICIAL_APP")
        mkdir -p "$REF_PARENT"
        sudo mv "$HIDDEN_DIR"/*.app "$REF_PARENT/" 2>/dev/null || true
    fi
    sudo rm -rf "$HIDDEN_DIR" 2>/dev/null || true
elif [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}/Contents/MacOS" ]; then
    sudo find "$OFFICIAL_APP/Contents/MacOS" -type f -exec chmod 755 {} \; 2>/dev/null || true
fi
sudo rm -f /var/tmp/.pipeline_hidden_path 2>/dev/null || true
# Clean restored reference app temp dir (created by recreation.sh for LaunchServices)
sudo rm -rf "/var/tmp/.pipeline_refapp_${APP_NAME}" 2>/dev/null || true
echo "  done"

# ── 5. Clean work directory (keep /var/tmp results intact) ───────────────

echo "[5/9] Cleaning work directory..."
rm -rf "$WORK_DIR/reference" 2>/dev/null || true
rm -rf "$WORK_DIR/tests" 2>/dev/null || true
rm -rf "$WORK_DIR/instance" 2>/dev/null || true
rm -rf "$WORK_DIR/logs" 2>/dev/null || true
rm -f "$WORK_DIR/"*_result.env 2>/dev/null || true
rm -f "$WORK_DIR/.network_state" 2>/dev/null || true
rm -rf "$WORK_DIR/checkpoints" 2>/dev/null || true
rmdir "$WORK_DIR" 2>/dev/null || true
echo "  done"

# ── 6. Clean recreation directory ────────────────────────────────────────

echo "[6/9] Cleaning recreation directories..."
rm -rf "$RECREATION_DIR" 2>/dev/null || true
sudo rm -rf "$DEVAGENT_HOME/Recreation/${APP_NAME}" 2>/dev/null || true
echo "  done"

# ── 7. Clean devagent state (for next app isolation) ─────────────────────

echo "[7/9] Cleaning devagent state..."
# Claude Code projects/memory/plans (prevent info leaking between apps)
sudo rm -rf "$DEVAGENT_HOME/.claude" 2>/dev/null || true
# Keep .claude.json (MCP registration) but clean trajectories
sudo find "$DEVAGENT_HOME" -name "*.jsonl" -delete 2>/dev/null || true
# DerivedData from previous builds
sudo rm -rf "$DEVAGENT_HOME/Library/Developer/Xcode/DerivedData" 2>/dev/null || true
# CLI caches
sudo rm -rf "$DEVAGENT_HOME/Library/Caches/claude-cli-nodejs" 2>/dev/null || true
# SwiftPM caches from previous app
sudo rm -rf "$DEVAGENT_HOME/.swiftpm" 2>/dev/null || true
# Any stray build dirs
sudo find "$DEVAGENT_HOME" -maxdepth 1 -type d \( -name "*_build" -o -name "*_project" -o -name "tmp_*" \) -exec rm -rf {} + 2>/dev/null || true
sudo find "$DEVAGENT_HOME" -maxdepth 1 -type f \( -name "*.log" -o -name "*.txt" -o -name "*.pdf" \) -delete 2>/dev/null || true
echo "  done"

# ── 8. Clean login-account caches and state ────────────────────────────

echo "[8/9] Cleaning login-account caches..."
# DerivedData
rm -rf "$HOME/Library/Developer/Xcode/DerivedData" 2>/dev/null || true
# Claude Code session data (but keep MCP config)
rm -rf ~/.claude/memory/ ~/.claude/projects/ ~/.claude/plans/ ~/.claude/tasks/ 2>/dev/null || true
# codex' sessions/ too: it is the same class of state as .claude/projects, and a rollout left
# behind leaks the previous stage into the next -- it gets collected a second time, and
# `codex exec resume --last` can attach to the wrong session.
rm -rf "${CODEX_HOME:-$HOME/.codex}/sessions/" 2>/dev/null || true
rm -f ~/.claude/*.jsonl 2>/dev/null || true
# CLI caches
rm -rf ~/Library/Caches/claude-cli-nodejs/ 2>/dev/null || true
# CLAUDE.md scattered by agents
find ~ -maxdepth 4 -name "CLAUDE.md" -delete 2>/dev/null || true
find ~ -maxdepth 4 -name ".claudeignore" -delete 2>/dev/null || true
# CocoaPods cache (can interfere between apps)
rm -rf ~/.cocoapods/repos 2>/dev/null || true
echo "  done"

# ── 9. Clean temp files ──────────────────────────────────────────────────

echo "[9/9] Cleaning temp files..."
rm -f /tmp/pipeline_*.log /tmp/recreation_*.log /tmp/pipeline_pf_rules.conf 2>/dev/null || true
rm -f /tmp/.mcp_*.pid /tmp/.pipeline_frozen-suite_prompt_* 2>/dev/null || true
rm -rf /tmp/verifier_screenshots 2>/dev/null || true
echo "  done"

echo ""
echo "========================================"
echo "  Cleanup Complete — $APP_NAME"
echo "  Target ready for next app"
echo "  Finished: $(date)"
echo "========================================"
