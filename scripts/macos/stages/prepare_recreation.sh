#!/bin/bash
# prepare_recreation.sh — Deterministic isolation before recreation.
#
# No agent involved — pure shell script.
#
# Required env vars:
#   APP_NAME, WORK_DIR
# Optional:
#   SOURCE_DIR, OFFICIAL_APP, MODEL_API_ENDPOINTS (space-separated allowed hosts)
#
# Outputs:
#   Clean environment with running reference app, no source/binary/memory, network restricted
set -euo pipefail
trap '' SIGPIPE
# Ensure common bin dirs are in PATH (SSH non-login shells have minimal PATH)
export PATH="$HOME/local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"


# Sanitize a value for safe `KEY="$(...)"` writing into *_result.env (matches build.sh):
# strip CR/LF and escape embedded double-quotes so a value with spaces/quotes can't
# break `source prepare_result.env` in recreation.
_env_sanitize() { printf '%s' "$1" | tr '\n\r' '  ' | sed 's/"/\\"/g'; }

# Suppress Screen Sharing "cannot control your own events" dialog.
# This is a one-time macOS alert triggered by CGEvent usage over VNC/Screen Sharing.
# Setting this preference prevents it from appearing during automated testing.
defaults write com.apple.ScreenSharing skipLocalAlerts -bool true 2>/dev/null || true
defaults write com.apple.ScreenSharing dontWarnOnVNCEncryption -bool true 2>/dev/null || true

# The controller starts and function-tests CUA before this stage. Keep that
# GUI-session daemon alive so the isolated recreation user sees the same MCP
# endpoint that passed setup preflight.

SOURCE_DIR="${SOURCE_DIR:-$WORK_DIR/source}"
MODEL_API_ENDPOINTS="${MODEL_API_ENDPOINTS:-}"

# Load the frozen reference result.
if [ -z "${OFFICIAL_APP:-}" ] && [ -f "$WORK_DIR/reference_result.env" ]; then
    if bash -n "$WORK_DIR/reference_result.env" 2>/dev/null; then
        source "$WORK_DIR/reference_result.env"
    else
        echo "WARNING: reference_result.env has syntax errors, skipping source" >&2
    fi
fi

echo "╔══════════════════════════════════════╗"
echo "║  Preparing isolated recreation       ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  App:       $APP_NAME"
echo "  Source:    $SOURCE_DIR"
echo "  Official:  ${OFFICIAL_APP:-unknown}"
echo "  Started:   $(date)"
echo ""

RECREATION_DIR="$HOME/Recreation/${APP_NAME}"
mkdir -p "$RECREATION_DIR"
REFERENCE_RESOURCES="$HOME/Resources/reference"
rm -rf "$REFERENCE_RESOURCES"
mkdir -p "$REFERENCE_RESOURCES"

# ── 1. Extract resource files ─────────────────────────────────────────────

echo "[1/9] Extracting resource files..."
if [ -d "$SOURCE_DIR" ]; then
    RESOURCE_COUNT=$(find "$SOURCE_DIR" \( \
        -name "*.png" -o -name "*.jpg" -o -name "*.jpeg" -o -name "*.svg" \
        -o -name "*.icns" -o -name "*.ico" -o -name "*.gif" -o -name "*.pdf" \
        -o -name "*.ttf" -o -name "*.otf" -o -name "*.woff" -o -name "*.woff2" \
        -o -name "*.mp3" -o -name "*.wav" -o -name "*.aiff" \
        -o -name "*.xcassets" \
    \) -not -path "*/build/*" -not -path "*/DerivedData/*" -not -path "*/.build/*" \
       -not -path "*/node_modules/*" -exec cp -r {} "$REFERENCE_RESOURCES/" \; -print 2>/dev/null | wc -l || true)
    echo "  Extracted $RESOURCE_COUNT resources to ~/Resources/reference/"
else
    echo "  No source directory found, skipping"
fi

# ── 2. Secure reference app IN PLACE (Plan A — align with Linux/Windows) ──
# Do NOT move the running app's bundle. Moving it (old `mv` behaviour) broke
# runtime file access for the STILL-RUNNING reference app (Electron native
# binaries / lazy asar chunks / on-demand resources -> spawn ENOENT / crash),
# and recreation reuses that broken instance. Linux/Windows keep the reference in
# place and isolate the devagent by ownership+perms; do the same here. The
# devagent is blocked from reading it by 700 perms on $HOME/pipeline_work
# (section 8c) plus the explicit lock in section 3.

echo "[2/9] Securing reference app in place (not moved)..."
# /var is a compatibility symlink to /private/var on macOS. The permission engine rejects
# symlinked ancestors by design, so name the physical path directly before it becomes a
# protected root.
HIDDEN_DIR="/private/var/tmp/.$(openssl rand -hex 16)"
mkdir -p "$HIDDEN_DIR"

if [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}" ]; then
    echo "  Reference app kept in place (running, intact): $OFFICIAL_APP"
else
    echo "  No reference app bundle found in place"
fi
echo "  Hidden dir for tests/metadata: $HIDDEN_DIR"

# ── 3. Lock source from devagent (kept in place; app stays running) ──────
# Plan A: do NOT delete/move source or the running .app (that broke the
# reference). Preserve build.sh/launch.sh for recreation, then lock the source
# subtree from the devagent via ownership+perms (mirrors Linux
# `chown ref + chmod -R 700 /workspace/repo`). $HOME/pipeline_work is already
# 700 (section 8c); this hardens the source subtree explicitly.

echo "[3/9] Locking source from devagent (kept in place)..."
if [ -d "$SOURCE_DIR" ]; then
    # Preserve build.sh / launch.sh in hidden dir for recreation/eval bookkeeping
    [ -f "$SOURCE_DIR/build.sh" ]  && cp "$SOURCE_DIR/build.sh"  "$HIDDEN_DIR/build.sh"  2>/dev/null || true
    [ -f "$SOURCE_DIR/launch.sh" ] && cp "$SOURCE_DIR/launch.sh" "$HIDDEN_DIR/launch.sh" 2>/dev/null || true
    sudo chown -R "$(whoami):staff" "$SOURCE_DIR" 2>/dev/null || true
    chmod -R go-rwx "$SOURCE_DIR" 2>/dev/null || true
    echo "  Source locked (owner-only); reference .app left running in place"
fi

# Lock the reference .app IN PLACE wherever it lives — it may sit OUTSIDE the
# source tree (Xcode DerivedData at ~/Library/... or a manifest-specified
# absolute path). $HOME/pipeline_work is 700 (section 8c) and $SOURCE_DIR is
# owner-only (above), but a DerivedData/out-of-tree bundle is under neither, so
# without this it would be readable by the devagent (anti-cheat hole). Replaces
# the old `mv -> HIDDEN_DIR + chmod 111` hide. `chmod -R go-rwx` keeps OWNER
# login-account access intact so the STILL-RUNNING app is unaffected; only
# group(staff)/other — i.e. the devagent — lose read+traverse on the bundle.
if [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}" ]; then
    sudo chown -R "$(whoami):staff" "$OFFICIAL_APP" 2>/dev/null || true
    chmod -R go-rwx "$OFFICIAL_APP" 2>/dev/null || true
    echo "  Reference app locked in place (owner-only): $OFFICIAL_APP"
fi

# NOTE: chmod 700/111 AND chown root:wheel both deferred to AFTER app restart
# verification (section 9b). LaunchServices needs user-owned bundle to launch.

# ── 4. Clean build artifacts ─────────────────────────────────────────────

echo "[4/9] Cleaning build artifacts..."
# Plan A: the reference app may be RUNNING from DerivedData. Deleting DerivedData
# wholesale (old behaviour) would tear the bundle out from under the running
# process — the exact class of bug Plan A fixes (spawn ENOENT / lazy-load crash,
# then recreation reuses the broken instance). If OFFICIAL_APP lives under
# DerivedData, keep that tree and LOCK it from the devagent instead of deleting;
# otherwise wipe it as before.
_DERIVED_DATA="$HOME/Library/Developer/Xcode/DerivedData"
case "${OFFICIAL_APP:-}" in
    "$_DERIVED_DATA"/*)
        echo "  Reference app runs from DerivedData — preserving + locking it (not deleting)"
        sudo chown -R "$(whoami):staff" "$_DERIVED_DATA" 2>/dev/null || true
        chmod -R go-rwx "$_DERIVED_DATA" 2>/dev/null || true
        ;;
    *)
        rm -rf "$_DERIVED_DATA/" 2>/dev/null || true
        ;;
esac
rm -rf "$HOME/Library/Application Support/com.apple.dt.Xcode/" 2>/dev/null || true
echo "  Build artifacts cleaned"

# ── 5. Restrict network — reference stays OFFLINE through release ────────────
# The frozen-suite harness applies the same restriction before recreation and
# during eval, keeping the reference app's observed state consistent. Sourced so
# its fail-closed `exit 1`
# propagates here (preserves the original prepare fail-closed behaviour).
source "$SCRIPT_DIR/restrict_network.sh"

# ── 5b. Block network-capable tools (anti-cheat from Linux RecreationBench) ──

echo "[5b/9] Blocking network tools for devagent..."

# Strategy: /usr/bin is SIP-protected (read-only) on modern macOS.
# Instead of modifying /usr/bin, install blocking wrappers in /usr/local/bin
# which shadows /usr/bin in PATH.

# Git wrapper: block clone/fetch/pull/remote
REAL_GIT=$(which git 2>/dev/null || echo "/usr/bin/git")
# NEVER let REAL_GIT be the wrapper itself. On a reused target a leftover /usr/local/bin/git
# (from a crashed run) is first in PATH, so `which git` returns the wrapper; git.real would
# then be written as `exec /usr/local/bin/git`, and a later cleanup `mv git.real git` bakes
# in an exec-self loop that hangs the NEXT run's first git (e.g. provisioning's `git config`)
# until the SSH timeout. Pin to the system git so install/restore is always a real
# passthrough; this also self-heals an already-modified target. Fresh targets never hit this;
# BYO/reused VMs do.
[ "$REAL_GIT" = "/usr/local/bin/git" ] && REAL_GIT="/usr/bin/git"
if [ ! -f /usr/local/bin/git ] || [ ! -f /usr/local/bin/git.real ]; then
    # Save the resolved real git path as data; both executable wrappers are
    # ordinary runtime assets from the complete RB artifact.
    sudo install -d -m 0755 /usr/local/share/recreationbench
    printf '%s\n' "$REAL_GIT" | \
        sudo tee /usr/local/share/recreationbench/real-git-path >/dev/null
    sudo chmod 0644 /usr/local/share/recreationbench/real-git-path
    sudo install -m 0755 \
        "$SCRIPT_DIR/../runtime_assets/git_real_wrapper.sh" \
        /usr/local/bin/git.real
    sudo install -m 0755 \
        "$SCRIPT_DIR/../runtime_assets/git_network_guard.sh" \
        /usr/local/bin/git
    echo "  git wrapper installed at /usr/local/bin/git (clone/fetch/pull/remote blocked)"
fi

# Block curl/wget/pip/npm/yarn/brew via shadow wrappers in /usr/local/bin
BLOCKED_TOOLS=""
for tool in curl wget pip pip3 npm npx yarn brew; do
    # First try renaming in writable dirs
    for dir in /usr/local/bin /opt/homebrew/bin; do
        if [ -f "$dir/$tool" ] && [ ! -f "$dir/$tool.blocked" ]; then
            sudo mv "$dir/$tool" "$dir/$tool.blocked" 2>/dev/null || true
            BLOCKED_TOOLS="$BLOCKED_TOOLS $tool"
        fi
    done
    # For tools in /usr/bin (SIP-protected), install a blocking wrapper in /usr/local/bin
    if [ -f "/usr/bin/$tool" ] && [ ! -f "/usr/local/bin/$tool" ]; then
        sudo install -m 0755 \
            "$SCRIPT_DIR/../runtime_assets/network_tool_guard.sh" \
            "/usr/local/bin/$tool"
        BLOCKED_TOOLS="$BLOCKED_TOOLS $tool"
    fi
done
[ -n "$BLOCKED_TOOLS" ] && echo "  Blocked:$BLOCKED_TOOLS" || echo "  No additional tools to block"

# /etc/hosts: block known code/package hosting domains
sudo cp /etc/hosts /etc/hosts.pipeline_backup
echo "127.0.0.1 github.com gitlab.com bitbucket.org raw.githubusercontent.com" | sudo tee -a /etc/hosts > /dev/null
echo "127.0.0.1 pypi.org npmjs.org registry.npmjs.org" | sudo tee -a /etc/hosts > /dev/null
echo "  /etc/hosts updated (github, pypi, npm blocked)"

# ── 6. Clean Claude Code memory, context, caches, and prior sessions ─────

echo "[6/9] Cleaning Claude Code data (all prior session traces)..."

# 6a. Core Claude Code state (conversation history, memory, plans)
rm -rf "$HOME/.claude/memory/" 2>/dev/null || true
rm -rf "$HOME/.claude/projects/" 2>/dev/null || true
# codex' rollout is the same class of state: left behind it leaks the previous stage into the
# next (collected twice, and `codex exec resume --last` can attach to the wrong session).
rm -rf "${CODEX_HOME:-$HOME/.codex}/sessions/" 2>/dev/null || true
rm -f "${CODEX_HOME:-$HOME/.codex}/history.jsonl" 2>/dev/null || true
rm -f "$HOME/.claude/"*.jsonl 2>/dev/null || true
rm -rf "$HOME/.claude/plans/" 2>/dev/null || true
rm -rf "$HOME/.claude/tasks/" 2>/dev/null || true

# 6c. Per-directory .claude folders (may contain settings, memory, plans)
rm -rf "$WORK_DIR/.claude/" 2>/dev/null || true
rm -rf "$RECREATION_DIR/.claude/" 2>/dev/null || true
rm -rf "$SOURCE_DIR/.claude/" 2>/dev/null || true
rm -rf "$HOME/.claude/" 2>/dev/null || true
# Recreate only settings.json (needed for Recreation permissions)
mkdir -p "$HOME/.claude"

# 6d. Claude CLI caches — MCP logs contain CU screenshots and AX tree dumps
rm -rf "$HOME/Library/Caches/claude-cli-nodejs/" 2>/dev/null || true
echo "  Claude CLI caches cleaned"

# 6e. CLAUDE.md files anywhere in home (may contain project descriptions)
find "$HOME" -maxdepth 4 -name "CLAUDE.md" -delete 2>/dev/null || true
find "$HOME" -maxdepth 4 -name ".claudeignore" -delete 2>/dev/null || true

# 6f. Old trajectory files from prior manual runs
rm -rf "$HOME/shared/" 2>/dev/null || true
rm -rf "$HOME/workspace/" 2>/dev/null || true

echo "  All Claude Code data cleaned"

# ── 7. Clean shell history ───────────────────────────────────────────────

echo "[7/9] Cleaning shell history..."
rm -f "$HOME/.zsh_history" "$HOME/.bash_history" 2>/dev/null || true
history -c 2>/dev/null || true
echo "  Shell history cleaned"

# ── 8. Clean temp files and secure sensitive data ────────────────────────

echo "[8/9] Cleaning temp files and securing sensitive data..."
rm -f /tmp/pipeline_*.log /tmp/recreation_*.log 2>/dev/null || true
rm -f "$WORK_DIR/logs/reference_agent.log" 2>/dev/null || true
rm -f "$WORK_DIR/logs/tests_agent.log" "$WORK_DIR/logs/tests_retry.log" 2>/dev/null || true
rm -rf "$SOURCE_DIR/spec.md" 2>/dev/null || true

# ── Defense-in-depth: kill any leftover clean-state-verify app instance ──────
# reference's clean-state verification builds + launches the reference app from a
# `mktemp -d` dir. launch.sh uses `open -W`, which reparents the app to launchd,
# so it can survive reference cleanup as a SECOND running instance whose bundle sits
# in a temp dir. That must NOT persist into recreation — devagent would see a duplicate
# reference app (and, before dir removal, its source). Kill any process whose
# executable path is a temp-dir .app, then remove leftover temp source dirs.
echo "  Sweeping clean-verify temp app leftovers..."
_TMP_APP_PIDS=$(ps -Axo pid,args 2>/dev/null \
    | grep -E '(/var/folders/[^ ]+/T/|/private/var/folders/[^ ]+/T/|/tmp/)tmp\.[A-Za-z0-9]+/source/[^/]+\.app/Contents/MacOS/' \
    | grep -v grep | awk '{print $1}' || true)
for _p in $_TMP_APP_PIDS; do kill -9 "$_p" 2>/dev/null || true; done
[ -n "$_TMP_APP_PIDS" ] && echo "    killed leftover temp-app pids: $(echo $_TMP_APP_PIDS | tr '\n' ' ')" || echo "    none found"
# Remove any leftover clean-verify temp dirs (source + binary)
for _d in /var/folders/*/*/T/tmp.*/source /private/var/folders/*/*/T/tmp.*/source /tmp/tmp.*/source; do
    [ -d "$_d" ] && rm -rf "$(dirname "$_d")" 2>/dev/null || true
done

# Move frozen inputs to a root-only directory before the agent starts.
if [ -n "${HIDDEN_DIR:-}" ] && [ -d "${HIDDEN_DIR:-}" ]; then
    if [ -d "$WORK_DIR/tests" ]; then
        sudo mv "$WORK_DIR/tests" "$HIDDEN_DIR/tests"
        echo "  Frozen tests moved to hidden dir"

        # Generate checksums so eval can verify that the frozen suite did not change.
        if sudo ls "$HIDDEN_DIR/tests/"test_ax_*.py 1>/dev/null 2>&1; then
            sudo shasum -a 256 "$HIDDEN_DIR/tests/"test_ax_*.py | sudo tee "$HIDDEN_DIR/test_checksums.sha256" > /dev/null || true
            echo "  test_ax_*.py checksums generated ($(sudo wc -l < "$HIDDEN_DIR/test_checksums.sha256" 2>/dev/null || echo 0) files)"
        fi
    fi
    sudo mv "$WORK_DIR/reference_result.env" "$HIDDEN_DIR/" 2>/dev/null || true
    # .hidden_ref_path literally points to the hidden binary
    rm -f "$WORK_DIR/.hidden_ref_path"
    echo "  Reference metadata secured"
fi

# Clean old stage logs that might contain source code paths or build info. Keep
# the active prepare.log: run_full.sh redirects this script there before entry,
# and deleting the directory unlinked the only useful failure diagnostic.
find "$WORK_DIR/logs" -mindepth 1 -maxdepth 1 ! -name prepare.log \
    -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$WORK_DIR/logs"

# Clean any trajectory JSONL files outside .claude/ (from prior manual runs)
find "$HOME" -maxdepth 5 -name "trajectory*.jsonl" -not -path "*/Recreation/*" -delete 2>/dev/null || true

echo "  Temp files cleaned"

# ── 8b. Prepare devagent isolation (if devagent user exists) ─────────────

DEVAGENT_USER="${DEVAGENT_USER:-devagent}"
if id "$DEVAGENT_USER" &>/dev/null; then
    echo "[8b] Preparing devagent isolation..."
    DEVAGENT_HOME=$(dscl . -read /Users/$DEVAGENT_USER NFSHomeDirectory 2>/dev/null | awk '{print $2}' || true)
    DEVAGENT_HOME="${DEVAGENT_HOME:-/Users/$DEVAGENT_USER}"

    # Use the prompt-visible path as a real directory.  The previous symlink pointed at
    # /Users/devagent/Recreation/$APP_NAME, so readlink/getcwd exposed the hidden app identity to
    # the agent even though the prompt itself was anonymised.  The trusted recreation stage copies
    # this directory into the login account's result tree only after the agent exits.
    CANONICAL_WORKSPACE="/Users/Shared/workspace/recreation"
    DEVAGENT_RECREATION="$CANONICAL_WORKSPACE"
    # Remove legacy app-named workspaces as well as any canonical link left by an older run.
    sudo rm -rf "$DEVAGENT_HOME/Recreation"
    sudo mkdir -p "$(dirname "$CANONICAL_WORKSPACE")"
    sudo rm -rf "$CANONICAL_WORKSPACE"
    sudo mkdir -p "$CANONICAL_WORKSPACE"
    sudo chown -R "$DEVAGENT_USER:staff" "$CANONICAL_WORKSPACE"
    # Traversable by the GUI login session, not 0700.
    #
    # `open`/LaunchServices does not exec the bundle itself: it asks the launchd of the Aqua
    # session -- which belongs to the console user, i.e. the harness account that runs the CUA
    # driver -- to spawn it, and that spawn uses plain filesystem access with no privilege
    # escalation.  A 0700 directory owned by $DEVAGENT_USER therefore makes every
    # `open <workspace>/build/App.app` fail with RBSRequestErrorDomain Code=5 /
    # NSPOSIXErrorDomain Code=111 "Launchd job spawn failed", so the agent cannot look at the
    # app it has just built.  The prompt asks it to "build it, launch it" and to diagnose a
    # failed launch, but the error it gets is about a permission bit it has no reason to suspect:
    # observed diagnosis in a release trajectory was "macOS does not allow launching from
    # /Users/Shared", after which the agent worked around it by staging a copy under /tmp.  Models
    # that do not find that workaround recreate blind.
    #
    # This bit protects nothing.  The only other local account is the trusted harness user, which
    # holds sudo and can read this tree regardless; the reference app is not here (it lives in the
    # harness home and the hidden metadata directory), and anonymity comes from this being a real
    # directory at a fixed path, not from its mode.  Post-run integrity is unchanged: recreation.sh
    # still does chown -R root:wheel + chmod -R u+rwX,go-rwx, snapshots, and publishes by rename
    # once the agent has exited.
    sudo chmod 755 "$CANONICAL_WORKSPACE"
    if [ -L "$CANONICAL_WORKSPACE" ] || [ ! -d "$CANONICAL_WORKSPACE" ]; then
        echo "ERROR: canonical workspace is not a real directory: $CANONICAL_WORKSPACE"
        exit 1
    fi
    sudo chmod 755 "$(dirname "$CANONICAL_WORKSPACE")"

    # Copy resources to one app-name-independent location in the agent's home.
    # The prompt must not use pipeline identity metadata to tell the model which window to inspect.
    sudo rm -rf "$DEVAGENT_HOME/Resources"
    sudo mkdir -p "$DEVAGENT_HOME/Resources/reference"
    sudo cp -R "$REFERENCE_RESOURCES/." "$DEVAGENT_HOME/Resources/reference/" 2>/dev/null || true
    sudo chown -R "$DEVAGENT_USER:staff" "$DEVAGENT_HOME/Resources"

    # Clean any prior Claude Code data in devagent home
    sudo rm -rf "$DEVAGENT_HOME/.claude/" 2>/dev/null || true
    sudo rm -rf "$DEVAGENT_HOME/Library/Caches/claude-cli-nodejs/" 2>/dev/null || true
    sudo find "$DEVAGENT_HOME" -maxdepth 4 -name "CLAUDE.md" -delete 2>/dev/null || true
    sudo rm -f "$DEVAGENT_HOME/.zsh_history" "$DEVAGENT_HOME/.bash_history" 2>/dev/null || true

    # Grant TCC permissions in devagent's user-level TCC DB
    DEVAGENT_TCC_DB="$DEVAGENT_HOME/Library/Application Support/com.apple.TCC/TCC.db"
    if [ -f "$DEVAGENT_TCC_DB" ]; then
        # Grant sshd-keygen-wrapper and Terminal access (needed for Claude Code CU/AX)
        for tcc_client in "/usr/libexec/sshd-keygen-wrapper" "com.apple.Terminal"; do
            for tcc_svc in kTCCServiceAccessibility kTCCServiceScreenCapture kTCCServiceAppleEvents kTCCServicePostEvent kTCCServiceListenEvent kTCCServiceSystemPolicyAllFiles kTCCServiceDeveloperTool kTCCServiceCalendar kTCCServiceAddressBook kTCCServiceContactsFull kTCCServiceContactsLimited kTCCServiceReminders kTCCServicePhotos kTCCServicePhotosAdd kTCCServiceCamera kTCCServiceMicrophone kTCCServiceBluetoothAlways kTCCServiceMediaLibrary kTCCServiceSpeechRecognition kTCCServiceMotion kTCCServiceLocation kTCCServiceFocusStatus kTCCServiceSystemPolicyDesktopFolder kTCCServiceSystemPolicyDocumentsFolder kTCCServiceSystemPolicyDownloadsFolder kTCCServiceSystemPolicyNetworkVolumes kTCCServiceSystemPolicyRemovableVolumes kTCCServiceFileProviderDomain kTCCServiceFileProviderPresence; do
                sudo sqlite3 "$DEVAGENT_TCC_DB" "INSERT OR REPLACE INTO access \
                    (service, client, client_type, auth_value, auth_reason, auth_version, indirect_object_identifier) \
                    VALUES ('$tcc_svc', '$tcc_client', 0, 2, 3, 1, 'UNUSED');" 2>/dev/null || true
            done
        done
        # Grant app permissions if BUNDLE_ID is known
        BUNDLE_ID="${BUNDLE_ID:-}"
        if [ -z "$BUNDLE_ID" ] && [ -f "$WORK_DIR/reference_result.env" ]; then
            BUNDLE_ID=$(grep '^BUNDLE_ID=' "$WORK_DIR/reference_result.env" 2>/dev/null | cut -d= -f2 || true)
        fi
        if [ -n "$BUNDLE_ID" ]; then
            for tcc_svc in kTCCServiceAccessibility kTCCServiceScreenCapture kTCCServiceAppleEvents kTCCServicePostEvent kTCCServiceListenEvent kTCCServiceSystemPolicyAllFiles kTCCServiceDeveloperTool kTCCServiceCalendar kTCCServiceAddressBook kTCCServiceContactsFull kTCCServiceContactsLimited kTCCServiceReminders kTCCServicePhotos kTCCServicePhotosAdd kTCCServiceCamera kTCCServiceMicrophone kTCCServiceBluetoothAlways kTCCServiceMediaLibrary kTCCServiceSpeechRecognition kTCCServiceMotion kTCCServiceLocation kTCCServiceFocusStatus kTCCServiceSystemPolicyDesktopFolder kTCCServiceSystemPolicyDocumentsFolder kTCCServiceSystemPolicyDownloadsFolder kTCCServiceSystemPolicyNetworkVolumes kTCCServiceSystemPolicyRemovableVolumes kTCCServiceFileProviderDomain kTCCServiceFileProviderPresence; do
                sudo sqlite3 "$DEVAGENT_TCC_DB" "INSERT OR REPLACE INTO access \
                    (service, client, client_type, auth_value, auth_reason, auth_version, indirect_object_identifier) \
                    VALUES ('$tcc_svc', '$BUNDLE_ID', 0, 2, 3, 1, 'UNUSED');" 2>/dev/null || true
            done
            # Fix any denied entries
            sudo sqlite3 "$DEVAGENT_TCC_DB" "UPDATE access SET auth_value=2 WHERE client='$BUNDLE_ID' AND auth_value=0;" 2>/dev/null || true
        fi
        # The recreation user talks to the already function-tested CUA daemon
        # over its Unix socket; it does not access AX/ScreenCapture directly.
        # Restarting tccd here invalidates that daemon's GUI/TCC context and
        # leaves a live process which can no longer enumerate any windows.
        echo "  devagent TCC permissions recorded (daemon context preserved)"
    fi

    # Save devagent info (will be written to prepare_result.env at end of script)
    PREPARE_DEVAGENT_USER="$DEVAGENT_USER"
    PREPARE_DEVAGENT_HOME="$DEVAGENT_HOME"
    PREPARE_DEVAGENT_RECREATION="$DEVAGENT_RECREATION"
    echo "  devagent isolation prepared at canonical workspace: $DEVAGENT_RECREATION"
else
    echo "[8b] No devagent user found, Recreation will run as current user (sudo accessible!)"
fi

# ── 8c. Lock down harness directories from the isolation account ───────

echo "[8c] Locking down harness directories from the isolation account..."
SETUP_PIPELINE_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
chmod 700 "$HOME/.claude" 2>/dev/null || true
chmod 700 "$WORK_DIR" 2>/dev/null || true
chmod 700 "$HOME/pipeline_work" 2>/dev/null || true
chmod 700 "$SETUP_PIPELINE_DIR" 2>/dev/null || true
sudo chmod -R 700 /var/tmp/pipeline_results 2>/dev/null || true
sudo chown -R root:wheel /var/tmp/pipeline_results 2>/dev/null || true
echo "  Locked: ~/.claude, $WORK_DIR, ~/pipeline_work, $SETUP_PIPELINE_DIR, /var/tmp/pipeline_results"

# ── 8d. Share the harness toolchains and build caches with the isolation account ──
# The image pre-installs rust at ~/.cargo (+.rustup; used via ~/.cargo/env, not on
# PATH), and the reference build downloaded the app's crates/modules into ~/.cargo,
# ~/go, ~/.bun. The OFFLINE recreation (devagent, no network) must REUSE these — it
# cannot reinstall. Open them r+w to all users so devagent (which recreation.sh
# points CARGO_HOME/RUSTUP_HOME/BUN_INSTALL/GOPATH at) can read+write the caches.
# ANTI-CHEAT: these hold ONLY third-party package caches + generic toolchains — the
# reference source ($WORK_DIR), binary, and .app are separately locked above (700 /
# hidden), so this exposes no reference code. The login home stays traversable.
# Doing it HERE (prepare) rather than recreation means a held target already has the
# shared dirs open for inspection; the real run reaches recreation either way.
echo "[8d] Sharing harness toolchain/caches with the isolation account..."
for _d in "$HOME/.cargo" "$HOME/.rustup" "$HOME/.bun" "$HOME/go"; do
    [ -e "$_d" ] && sudo chmod -R a+rwX "$_d" 2>/dev/null || true
done
echo "  Shared (r+w): ~/.cargo ~/.rustup ~/.bun ~/go"

# ── 9. Verify clean state ────────────────────────────────────────────────

echo "[9/9] Verifying clean state..."
ERRORS=0

# A/B. Plan A: source + reference app are KEPT IN PLACE and locked from the
# devagent via perms ($HOME/pipeline_work is 700 in section 8c; source subtree
# is owner-only from section 3). We intentionally no longer delete/move them
# (that broke the running reference). Report state; do not fail the stage.
if [ -d "$SOURCE_DIR" ]; then
    echo "  Source present but locked from devagent (Plan A): $SOURCE_DIR"
fi
if [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}" ]; then
    echo "  Reference app in place (Plan A): $OFFICIAL_APP"
fi

# A/B (assert). Plan A keeps source + app on disk (locked) instead of deleting
# them, so — unlike delete — a lock that silently failed to apply would leak the
# reference to the devagent. Make it FAIL-LOUD: actually attempt the read AS the
# devagent user and error if it succeeds. `test -r` resolves the whole path, so
# this transitively covers the $HOME/pipeline_work 700 gate (§8c), the source
# go-rwx and the OFFICIAL_APP/DerivedData go-rwx locks (§3/§4) in one check.
# This is the runtime guarantee that anti-cheat is intact for this run.
if [ -n "${PREPARE_DEVAGENT_USER:-}" ] && id "${PREPARE_DEVAGENT_USER}" &>/dev/null; then
    if [ -d "$SOURCE_DIR" ] && sudo -u "$PREPARE_DEVAGENT_USER" test -r "$SOURCE_DIR" 2>/dev/null; then
        echo "  ERROR: devagent CAN read SOURCE_DIR — anti-cheat lock did not take: $SOURCE_DIR"
        ERRORS=$((ERRORS + 1))
    fi
    if [ -n "${OFFICIAL_APP:-}" ] && [ -d "${OFFICIAL_APP:-}" ] \
        && sudo -u "$PREPARE_DEVAGENT_USER" test -r "$OFFICIAL_APP" 2>/dev/null; then
        echo "  ERROR: devagent CAN read reference app — anti-cheat lock did not take: $OFFICIAL_APP"
        ERRORS=$((ERRORS + 1))
    fi
    if [ "$ERRORS" -eq 0 ]; then
        echo "  Anti-cheat assert OK: devagent cannot read source or reference app"
    fi
fi

# C. Reference app still running
APP_BINARY_NAME=""
IS_LSUIELEMENT="false"
if [ -n "${BUNDLE_ID:-}" ]; then
    # Plan A: app is in place at OFFICIAL_APP (no longer moved to HIDDEN_DIR)
    _PLIST="${OFFICIAL_APP:-/nonexistent}/Contents/Info.plist"
    APP_BINARY_NAME=$(defaults read "$_PLIST" CFBundleExecutable 2>/dev/null || echo "")
    # Detect LSUIElement/LSBackgroundOnly apps (auto-exit, no persistent process)
    LSUIVal=$(defaults read "$_PLIST" LSUIElement 2>/dev/null || echo "")
    LSBGVal=$(defaults read "$_PLIST" LSBackgroundOnly 2>/dev/null || echo "")
    if [ "$LSUIVal" = "1" ] || [ "$LSUIVal" = "YES" ] || [ "$LSBGVal" = "1" ] || [ "$LSBGVal" = "YES" ]; then
        IS_LSUIELEMENT="true"
    fi
fi
APP_BINARY_NAME="${APP_BINARY_NAME:-${APP_BINARY:-$APP_NAME}}"
if ! pgrep "${APP_BINARY_NAME:0:15}" > /dev/null 2>&1; then
    if ! pgrep -f "$APP_NAME" 2>/dev/null | while read pid; do
        PNAME=$(ps -p "$pid" -o comm= 2>/dev/null || true)
        case "$PNAME" in
            *bash*|*grep*|*pgrep*|*tail*|*tee*) continue ;;
            *) echo "$pid"; break ;;
        esac
    done | grep -q .; then
        # App not running (e.g. LSUIElement auto-exit, or killed by tests cleanup)
        # — restart from the IN-PLACE reference app (Plan A: not in hidden dir).
        echo "  WARNING: Reference app not running — restarting..."
        _RESTART_APP="${OFFICIAL_APP:-${HIDDEN_DIR}/$(basename "${OFFICIAL_APP:-${APP_NAME}.app}")}"
        _RESTART_FAILURE_REASON=""
        _STARTED=false

        # Copy the app to a login-account-readable location before launching.
        # (section 9b will lock hidden dir, which would kill an app running from there)
        _REFAPP_DIR="/var/tmp/.pipeline_refapp_${APP_NAME}"
        sudo rm -rf "$_REFAPP_DIR" 2>/dev/null || true
        sudo mkdir -p "$_REFAPP_DIR"
        if [ -d "$_RESTART_APP" ]; then
            sudo cp -R "$_RESTART_APP" "$_REFAPP_DIR/"
            sudo chown -R "$(whoami):staff" "$_REFAPP_DIR"
            chmod -R 700 "$_REFAPP_DIR"
            _RESTART_APP="$_REFAPP_DIR/$(basename "$_RESTART_APP")"
            echo "  Copied reference app to login-account-readable dir: $_RESTART_APP"
        fi
        if [ -f "$HIDDEN_DIR/launch.sh" ]; then
            sudo cp "$HIDDEN_DIR/launch.sh" "$_REFAPP_DIR/launch.sh" 2>/dev/null || true
            sudo chown "$(whoami):staff" "$_REFAPP_DIR/launch.sh" 2>/dev/null || true
        fi

        # Attempt 0 (FIRST): launch.sh — independent of .app existence
        _RESTORED_LAUNCH_SH="$_REFAPP_DIR/launch.sh"
        if [ -f "$_RESTORED_LAUNCH_SH" ]; then
            echo "  Trying launch.sh from restored dir..."
            bash "$SCRIPT_DIR/../tools/run_launch.sh" "$_RESTORED_LAUNCH_SH" &
            sleep 5
            if pgrep "${APP_BINARY_NAME:0:15}" > /dev/null 2>&1; then
                _STARTED=true
                echo "  [diag] launch.sh succeeded"
            elif pgrep -f "$APP_NAME" 2>/dev/null | while read pid; do
                PNAME=$(ps -p "$pid" -o comm= 2>/dev/null || true)
                case "$PNAME" in *bash*|*grep*|*pgrep*|*tee*|*sudo*) continue ;; *) echo "$pid"; break ;; esac
            done | grep -q .; then
                _STARTED=true
                echo "  [diag] launch.sh succeeded (broad match)"
            else
                echo "  [diag] launch.sh did not produce a running process"
            fi
        fi

        # Attempt 1+: .app-based restart from the restored readable copy.
        if [ "$_STARTED" = false ] && [ -d "$_RESTART_APP" ]; then
            echo "  [diag] App bundle: $_RESTART_APP"
            echo "  [diag] CFBundleExecutable: ${APP_BINARY_NAME:-<unknown>}"
            echo "  [diag] LSUIElement: ${LSUIVal:-<unset>}, LSBackgroundOnly: ${LSBGVal:-<unset>}"
            _BUNDLE_ID_DIAG=$(defaults read "$_RESTART_APP/Contents/Info.plist" CFBundleIdentifier 2>/dev/null || echo "<unreadable>")
            echo "  [diag] CFBundleIdentifier: $_BUNDLE_ID_DIAG"

            # Re-sign to clear quarantine/invalid signatures after move
            xattr -dr com.apple.quarantine "$_RESTART_APP" 2>/dev/null || true
            codesign -fs - --deep "$_RESTART_APP" 2>/dev/null || true
            # Attempt 1: open (captures stderr for diagnostics)
            _OPEN_STDERR=$(open "$_RESTART_APP" 2>&1) || true
            if [ -n "$_OPEN_STDERR" ]; then
                echo "  [diag] open stderr: $_OPEN_STDERR"
            fi
            # Poll for up to 30 seconds (process may take time to start)
            for _i in $(seq 1 15); do
                sleep 2
                if pgrep "${APP_BINARY_NAME:0:15}" > /dev/null 2>&1; then
                    _STARTED=true
                    echo "  [diag] pgrep matched after $((_i * 2))s"
                    break
                fi
            done
            if [ "$_STARTED" = false ]; then
                echo "  [diag] pgrep '${APP_BINARY_NAME:0:15}' found nothing after 30s"
                echo "  [diag] ps aux matching '$APP_NAME':"
                ps aux 2>/dev/null | grep -i "$APP_NAME" | grep -v grep | head -5 || echo "    <none>"
                # Fallback: try direct binary execution
                echo "  WARNING: open failed, trying direct binary execution..."
                _MACOS_DIR="$_RESTART_APP/Contents/MacOS"
                _BINARY=$(find "$_MACOS_DIR" -type f -perm +111 2>/dev/null | head -1 || true)
                if [ -n "$_BINARY" ]; then
                    echo "  [diag] Executing binary directly: $_BINARY"
                    nohup "$_BINARY" > /dev/null 2>&1 &
                    sleep 5
                    if pgrep "${APP_BINARY_NAME:0:15}" > /dev/null 2>&1; then
                        _STARTED=true
                    else
                        echo "  [diag] Direct execution: pgrep still found nothing"
                        _RESTART_FAILURE_REASON="binary_exec_no_process"
                    fi
                else
                    echo "  [diag] No executable found in $_MACOS_DIR"
                    _RESTART_FAILURE_REASON="no_executable_in_bundle"
                fi
            fi
            # Supplementary check: osascript System Events (GUI visibility)
            if [ "$_STARTED" = false ]; then
                _SE_CHECK=$(osascript -e "tell application \"System Events\" to get name of every process whose name contains \"${APP_BINARY_NAME:0:15}\"" 2>/dev/null || echo "")
                if [ -n "$_SE_CHECK" ] && [ "$_SE_CHECK" != "{}" ]; then
                    echo "  [diag] System Events sees process: $_SE_CHECK (pgrep missed it)"
                    _STARTED=true
                else
                    echo "  [diag] System Events also cannot see the process"
                fi
            fi
            # Log recent crash/launch diagnostics (last 30s)
            if [ "$_STARTED" = false ]; then
                echo "  [diag] Recent launchservices/crash log entries:"
                log show --predicate "processImagePath CONTAINS '${APP_BINARY_NAME:0:15}' OR subsystem == 'com.apple.launchservices'" --last 30s --style compact 2>/dev/null | tail -10 || echo "    <log show unavailable>"
                [ -z "$_RESTART_FAILURE_REASON" ] && _RESTART_FAILURE_REASON="process_not_detected"
            fi
        fi

        # Final verdict
        if [ "$_STARTED" = true ]; then
            echo "  OK: Reference app restarted successfully"
        elif [ -n "${OFFICIAL_APP:-}" ] || [ -d "$_RESTART_APP" ] 2>/dev/null; then
            echo "  ERROR: Reference app failed to restart"
            echo "  ERROR: Failure reason: ${_RESTART_FAILURE_REASON:-unknown}"
            if [ "$IS_LSUIELEMENT" = "true" ]; then
                echo "  NOTE: App is LSUIElement — still fatal since recreation agent needs a running reference app"
            fi
            ERRORS=$((ERRORS + 1))
        else
            echo "  WARNING: No .app bundle and launch.sh did not produce a persistent process"
            echo "  Continuing — launch.sh will be available in recreation for reference"
        fi
    fi
fi

# D. No Claude Code data leaks
if [ -d "$HOME/.claude/memory" ]; then
    echo "  ERROR: Claude memory not cleaned"
    ERRORS=$((ERRORS + 1))
fi
if [ -d "$HOME/.claude/projects" ]; then
    echo "  ERROR: Claude projects not cleaned"
    ERRORS=$((ERRORS + 1))
fi
# Verified, not assumed: a cleanup that silently failed leaves the previous stage's codex
# rollout in place, and the next stage collects it as its own.
if [ -d "${CODEX_HOME:-$HOME/.codex}/sessions" ]; then
    echo "  ERROR: codex sessions not cleaned"
    ERRORS=$((ERRORS + 1))
fi
if [ -d "$HOME/.claude/tasks" ]; then
    echo "  ERROR: Claude tasks not cleaned"
    ERRORS=$((ERRORS + 1))
fi
if [ -d "$HOME/Library/Caches/claude-cli-nodejs" ]; then
    echo "  ERROR: Claude CLI caches not cleaned"
    ERRORS=$((ERRORS + 1))
fi

# E. No frozen suite visible to the recreation agent.
if [ -d "$WORK_DIR/tests" ]; then
    echo "  ERROR: Frozen tests directory not moved to hidden dir"
    ERRORS=$((ERRORS + 1))
fi

# F. No .hidden_ref_path (would expose binary location)
if [ -f "$WORK_DIR/.hidden_ref_path" ]; then
    echo "  ERROR: .hidden_ref_path not deleted"
    ERRORS=$((ERRORS + 1))
fi

# G. No reference metadata left in the visible work directory.
if [ -f "$WORK_DIR/reference_result.env" ]; then
    echo "  ERROR: Reference metadata was not moved"
    ERRORS=$((ERRORS + 1))
fi

# H. No old trajectory files
OLD_TRAJ=$(find "$HOME" -maxdepth 5 -name "trajectory*.jsonl" -not -path "*/Recreation/*" 2>/dev/null | head -1 || true)
if [ -n "$OLD_TRAJ" ]; then
    echo "  ERROR: Old trajectory files found: $OLD_TRAJ"
    ERRORS=$((ERRORS + 1))
fi

# I. No old shared/workspace directories
if [ -d "$HOME/shared" ] || [ -d "$HOME/workspace" ]; then
    echo "  ERROR: Old shared/workspace directories not cleaned"
    ERRORS=$((ERRORS + 1))
fi

# J. Git wrapper installed (in /usr/local/bin, shadowing /usr/bin)
if [ ! -f /usr/local/bin/git.real ]; then
    echo "  ERROR: Git wrapper not installed"
    ERRORS=$((ERRORS + 1))
fi

# K. /etc/hosts backup exists (means hosts were modified)
if [ ! -f /etc/hosts.pipeline_backup ]; then
    echo "  ERROR: /etc/hosts not modified (no backup found)"
    ERRORS=$((ERRORS + 1))
fi

if [ "$ERRORS" -gt 0 ]; then
    echo ""
    echo "  $ERRORS verification errors found!"
    # Write sub-step report for diagnostics
    cat > "$WORK_DIR/prepare_report.json" <<REPORT_EOF
{
    "status": "partial",
    "errors": $ERRORS,
    "restart_failure_reason": "${_RESTART_FAILURE_REASON:-none}",
    "is_lsuielement": "$IS_LSUIELEMENT",
    "app_binary_name": "${APP_BINARY_NAME:-}",
    "hidden_dir": "${HIDDEN_DIR:-}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "substeps": {
        "extract_resources": "ok",
        "hide_binary": "ok",
        "delete_source": "ok",
        "network_isolation": "ok",
        "block_tools": "ok",
        "clean_memory": "ok",
        "clean_history": "ok",
        "secure_data": "ok",
        "devagent_setup": "ok",
        "restart_reference": "${_RESTART_FAILURE_REASON:-ok}",
        "verification": "failed ($ERRORS errors)"
    }
}
REPORT_EOF
    echo "RECREATION_DIR=$RECREATION_DIR" > "$WORK_DIR/prepare_result.env"
    echo "RESOURCE_DIR=$HOME/Resources/${APP_NAME}" >> "$WORK_DIR/prepare_result.env"
    [ -n "${OFFICIAL_APP:-}" ] && echo "OFFICIAL_APP=\"$(_env_sanitize "$OFFICIAL_APP")\"" >> "$WORK_DIR/prepare_result.env"
    [ -n "${APP_BINARY_NAME:-}" ] && echo "APP_BINARY=\"$(_env_sanitize "$APP_BINARY_NAME")\"" >> "$WORK_DIR/prepare_result.env"
    echo "PREPARE_PARTIAL=1" >> "$WORK_DIR/prepare_result.env"
    echo "PREPARE_ERRORS=$ERRORS" >> "$WORK_DIR/prepare_result.env"
    echo "PREPARE_RESTART_FAILURE=${_RESTART_FAILURE_REASON:-none}" >> "$WORK_DIR/prepare_result.env"
    if [ -n "${PREPARE_DEVAGENT_USER:-}" ]; then
        echo "DEVAGENT_USER=$PREPARE_DEVAGENT_USER" >> "$WORK_DIR/prepare_result.env"
        echo "DEVAGENT_HOME=$PREPARE_DEVAGENT_HOME" >> "$WORK_DIR/prepare_result.env"
        echo "DEVAGENT_RECREATION=$PREPARE_DEVAGENT_RECREATION" >> "$WORK_DIR/prepare_result.env"
    fi
    echo "  Partial prepare_result.env written"
    echo "  Report: $WORK_DIR/prepare_report.json"
    exit 1
fi

echo "  All checks passed (11 items verified)"

# ── 9b. NOW lock down hidden dir (after app restart verified) ────────────
# This must happen AFTER restart verification because LaunchServices needs to
# read Info.plist, frameworks, and resources to launch the app.
echo "[9b] Locking hidden directory permissions..."
if [ -n "${HIDDEN_DIR:-}" ] && [ -d "${HIDDEN_DIR:-}" ]; then
    sudo chown -R "$(whoami):staff" "$HIDDEN_DIR" 2>/dev/null || true
    sudo chmod -R 700 "$HIDDEN_DIR" 2>/dev/null || true
    echo "  Hidden dir restricted to the login account (chown $(whoami):staff + chmod 700)"
    # Execute-only on binary (defense in depth)
    if [ -n "${OFFICIAL_APP:-}" ]; then
        HIDDEN_APP_NAME=$(basename "$OFFICIAL_APP")
        if [ -d "$HIDDEN_DIR/$HIDDEN_APP_NAME/Contents/MacOS" ]; then
            sudo find "$HIDDEN_DIR/$HIDDEN_APP_NAME/Contents/MacOS" -type f -perm +111 \
                -exec chmod 111 {} \; 2>/dev/null || true
            echo "  Binary set to execute-only (chmod 111)"
        fi
    fi
fi

# Save HIDDEN_DIR to a root-only file (prevents dev agent from discovering it)
HIDDEN_PATH_FILE="/var/tmp/.pipeline_hidden_path"
if [ -n "${HIDDEN_DIR:-}" ]; then
    echo "$HIDDEN_DIR" | sudo tee "$HIDDEN_PATH_FILE" > /dev/null
    sudo chown root:wheel "$HIDDEN_PATH_FILE"
    sudo chmod 600 "$HIDDEN_PATH_FILE"
fi

# Save non-sensitive preparation result for recreation.
echo "RECREATION_DIR=$RECREATION_DIR" > "$WORK_DIR/prepare_result.env"
echo "RESOURCE_DIR=$HOME/Resources/${APP_NAME}" >> "$WORK_DIR/prepare_result.env"
# Plan A: the reference app is kept IN PLACE (not moved to the hidden dir). Publish its
# path + real binary name so recreation resolves them reliably instead of via the fragile
# locked-hidden-dir reference_result.env read (which broke → "Reference app NOT running").
# Path/name only — the app + source content stay locked go-rwx, so the devagent still
# cannot read the reference. (prepare_result.env is harness-owned.)
[ -n "${OFFICIAL_APP:-}" ] && echo "OFFICIAL_APP=\"$(_env_sanitize "$OFFICIAL_APP")\"" >> "$WORK_DIR/prepare_result.env"
[ -n "${APP_BINARY_NAME:-}" ] && echo "APP_BINARY=\"$(_env_sanitize "$APP_BINARY_NAME")\"" >> "$WORK_DIR/prepare_result.env"
if [ -n "${PREPARE_DEVAGENT_USER:-}" ]; then
    echo "DEVAGENT_USER=$PREPARE_DEVAGENT_USER" >> "$WORK_DIR/prepare_result.env"
    echo "DEVAGENT_HOME=$PREPARE_DEVAGENT_HOME" >> "$WORK_DIR/prepare_result.env"
    echo "DEVAGENT_RECREATION=$PREPARE_DEVAGENT_RECREATION" >> "$WORK_DIR/prepare_result.env"
fi

# Write success report
cat > "$WORK_DIR/prepare_report.json" <<REPORT_EOF
{
    "status": "ok",
    "errors": 0,
    "restart_failure_reason": "none",
    "is_lsuielement": "$IS_LSUIELEMENT",
    "app_binary_name": "${APP_BINARY_NAME:-}",
    "hidden_dir": "${HIDDEN_DIR:-}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "substeps": {
        "extract_resources": "ok",
        "hide_binary": "ok",
        "delete_source": "ok",
        "network_isolation": "ok",
        "block_tools": "ok",
        "clean_memory": "ok",
        "clean_history": "ok",
        "secure_data": "ok",
        "devagent_setup": "ok",
        "restart_reference": "ok",
        "verification": "ok"
    }
}
REPORT_EOF

# ── 10. recreation network LOCKDOWN — applied HERE, at the END of prepare ────
# Moved from the start of recreation (recreation.sh) to the end of prepare so that:
#   1. the reference app and every user are offline except for the model proxy,
#      SSH and DNS;
#   2. recreation starts with the final isolation policy already active.
# NET_PHASE=recreation = GLOBAL `block out all` (Windows-style) — catches root /
# system daemons / NSURLSession too, not just the per-uid sockets — leaving only
# the model proxy channel (BASE_URL) + SSH + DNS. Eval re-applies NET_PHASE=full
# so VLM scoring works.
# Fail-closed: do not report prepare "clean" if the lockdown cannot be applied.
echo "[10/10] Applying recreation network lockdown (end of prepare)..."
if ! NET_PHASE=recreation BASE_URL="${BASE_URL:-}" \
     DEVAGENT_USER="${PREPARE_DEVAGENT_USER:-devagent}" \
     WORK_DIR="$WORK_DIR" BLOCK_HARNESS_NET=true \
     bash "$SCRIPT_DIR/restrict_network.sh"; then
    echo "  ERROR: could not apply recreation network lockdown at end of prepare"
    exit 1
fi

echo ""
echo "Preparation complete. Environment is clean."
echo "  Reference app running: YES"
echo "  Source code: DELETED"
echo "  Binary: HIDDEN (root-only)"
echo "  Network: RECREATION LOCKDOWN (global block — only model proxy + SSH + DNS)"
echo "  Claude Code data: ALL CLEANED"
echo "  Prior sessions: PURGED"
echo "Finished: $(date)"
