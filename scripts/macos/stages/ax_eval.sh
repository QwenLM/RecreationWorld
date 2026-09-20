#!/bin/bash
# ax_eval.sh — Score a recreation against the frozen AX suite.
#
# The release accepts only the frozen test_ax_*.py suite.
#
# Required env vars:
#   APP_NAME, WORK_DIR, MODEL
# Optional:
#   RECREATED_APP, RECREATION_DIR
#
# Outputs:
#   $RESULTS_DIR/ — pytest_result.txt or ax_results.json, score.json, recreated source
set -euo pipefail
trap '' SIGPIPE
# Ensure common bin dirs are in PATH (SSH non-login shells have minimal PATH)
export PATH="$HOME/local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIPELINE_DIR="${PIPELINE_DIR:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
MACOS_RUNTIME="$PIPELINE_DIR/scripts/macos/runtime.py"


# EXIT trap: clean up MCP processes on unexpected exit
eval_cleanup() {
    pkill -f "qwen-cua-driver" 2>/dev/null || true
}
trap eval_cleanup EXIT

# Helper: write zero-score results when recreation produced no working app.
# Exit 0 (pipeline succeeded, agent scored 0). Exit 1 is reserved for infra failures.
_exit_zero_score() {
    local reason="${1:-agent_failed}"
    local score_reason="RECREATION_FAIL"
    case "$reason" in
        app_launch_failed) score_reason="LAUNCH_FAIL" ;;
        no_app_found)      score_reason="RECREATION_FAIL" ;;
    esac

    # Count actual test cases so TOTAL reflects real denominator
    local test_total
    test_total=$(python3 "$MACOS_RUNTIME" count-ax-tests "$TESTS_DIR" \
        2>/dev/null || echo "0")

    # Count VLM assertions from test code (so RECREATION_FAIL counts as 0/N, not excluded).
    # Each screenshot_assert() call = 1 VLM test case. Exclude the function definition itself.
    local vlm_total
    vlm_total=$(rb_frozen_vlm_total "${TESTS_DIR}")
    [ -z "$vlm_total" ] && vlm_total=0

    echo '{"passed": 0, "total": '"$test_total"', "error": "'"$reason"'", "score_reason": "'"$score_reason"'"}' > "$RESULTS_DIR/score.json"
    echo "RESULTS_DIR=$RESULTS_DIR" > "$WORK_DIR/eval_result.env"
    echo "PASS_COUNT=0" >> "$WORK_DIR/eval_result.env"
    echo "FAIL_COUNT=$test_total" >> "$WORK_DIR/eval_result.env"
    echo "TOTAL=$test_total" >> "$WORK_DIR/eval_result.env"
    echo "RECREATED_APP=${RECREATED_APP:-}" >> "$WORK_DIR/eval_result.env"
    echo "TEST_FORMAT=ax_tests" >> "$WORK_DIR/eval_result.env"
    echo "VLM_PASSED=0" >> "$WORK_DIR/eval_result.env"
    echo "VLM_TOTAL=$vlm_total" >> "$WORK_DIR/eval_result.env"
    echo "SCORE_REASON=$score_reason" >> "$WORK_DIR/eval_result.env"
    python3 "$MACOS_RUNTIME" write-zero-score \
        --results-dir "$RESULTS_DIR" --app "${APP_NAME:-}" --model "${MODEL:-}" \
        --config "${RESULTS_MODEL_DIR:-}" --reason "$reason" \
        --score-reason "$score_reason" --total "$test_total" \
        --vlm-total "$vlm_total" 2>/dev/null || true
    echo "  Score: 0/$test_total ($reason — $score_reason)"
    exit 0
}

# Clean orphan MCP processes from prior stages
pkill -f "qwen-cua-driver" 2>/dev/null || true
sleep 0.5

# Load previous stage results (quote-safe: handles paths with spaces)
_safe_source() {
    local f="$1"
    [ -f "$f" ] || return 0
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        value="${value%\"}" ; value="${value#\"}"
        export "$key=$value"
    done < "$f"
}
_safe_source "$WORK_DIR/prepare_result.env"
_safe_source "$WORK_DIR/recreation_result.env"

# HIDDEN_DIR is stored in a root-only file (not in preparation_result.env for security)
HIDDEN_PATH_FILE="/var/tmp/.pipeline_hidden_path"
if [ -f "$HIDDEN_PATH_FILE" ]; then
    HIDDEN_DIR=$(sudo cat "$HIDDEN_PATH_FILE" 2>/dev/null || true)
fi

# Reference metadata was moved to HIDDEN_DIR during preparation.
if [ -n "${HIDDEN_DIR:-}" ] && sudo test -d "${HIDDEN_DIR}" 2>/dev/null; then
    if sudo test -f "$HIDDEN_DIR/reference_result.env"; then
        if sudo bash -n "$HIDDEN_DIR/reference_result.env" 2>/dev/null; then
            source <(sudo cat "$HIDDEN_DIR/reference_result.env")
        else
            echo "ERROR: reference_result.env has syntax errors" >&2
            exit 2
        fi
    fi
fi

# Frozen tests were moved to HIDDEN_DIR during preparation.
TESTS_DIR=""

# The VLM denominator is the release's canonical frozen manifest, not a count of runtime
# screenshot calls. Runtime tests can contain unbacked assertions that are intentionally
# absent from the frozen scoring inventory. The unified staging step writes that inventory
# beside the tests before they are moved into the hidden harness directory.
rb_frozen_vlm_total() {
    "${PYTHON_BIN:-python3}" "$MACOS_RUNTIME" vlm-total "$1" 2>/dev/null \
        || printf '0'
}

if [ -n "${HIDDEN_DIR:-}" ] && sudo test -d "${HIDDEN_DIR}/tests" 2>/dev/null; then
    # Make the hidden directory readable by the trusted evaluator account.
    sudo chmod -R a+rX "${HIDDEN_DIR}"
    TESTS_DIR="${HIDDEN_DIR}/tests"
else
    TESTS_DIR="${WORK_DIR}/tests"
fi
RECREATION_DIR="${RECREATION_DIR:-$HOME/Recreation/${APP_NAME}}"
RESULTS_DIR="${RESULTS_DIR:-/var/tmp/pipeline_results/${APP_NAME}/${RESULTS_MODEL_DIR:-$MODEL}}"

# Unlock results dir (save_stage_results locks to root:700 after each stage)
sudo chmod -R a+rwX "$RESULTS_DIR" 2>/dev/null || true
sudo chown -R $(whoami) "$RESULTS_DIR" 2>/dev/null || true

echo "╔══════════════════════════════════════╗"
echo "║  Eval                                ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  App:          $APP_NAME"
echo "  Model:        $MODEL"
echo "  Recreated:    ${RECREATED_APP:-not found}"
echo "  Frozen tests: $TESTS_DIR"
echo "  Results:      $RESULTS_DIR"
echo "  Started:      $(date)"
echo ""

# Create results dir as root first, then grant write access for this stage
sudo mkdir -p "/var/tmp/pipeline_results/${APP_NAME}/${RESULTS_MODEL_DIR:-$MODEL}"
sudo chown -R "$(whoami)" "/var/tmp/pipeline_results/${APP_NAME}"
sudo chmod a+x "/var/tmp/pipeline_results"
mkdir -p "$RESULTS_DIR"
mkdir -p "$WORK_DIR/logs"

# ── Compute fixed test totals from test file definitions ──────────────────
# Benchmark totals must be deterministic: count from source, not runtime results.
TRUE_AX_TOTAL=$(python3 "$MACOS_RUNTIME" count-ax-tests "$TESTS_DIR" \
    2>/dev/null || echo "0")
TRUE_VLM_TOTAL=$(rb_frozen_vlm_total "${TESTS_DIR}")
[ -z "$TRUE_VLM_TOTAL" ] && TRUE_VLM_TOTAL=0
echo "  Fixed totals: AX=$TRUE_AX_TOTAL  VLM=$TRUE_VLM_TOTAL (from test definitions)"

# ── 0. Keep the offline network restriction active for eval ───────────────
echo "[0/5] (Re)applying offline network restriction for eval (VLM allowed)..."

# Keep the reference/recreated app OFFLINE through eval so the recreated app runs
# in the SAME offline state as the fixtures it is judged against, and VLM scoring
# still works (VLM endpoint is allow-listed in restrict_network.sh). Previously
# this step did `pfctl -d` (full network restore) — that would let a network-
# dependent app behave differently at eval than at frozen-suite/recreation. Run as a
# subprocess (pf rules apply system-wide + persist) and fail closed if isolation
# cannot be established. Vars are passed explicitly
# because a bare `bash` subprocess only inherits EXPORTED vars, and VLM_BASE_URL
# is not exported until later in this stage — without this the VLM endpoint could
# be missing from the allow-list and VLM scoring would be blocked.
VLM_BASE_URL="${VLM_BASE_URL:-}" BASE_URL="${BASE_URL:-}" \
  MODEL_API_ENDPOINTS="${MODEL_API_ENDPOINTS:-}" DEVAGENT_USER="${DEVAGENT_USER:-devagent}" \
  WORK_DIR="${WORK_DIR:-}" BLOCK_HARNESS_NET="${BLOCK_HARNESS_NET:-true}" \
  bash "$SCRIPT_DIR/restrict_network.sh" || {
    echo "ERROR: could not apply required offline network restriction for eval" >&2
    exit 2
  }

# Restore /etc/hosts
if [ -f /etc/hosts.pipeline_backup ]; then
    sudo mv /etc/hosts.pipeline_backup /etc/hosts 2>/dev/null || true
else
    sudo sed -i "" '/^127\.0\.0\.1.*\(github\|pypi\|gitlab\|bitbucket\|npmjs\|raw\.githubusercontent\|registry\.npmjs\|crates\.io\|cocoapods\|rubygems\|packagist\|maven\)/d' /etc/hosts 2>/dev/null || true
fi

# Restore git wrapper
if [ -f /usr/local/bin/git.real ]; then
    sudo mv /usr/local/bin/git.real /usr/local/bin/git 2>/dev/null || true
fi
if [ -f /usr/bin/git.real ]; then
    sudo mv /usr/bin/git.real /usr/bin/git 2>/dev/null || true
fi

# Restore blocked tools
for tool in curl wget pip pip3 npm npx yarn brew; do
    if [ -f "/usr/local/bin/$tool" ] && grep -q "BLOCKED:" "/usr/local/bin/$tool" 2>/dev/null; then
        sudo rm -f "/usr/local/bin/$tool"
    fi
    for dir in /usr/local/bin /opt/homebrew/bin; do
        [ -f "$dir/$tool.blocked" ] && sudo mv "$dir/$tool.blocked" "$dir/$tool" 2>/dev/null || true
    done
done

# NOTE: the git/hosts/tool restores above are redundant with final cleanup and
# run_full's exit trap (both restore them for the next app). They do NOT re-open
# the network for this eval — the pf restriction re-applied in [0/5] is the
# backstop, so the reference/recreated app stay OFFLINE regardless.
echo "  Tools/hosts restored for next stage; network kept restricted (offline eval)"
echo ""

# ── 1. Find and launch recreated app ─────────────────────────────────────

echo "[1/5] Launching recreated app..."

# Generated launchers may embed the canonical workspace path.  Rebuild that path as a real,
# evaluator-owned directory from the frozen recreation snapshot.  This covers eval-only resumes
# without reintroducing the task-revealing symlink removed from the authoring boundary.
CANONICAL_RECREATION_WORKSPACE="/Users/Shared/workspace/recreation"
if [ -d "${RECREATION_DIR:-}" ]; then
    EVAL_AGENT_USER="${DEVAGENT_USER:-devagent}"
    if id "$EVAL_AGENT_USER" >/dev/null 2>&1; then
        for _stop_attempt in 1 2 3; do
            if ! sudo pgrep -u "$EVAL_AGENT_USER" >/dev/null 2>&1; then
                break
            fi
            sudo pkill -KILL -u "$EVAL_AGENT_USER" 2>/dev/null || true
            sleep 1
        done
        if sudo pgrep -u "$EVAL_AGENT_USER" >/dev/null 2>&1; then
            echo "ERROR: agent processes survived shutdown before eval"
            exit 2
        fi
    fi
    sudo mkdir -p "$(dirname "$CANONICAL_RECREATION_WORKSPACE")"
    EVAL_WORKSPACE_TMP="${CANONICAL_RECREATION_WORKSPACE}.eval.$$"
    sudo rm -rf "$EVAL_WORKSPACE_TMP"
    sudo mkdir -p "$EVAL_WORKSPACE_TMP"
    sudo chmod 700 "$EVAL_WORKSPACE_TMP"
    if ! sudo cp -R "$RECREATION_DIR/." "$EVAL_WORKSPACE_TMP/"; then
        echo "ERROR: could not restore eval workspace at $CANONICAL_RECREATION_WORKSPACE"
        sudo rm -rf "$EVAL_WORKSPACE_TMP"
        exit 2
    fi
    sudo chown -R "$(whoami):staff" "$EVAL_WORKSPACE_TMP"
    sudo chmod -R u+rwX,go-rwx "$EVAL_WORKSPACE_TMP"
    sudo rm -rf "$CANONICAL_RECREATION_WORKSPACE"
    sudo mv "$EVAL_WORKSPACE_TMP" "$CANONICAL_RECREATION_WORKSPACE"
    if [ -L "$CANONICAL_RECREATION_WORKSPACE" ] || [ ! -x "$CANONICAL_RECREATION_WORKSPACE" ]; then
        echo "ERROR: eval workspace is not an accessible real directory: $CANONICAL_RECREATION_WORKSPACE"
        exit 2
    fi
    echo "  Eval workspace restored: $CANONICAL_RECREATION_WORKSPACE"
fi

# Look for launch.sh (aligned with Linux: prefer launch.sh over direct open)
LAUNCH_SH=""
for search_launch in "$RECREATION_DIR/launch.sh" "$RECREATION_DIR/src/launch.sh"; do
    if [ -f "$search_launch" ]; then
        LAUNCH_SH="$search_launch"
        break
    fi
done

# Frozen suites historically invoke LAUNCH_SH with `bash`, even though the
# recreation contract allows a script to select its own interpreter.  macOS
# agents commonly emit `#!/bin/zsh` and use zsh-only expansions such as
# `${0:A:h}`.  Give the suite a bash-compatible wrapper which delegates to the
# original script through its shebang.
if [ -n "$LAUNCH_SH" ]; then
    RB_ORIGINAL_LAUNCH_SH="$LAUNCH_SH"
    LAUNCH_SH="$WORK_DIR/.rb_eval_launch.sh"
    printf '%s\n' \
        '#!/bin/bash' \
        'set -euo pipefail' \
        'exec bash "$RB_LAUNCH_RUNNER" "$RB_ORIGINAL_LAUNCH_SH" "$@"' \
        > "$LAUNCH_SH"
    chmod 700 "$LAUNCH_SH"
    export RB_ORIGINAL_LAUNCH_SH
    export RB_LAUNCH_RUNNER="$SCRIPT_DIR/../tools/run_launch.sh"
fi

if [ -z "${RECREATED_APP:-}" ] || [ ! -d "${RECREATED_APP:-}" ]; then
    RECREATED_APP=""
    for search_dir in "$RECREATION_DIR/build" "$RECREATION_DIR" "$HOME/Library/Developer/Xcode/DerivedData"; do
        if [ -d "$search_dir" ]; then
            FOUND=$(find "$search_dir" -name "*.app" -type d 2>/dev/null | head -1 || true)
            if [ -n "$FOUND" ]; then
                RECREATED_APP="$FOUND"
                break
            fi
        fi
    done
fi

if [ -z "${RECREATED_APP:-}" ] && [ -z "$LAUNCH_SH" ]; then
    echo "NOTICE: No recreated .app found and no launch.sh — agent did not produce a working recreation"
    _exit_zero_score "no_app_found"
fi

echo "  Found app: ${RECREATED_APP:-none}"
echo "  Launch script: ${LAUNCH_SH:-none (will use open)}"

# Kill reference app to avoid conflicts
# BUG-019: pgrep -f matches pipeline scripts too — match on short name only
# BUG-021: pgrep -x with [:15] misses 16-char kernel names (MAXCOMLEN=16)
APP_BINARY="${APP_BINARY:-$APP_NAME}"

# List pids of any process whose executable runs from the reference bundle path
# (.pipeline_refapp / pipeline_work/.../source/). The recreation ($RECREATED_APP)
# is explicitly excluded. Catches the reference MAIN process AND all its helpers
# (Electron/CEF), which a single recorded pid would miss.
_ref_survivors() {
    local out
    out=$(ps -axo pid=,args= 2>/dev/null \
        | grep -iE '\.pipeline_refapp|/pipeline_work/[^ ]*/source/' \
        | grep -v grep)
    if [ -n "${RECREATED_APP:-}" ]; then
        out=$(printf '%s\n' "$out" | grep -vF "$RECREATED_APP")
    fi
    printf '%s\n' "$out" | awk 'NF{print $1}'
}

# Clean-target evidence (BEFORE any kill): record the reference process(es) present at
# eval start. preparation/4 keep the reference running, so this proves what the kill
# sequence below must remove — the eval log then shows the reference was actually
# there and was eliminated before scoring.
_REF_INITIAL=$(_ref_survivors)
if [ -n "$_REF_INITIAL" ]; then
    echo "  [clean-env] reference process(es) present at eval start (pids: $_REF_INITIAL) — must be killed:"
    ps -axo pid=,args= 2>/dev/null | grep -iE '\.pipeline_refapp|/pipeline_work/[^ ]*/source/' \
        | grep -v grep | grep -vF "${RECREATED_APP:-/nonexistent/sentinel}" | sed 's/^/    /' || true
else
    echo "  [clean-env] no reference-bundle process detected at eval start"
fi

REF_PIDS=$(pgrep "${APP_BINARY:0:15}" 2>/dev/null || true)
if [ -z "$REF_PIDS" ] && [ "$APP_BINARY" != "$APP_NAME" ]; then
    REF_PIDS=$(pgrep "${APP_NAME:0:15}" 2>/dev/null || true)
fi

# Grant TCC to recreated app
if [ -n "${RECREATED_APP:-}" ]; then
    REC_BUNDLE_ID=$(defaults read "$RECREATED_APP/Contents/Info.plist" CFBundleIdentifier 2>/dev/null || echo "")
    if [ -n "$REC_BUNDLE_ID" ]; then
        SCAFFOLD_DIR="${SCAFFOLD_DIR:-$PIPELINE_DIR/scripts/macos/scaffold}"
        bash "$SCAFFOLD_DIR/pipeline/grant_tcc_permissions.sh" --bundle-id "$REC_BUNDLE_ID" 2>/dev/null || true
    fi
fi

# Kill reference app first, then launch recreated
for pid in $REF_PIDS; do
    kill "$pid" 2>/dev/null || true
done
sleep 2

# Also kill the reference by EXECUTABLE PATH and verify it is gone. Killing by name
# alone (above) misses it if the pgrep snapshot was stale or the reference was
# relaunched (preparation/4 keep it running). A surviving reference has the same binary
# name as the recreation, so it can be scored in its place (AX) or leak into the
# assertion screenshots (VLM) — the reference-contamination bug. The reference
# always runs from a pipeline-internal path (.pipeline_refapp / pipeline_work
# source); the recreation never does, so this is safe. (_ref_survivors defined above.)
for _try in 1 2 3; do
    REF_PATH_PIDS=$(_ref_survivors)
    [ -z "$REF_PATH_PIDS" ] && break
    echo "  Killing surviving reference process(es) by path: $REF_PATH_PIDS"
    for pid in $REF_PATH_PIDS; do kill -9 "$pid" 2>/dev/null || sudo kill -9 "$pid" 2>/dev/null || true; done
    sleep 1
done

# ── HARD clean-environment gate ───────────────────────────────────────────────
# eval must score the recreation in a reference-FREE environment ("like a clean
# target"). If any reference-bundle process is STILL alive after the kill loop, escalate
# to sudo, then verify one final time. If it cannot be removed we must NOT produce a
# score — a live reference (same binary name) silently inflates AX/VLM by being
# scored in the recreation's place. Fail the stage loudly instead (retryable) so a
# contaminated score can never reach the results.
REF_SURVIVORS=$(_ref_survivors)
if [ -n "$REF_SURVIVORS" ]; then
    echo "  WARNING: reference still alive after kill loop (pids: $REF_SURVIVORS) — escalating with sudo"
    for pid in $REF_SURVIVORS; do sudo kill -9 "$pid" 2>/dev/null || true; done
    sleep 2
    REF_SURVIVORS=$(_ref_survivors)
fi
if [ -n "$REF_SURVIVORS" ]; then
    echo "ERROR: reference app process(es) could not be killed before eval (pids: $REF_SURVIVORS)"
    echo "ERROR: refusing to score in a contaminated environment — a live reference inflates AX/VLM."
    ps -axo pid=,args= 2>/dev/null | grep -iE '\.pipeline_refapp|/pipeline_work/[^ ]*/source/' | grep -v grep >&2 || true
    echo "ERROR: eval failed — reference not reference-free"
    exit 1
fi
echo "  ✓ Reference-free environment verified (no reference-bundle process alive before eval)"

# Clean desktop before launching recreated app (leftover apps pollute assertion screenshots)
for gui_app in $(osascript -e 'tell application "System Events" to get name of every process whose background only is false' 2>/dev/null | tr ',' '\n' | sed 's/^ *//;s/ *$//'); do
    case "$gui_app" in
        Finder|Terminal|loginwindow|"") continue ;;
    esac
    killall "$gui_app" 2>/dev/null || true
done
osascript -e 'tell application "Finder" to close every window' 2>/dev/null || true
killall NotificationCenter 2>/dev/null || true

# Launch via launch.sh if available (aligned with Linux), otherwise fall back to open
if [ -n "$LAUNCH_SH" ]; then
    echo "  Starting via launch.sh..."
    "$LAUNCH_SH" &
    LAUNCH_PID=$!
    sleep 5
    if ! kill -0 "$LAUNCH_PID" 2>/dev/null; then
        echo "  WARNING: launch.sh process exited (may be normal for GUI apps)"
    fi
elif [ -n "${RECREATED_APP:-}" ]; then
    # BUG-020: If RECREATED_APP points to devagent's path, `open` may fail with procNotFound.
    if ! open "$RECREATED_APP" 2>/dev/null; then
        echo "  WARNING: Failed to open $RECREATED_APP — trying the login-account copy"
        FALLBACK_APP=$(find "$RECREATION_DIR" -name "*.app" -type d 2>/dev/null | head -1 || true)
        if [ -n "$FALLBACK_APP" ]; then
            RECREATED_APP="$FALLBACK_APP"
            echo "  Using fallback: $RECREATED_APP"
            open "$RECREATED_APP" || {
                echo "NOTICE: Cannot launch recreated app"
                _exit_zero_score "app_launch_failed"
            }
        else
            echo "NOTICE: Cannot launch recreated app and no fallback found"
            _exit_zero_score "app_launch_failed"
        fi
    fi
else
    echo "NOTICE: No way to launch recreated app"
    _exit_zero_score "app_launch_failed"
fi
# Give the recreated app time to finish launching before the AX verifier runs.
# Slow-starting apps can otherwise be probed before their UI is up → false 0 score.
sleep 30

# Diagnostic (clean-target evidence): record the GUI apps actually running at scoring
# time. The clean-environment gate above already guarantees no reference-bundle
# process is alive; this makes the eval log itself the audit trail that ONLY the
# recreated app (+ Finder/Terminal shells) is open when the verifier scores it.
echo "  [clean-env] Foreground GUI apps open at eval time:"
osascript -e 'tell application "System Events" to get name of every process whose background only is false' 2>/dev/null \
    | tr ',' '\n' | sed 's/^ *//;s/ *$//' | sed 's/^/    - /' || true
_REF_DIAG=$(_ref_survivors)
if [ -n "$_REF_DIAG" ]; then
    echo "  [clean-env] UNEXPECTED: reference-bundle pids alive at eval time: $_REF_DIAG"
else
    echo "  [clean-env] Reference-bundle processes at eval time: (none — reference-free confirmed)"
fi

# Capture the eval-time desktop (VNC-equivalent frame): what the scorer actually sees.
# With the reference killed and the desktop cleaned, this should show ONLY the recreated
# app. Uploaded per-app (run_full.sh eval block) so the frame is reviewable offline —
# the scalable substitute for manually VNC-ing each app.
_EVAL_SHOT="$WORK_DIR/logs/eval_desktop.png"
screencapture -x "$_EVAL_SHOT" 2>/dev/null || true
if [ -f "$_EVAL_SHOT" ]; then
    echo "  [clean-env] eval-time desktop screenshot saved: $_EVAL_SHOT ($(stat -f%z "$_EVAL_SHOT" 2>/dev/null || echo '?') bytes)"
else
    echo "  [clean-env] WARNING: eval-time screenshot could not be captured"
fi

# ── 2. Run verifier ──────────────────────────────────────────────────────

echo "[2/5] Running verifier..."

# Frozen macOS suites consist of test_ax_*.py lanes.
AX_TEST_COUNT=$(find "$TESTS_DIR" -maxdepth 1 -name "test_ax_*.py" 2>/dev/null | wc -l || true)

export APP_PATH="$RECREATED_APP"
export LAUNCH_SH="${LAUNCH_SH:-}"

if [ "$AX_TEST_COUNT" -gt 0 ]; then
    # ── New format: test_ax_*.py via ax_test_runner.py ──
    echo "  Format: test_ax_*.py ($AX_TEST_COUNT files)"

    # Export the canonical judge settings for the post-test shared VLM phases.
    export VLM_API_KEY="${VLM_API_KEY:-}"
    export VLM_BASE_URL="${VLM_BASE_URL:-}"
    export VLM_MODEL="${VLM_MODEL:-}"
    # Find official screenshots (may be in HIDDEN_DIR after preparation isolation)
    if [ -n "${HIDDEN_DIR:-}" ] && sudo test -d "${HIDDEN_DIR}/tests/official_screenshots" 2>/dev/null; then
        sudo chmod -R a+rX "${HIDDEN_DIR}/tests/official_screenshots" 2>/dev/null || true
        export OFFICIAL_SCREENSHOTS_DIR="${HIDDEN_DIR}/tests/official_screenshots"
    elif [ -d "$TESTS_DIR/official_screenshots" ]; then
        export OFFICIAL_SCREENSHOTS_DIR="$TESTS_DIR/official_screenshots"
    fi

    # Integrity check: verify test files haven't been tampered with
    if [ -n "${HIDDEN_DIR:-}" ] && [ -f "$HIDDEN_DIR/test_checksums.sha256" ]; then
        echo "  Checking test file integrity..."
        TAMPERED=false
        while IFS=' ' read -r expected_sha file_path; do
            if [ -f "$file_path" ]; then
                actual_sha=$(shasum -a 256 "$file_path" 2>/dev/null | awk '{print $1}' || true)
                if [ "$actual_sha" != "$expected_sha" ]; then
                    echo "  TAMPERED: $file_path"
                    TAMPERED=true
                fi
            fi
        done < "$HIDDEN_DIR/test_checksums.sha256"
        if [ "$TAMPERED" = true ]; then
            echo "FATAL: Test files integrity check FAILED — may have been tampered during Recreation"
            echo '{"passed": 0, "total": 0, "error": "tests_tampered"}' > "$RESULTS_DIR/score.json"
            exit 1
        fi
        echo "  Integrity check: OK"
    else
        echo "  WARNING: No HIDDEN_DIR or test_checksums.sha256 — cannot verify test integrity"
        echo "  HIDDEN_DIR=${HIDDEN_DIR:-<empty>}"
    fi

    AX_RESULTS_DIR="$RESULTS_DIR/ax_results"
    FIXTURES_DIR="$TESTS_DIR/fixtures"
    mkdir -p "$AX_RESULTS_DIR"

    # Prefer pipeline's ax_test_runner.py (has auto-screenshot), fall back to agent-generated
    PIPELINE_AX_RUNNER="$PIPELINE_DIR/scripts/macos/tools/ax_test_runner.py"
    export APP_PATH="${RECREATED_APP:-}"
    export APP_BINARY="${APP_BINARY:-$APP_NAME}"
    export LAUNCH_SH="${LAUNCH_SH:-}"

    # ── TCC preflight gate (fixes per-sandbox tccd race) ─────────────────────────────
    # If the AX test-runner is not a trusted Accessibility client, every AX query EPERMs
    # and the whole app scores a silent 0 (looks like a bad recreation). Detect it, try to
    # re-grant + reload tccd, and if STILL untrusted, fail as INFRA (exit 1, retryable) so
    # the job is re-run on a fresh sandbox instead of recording a fake 0.
    _axrc=$(python3 "$MACOS_RUNTIME" accessibility-status 2>/dev/null || echo 2)
    if [ "$_axrc" = "1" ]; then
        echo "  [TCC preflight] AXIsProcessTrusted=False — re-granting Accessibility + reloading tccd"
        for _c in "/usr/libexec/sshd-keygen-wrapper" "com.apple.Terminal"; do
            for _s in kTCCServiceAccessibility kTCCServiceScreenCapture kTCCServiceAppleEvents kTCCServicePostEvent; do
                sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" "INSERT OR REPLACE INTO access (service,client,client_type,auth_value,auth_reason,auth_version,flags) VALUES ('$_s','$_c',0,2,0,1,0)" 2>/dev/null || true
            done
        done
        sudo killall tccd 2>/dev/null || killall tccd 2>/dev/null || true
        sleep 3
        _axrc=$(python3 "$MACOS_RUNTIME" accessibility-status 2>/dev/null || echo 2)
        if [ "$_axrc" = "1" ]; then
            echo "  [TCC preflight] STILL untrusted after re-grant + tccd reload → INFRA FAILURE (retryable), NOT scoring 0"
            exit 1
        fi
        echo "  [TCC preflight] Accessibility recovered after tccd reload"
    fi

    if [ -f "$PIPELINE_AX_RUNNER" ]; then
        python3 "$PIPELINE_AX_RUNNER" \
            --app-path "$RECREATED_APP" \
            --app-name "$APP_NAME" \
            --tests "$TESTS_DIR" \
            --results "$AX_RESULTS_DIR" \
            --fixtures "$FIXTURES_DIR" \
            --summary ax_results.json \
            --print-summary \
            > "$RESULTS_DIR/ax_test_output.txt" 2>&1 || true
        echo "  (full output in ax_test_output.txt — showing summary)"
        grep -E "FAIL|ERROR|error|Score|Total|passed|failed|Summary" "$RESULTS_DIR/ax_test_output.txt" 2>/dev/null | tail -30 || true
    elif [ -f "$TESTS_DIR/run_ax_tests.py" ]; then
        python3 "$TESTS_DIR/run_ax_tests.py" \
            --app-path "$RECREATED_APP" \
            --app-name "$APP_NAME" \
            --tests "$TESTS_DIR" \
            --results "$AX_RESULTS_DIR" \
            --fixtures "$FIXTURES_DIR" \
            --summary ax_results.json \
            --print-summary \
            > "$RESULTS_DIR/ax_test_output.txt" 2>&1 || true
        echo "  (full output in ax_test_output.txt — showing summary)"
        grep -E "FAIL|ERROR|error|Score|Total|passed|failed|Summary" "$RESULTS_DIR/ax_test_output.txt" 2>/dev/null | tail -30 || true
    else
        echo "  WARNING: No test runner found, running tests individually"
        for tf in "$TESTS_DIR/"test_ax_*.py; do
            python3 "$tf" --app-name "$APP_NAME" --results-dir "$AX_RESULTS_DIR" 2>/dev/null || true
        done
    fi

    # Read results from ax_results.json (produced by any runner path above)
    if [ -f "$AX_RESULTS_DIR/ax_results.json" ]; then
        read -r PASS_COUNT TOTAL PASS_RATE < <(python3 "$MACOS_RUNTIME" json-fields \
            "$AX_RESULTS_DIR/ax_results.json" passed total pass_rate 2>/dev/null \
            || echo "0 0 0")
        FAIL_COUNT=$((TOTAL - PASS_COUNT))
        cp "$AX_RESULTS_DIR/ax_results.json" "$RESULTS_DIR/score.json"
    else
        PASS_COUNT=0
        FAIL_COUNT=0
        TOTAL=0
        PASS_RATE=0
    fi

else
    echo "ERROR: Frozen suite has no test_ax_*.py files"
    echo '{"passed": 0, "total": 0, "error": "no_tests"}' > "$RESULTS_DIR/score.json"
    exit 1
fi

echo ""
echo "  Results: $PASS_COUNT passed, $FAIL_COUNT failed (total: $TOTAL)"

# ── 3. VLM Phase 2: Screenshot assertion judging ────────────────────────

echo "[3/4] VLM Phase 2: Screenshot assertion judging..."

VLM_JUDGE="$PIPELINE_DIR/scripts/common/vlm_judge.py"
VLM_PASSED=0
VLM_TOTAL=0
VLM_INFRA_FAILURE=0
VLM_RUNTIME_ASSERTIONS=0
VLM_JUDGED=0

# The candidate app and GUI harness deliberately share the login account's UID, which remains
# offline during eval.  The VLM judge is infrastructure, not part of the candidate, and must
# not inherit that UID-scoped deny rule.  Run only the file-based judge processes as root;
# the candidate keeps running as that account and therefore stays offline. Credentials remain in
# the controller-written 0600 env file (never argv), matching the rest of the macOS path.
_run_vlm_python() {
    if [ "$(id -u)" = "0" ]; then
        python3 "$@"
    else
        sudo -n bash -c \
            'set -a; . "$1"; set +a; shift; exec python3 "$@"' \
            _ "$PIPELINE_DIR/.runtime_env" "$@"
    fi
}

if [ -f "$VLM_JUDGE" ] && [ -n "${VLM_API_KEY:-}" ] && \
   [ -n "${VLM_BASE_URL:-}" ] && [ -n "${VLM_MODEL:-}" ]; then
    # Judge exactly the canonical frozen inventory. Runtime helpers may emit extra
    # assertions; judging those and merely overriding the total afterwards can produce
    # impossible scores such as 8/7. Conversely, a canonical checkpoint not reached by
    # the app must remain in the denominator as a missing-image failure.
    VLM_DIRS=()
    if [ "$TRUE_VLM_TOTAL" -gt 0 ]; then
        VLM_MANIFEST="$TESTS_DIR/test_manifest.json"
        [ -r "$VLM_MANIFEST" ] || {
            echo "ERROR: canonical frozen VLM manifest is missing: $VLM_MANIFEST"
            exit 1
        }
        VLM_CANONICAL_DIR="$RESULTS_DIR/vlm_canonical"
        rm -rf "$VLM_CANONICAL_DIR"
        mkdir -p "$VLM_CANONICAL_DIR"
        VLM_RUNTIME_ASSERTIONS=$(python3 "$MACOS_RUNTIME" stage-vlm \
            "$VLM_MANIFEST" "$AX_RESULTS_DIR" "$VLM_CANONICAL_DIR") || {
            echo "ERROR: failed to stage canonical frozen VLM assertions"
            exit 1
        }
        VLM_DIRS+=("$VLM_CANONICAL_DIR")
        echo "  Canonical VLM checkpoints reached: $VLM_RUNTIME_ASSERTIONS/$TRUE_VLM_TOTAL"
    fi

    if [ "${#VLM_DIRS[@]}" -gt 0 ]; then
        VLM_OUTPUT="$RESULTS_DIR/vlm_aggregate.json"

        # VLM creds via env (NOT argv — argv is world-visible via ps).
        export VLM_API_KEY VLM_BASE_URL VLM_MODEL
        _run_vlm_python "$VLM_JUDGE" \
            "${VLM_DIRS[@]}" \
            --model "$VLM_MODEL" \
            --missing-as-fail \
            --output "$VLM_OUTPUT" \
            > "$RESULTS_DIR/vlm_judge_output.txt" 2>&1 || VLM_JUDGE_RC=$?
        grep -E "PASS|FAIL|ERR |Score|Total|Aggregate" "$RESULTS_DIR/vlm_judge_output.txt" 2>/dev/null | tail -20 || true
        if [ -n "${VLM_JUDGE_RC:-}" ]; then
            # `|| true` used to swallow this. The judge exited 2 for over a month on an
            # --output flag it did not accept, and the block below still produced a
            # confident "0 of 9" out of the authored assertions.
            echo "  WARNING: vlm_judge.py exited ${VLM_JUDGE_RC} — see vlm_judge_output.txt"
            head -3 "$RESULTS_DIR/vlm_judge_output.txt" 2>/dev/null | sed 's/^/    /'
            VLM_INFRA_FAILURE=1
        fi

        # Prefer the judge's own aggregate: it reports how many assertions actually got a
        # verdict (`total`) separately from how many were authored (`authored`) and how
        # many could not be judged at all (`errors`). VLM_JUDGED is what decides whether
        # this dimension was measured; summing the authored files cannot tell the
        # difference between "graded 0" and "never graded".
        VLM_JUDGED=0
        if [ -f "$VLM_OUTPUT" ]; then
            read -r VLM_PASSED VLM_JUDGED VLM_ERRORS < <(
                python3 "$MACOS_RUNTIME" json-fields \
                    "$VLM_OUTPUT" passed total errors 2>/dev/null || echo "0 0 0"
            )
            VLM_TOTAL=$VLM_JUDGED
            if [ "${VLM_ERRORS:-0}" -gt 0 ] 2>/dev/null; then
                echo "  ERROR: ${VLM_ERRORS} canonical assertion(s) could not be judged"
                VLM_INFRA_FAILURE=1
            fi
        fi
        echo "  VLM Score: $VLM_PASSED / $VLM_TOTAL"

        # Merge VLM results into score.json
        if [ -f "$RESULTS_DIR/score.json" ] && [ "$VLM_TOTAL" -gt 0 ]; then
            python3 "$MACOS_RUNTIME" merge-visual \
                "$RESULTS_DIR/score.json" "$VLM_PASSED" "$VLM_TOTAL"
            echo "  VLM results merged into score.json"
        fi
    else
        echo "  No runtime assertions.json found — no frozen VLM checkpoint was reached"
    fi
else
    if [ ! -f "$VLM_JUDGE" ]; then
        echo "  WARNING: vlm_judge.py not found at $VLM_JUDGE"
    elif [ -z "${VLM_API_KEY:-}" ]; then
        echo "  WARNING: VLM_API_KEY not set — skipping VLM phase"
    elif [ -z "${VLM_BASE_URL:-}" ]; then
        echo "  WARNING: VLM_BASE_URL not set — skipping VLM phase"
    elif [ -z "${VLM_MODEL:-}" ]; then
        echo "  WARNING: VLM_MODEL not set — skipping VLM phase"
    fi
    if [ "${TRUE_VLM_TOTAL:-0}" -gt 0 ] 2>/dev/null; then
        VLM_INFRA_FAILURE=1
    fi
fi

# ── 4. Archive results (organized structure) ─────────────────────────────

echo "[4/4] Archiving results..."

TEST_FORMAT="ax_tests"

# Create organized directory structure
mkdir -p "$RESULTS_DIR/scores/modules"
mkdir -p "$RESULTS_DIR/screenshots/official"
mkdir -p "$RESULTS_DIR/screenshots/recreated"
mkdir -p "$RESULTS_DIR/screenshots/assertions"
mkdir -p "$RESULTS_DIR/testcases"
mkdir -p "$RESULTS_DIR/source"
mkdir -p "$RESULTS_DIR/logs"
mkdir -p "$RESULTS_DIR/sessions"

# ── scores/ — AX module results + VLM results ────────────────────────────

if [ -d "$RESULTS_DIR/ax_results" ]; then
    # Move per-module results into scores/modules/
    for module_dir in "$RESULTS_DIR/ax_results/"*/; do
        [ -d "$module_dir" ] || continue
        module_name=$(basename "$module_dir")
        # Move module JSON result
        for jf in "$module_dir"*_results.json; do
            [ -f "$jf" ] && mv "$jf" "$RESULTS_DIR/scores/modules/${module_name}.json" 2>/dev/null || true
        done
        # Move VLM/assertion JSONs from screenshots subdir to scores/
        if [ -d "$module_dir/screenshots" ]; then
            [ -f "$module_dir/screenshots/vlm_results.json" ] && \
                mv "$module_dir/screenshots/vlm_results.json" "$RESULTS_DIR/scores/vlm_${module_name}.json" 2>/dev/null || true
            [ -f "$module_dir/screenshots/assertions.json" ] && \
                cp "$module_dir/screenshots/assertions.json" "$RESULTS_DIR/screenshots/assertions/" 2>/dev/null || true
            for png in "$module_dir/screenshots/"*.png; do
                [ -f "$png" ] && cp "$png" "$RESULTS_DIR/screenshots/assertions/" 2>/dev/null || true
            done
        fi
    done
    # Move ax_results.json → scores/ax_summary.json
    [ -f "$RESULTS_DIR/ax_results/ax_results.json" ] && \
        mv "$RESULTS_DIR/ax_results/ax_results.json" "$RESULTS_DIR/scores/ax_summary.json" 2>/dev/null || true
    # Remove the now-empty ax_results tree
    rm -rf "$RESULTS_DIR/ax_results" 2>/dev/null || true
    echo "  scores/ organized"
fi

# Move VLM results already at top level into scores/
[ -f "$RESULTS_DIR/vlm_aggregate.json" ] && \
    mv "$RESULTS_DIR/vlm_aggregate.json" "$RESULTS_DIR/scores/vlm_aggregate.json" 2>/dev/null || true
[ -f "$RESULTS_DIR/vlm_judge_output.txt" ] && \
    mv "$RESULTS_DIR/vlm_judge_output.txt" "$RESULTS_DIR/logs/vlm_judge_output.txt" 2>/dev/null || true
[ -f "$RESULTS_DIR/ax_test_output.txt" ] && \
    mv "$RESULTS_DIR/ax_test_output.txt" "$RESULTS_DIR/logs/ax_test_output.txt" 2>/dev/null || true
[ -f "$RESULTS_DIR/pytest_result.txt" ] && \
    mv "$RESULTS_DIR/pytest_result.txt" "$RESULTS_DIR/logs/pytest_result.txt" 2>/dev/null || true

# ── screenshots/ — official + recreated + assertions (separated) ──────────

# Copy official screenshots (pairwise baseline from tests)
if [ -n "${OFFICIAL_SS_DIR:-}" ] && sudo test -d "$OFFICIAL_SS_DIR"; then
    sudo cp "$OFFICIAL_SS_DIR/"*.png "$RESULTS_DIR/screenshots/official/" 2>/dev/null || true
    sudo chmod -R a+rX "$RESULTS_DIR/screenshots/official/" 2>/dev/null || true
    sudo cp "$OFFICIAL_SS_DIR/assertions.json" "$RESULTS_DIR/screenshots/assertions.json" 2>/dev/null || true
elif [ -d "$TESTS_DIR/official_screenshots" ]; then
    cp "$TESTS_DIR/official_screenshots/"*.png "$RESULTS_DIR/screenshots/official/" 2>/dev/null || true
    cp "$TESTS_DIR/official_screenshots/assertions.json" "$RESULTS_DIR/screenshots/assertions.json" 2>/dev/null || true
fi

# Some frozen AX helpers still write screenshots to this fixed runtime directory.
if [ -d "/tmp/verifier_screenshots" ]; then
    for png in "/tmp/verifier_screenshots/"*.png; do
        [ -f "$png" ] || continue
        cp "$png" "$RESULTS_DIR/screenshots/assertions/" 2>/dev/null || true
    done
fi

OFFICIAL_COUNT=$(find "$RESULTS_DIR/screenshots/official" -name "*.png" 2>/dev/null | wc -l | tr -d ' ' || true)
RECREATED_COUNT=$(find "$RESULTS_DIR/screenshots/recreated" -name "*.png" 2>/dev/null | wc -l | tr -d ' ' || true)
ASSERTION_COUNT=$(find "$RESULTS_DIR/screenshots/assertions" -name "*.png" 2>/dev/null | wc -l | tr -d ' ' || true)
echo "  screenshots: official=$OFFICIAL_COUNT recreated=$RECREATED_COUNT assertions=$ASSERTION_COUNT"

# Remove empty screenshot dirs
rmdir "$RESULTS_DIR/screenshots/official" 2>/dev/null || true
rmdir "$RESULTS_DIR/screenshots/recreated" 2>/dev/null || true
rmdir "$RESULTS_DIR/screenshots/assertions" 2>/dev/null || true
rmdir "$RESULTS_DIR/screenshots" 2>/dev/null || true

# ── testcases/ — test scripts from tests ────────────────────────────────

for tf in "$TESTS_DIR/"test_ax_*.py; do
    [ -f "$tf" ] && cp "$tf" "$RESULTS_DIR/testcases/" 2>/dev/null || true
done
cp "$TESTS_DIR/run_ax_tests.py" "$RESULTS_DIR/testcases/" 2>/dev/null || true
cp "$TESTS_DIR/ax_helpers.py" "$RESULTS_DIR/testcases/" 2>/dev/null || true
cp "$TESTS_DIR/verifier_helpers.py" "$RESULTS_DIR/testcases/" 2>/dev/null || true
cp "$TESTS_DIR/feature_list.md" "$RESULTS_DIR/testcases/" 2>/dev/null || true
echo "  testcases/ archived"

# ── source/ — code artifacts ─────────────────────────────────────────────

if [ -d "$RECREATION_DIR" ]; then
    cd "$RECREATION_DIR"
    tar czf "$RESULTS_DIR/source/recreation.tar.gz" \
        --exclude='build' --exclude='Build' --exclude='DerivedData' \
        --exclude='node_modules' --exclude='.git' \
        . 2>/dev/null || true
    echo "  source/recreation.tar.gz archived"
fi

# ── logs/ — all stage logs ───────────────────────────────────────────────

for logf in "$WORK_DIR/logs/"stage*.log; do
    [ -f "$logf" ] && cp "$logf" "$RESULTS_DIR/logs/" 2>/dev/null || true
done
for envf in "$WORK_DIR/"stage*_result.env; do
    [ -f "$envf" ] && cp "$envf" "$RESULTS_DIR/logs/" 2>/dev/null || true
done
echo "  logs/ archived"

# ── sessions/ — agent transcripts ──────────────────────────────────────────

if [ -n "${HIDDEN_DIR:-}" ] && sudo test -d "${HIDDEN_DIR}/sessions" 2>/dev/null; then
    for tj in "$HIDDEN_DIR/sessions/"*; do
        sudo test -f "$tj" 2>/dev/null || continue
        local_name=$(basename "$tj")
        # preparation already prefixes files (reference_main_, tests_sub_, etc.)
        # just preserve the name as-is
        sudo cp "$tj" "$RESULTS_DIR/sessions/${local_name}" 2>/dev/null || true
    done
    echo "  sessions/ archived (reference/2)"
fi
# Save recreation transcripts from both run accounts via the shared collector.
# (core/trajectory.py), replacing the two hand-rolled find/cp walks that lived here. Those walks
# preserved Claude Code's derived files (.checkpoint. / trajectory.jsonl) as if they were
# transcripts, and they named the root user's files `recreation_sub_*` with NO user label while
# run_full.sh -- same pipeline, same stage -- had already moved to `recreation_root_sub_*`. Two
# namings for one artifact in one run. The module owns the multi-user walk, so both users are
# one call, and it copies the stream-json as recreation_stream.jsonl (the name
# The macOS target runtime finds this by `find -name`, so it must not change).
_AX_TRAJ_HOMES="$HOME:root"
if [ -n "${DEVAGENT_HOME:-}" ]; then
    _AX_TRAJ_HOMES="${_AX_TRAJ_HOMES},$DEVAGENT_HOME:devagent"
fi
sudo PYTHONPATH="$PIPELINE_DIR/scripts" python3 -m core.trajectory collect \
    --agent-home "$_AX_TRAJ_HOMES" \
    --dest "$RESULTS_DIR/sessions" \
    --stage recreation \
    --stream "$WORK_DIR/logs/recreation_trajectory.jsonl" 2>&1 || \
    echo "  WARN: recreation transcript collect failed (non-fatal)"

# ── Override runtime totals with fixed test-file totals ───────────────────
# Benchmark requires deterministic totals from test definitions, not runtime.
if [ "${TRUE_AX_TOTAL:-0}" -gt 0 ] 2>/dev/null; then
    TOTAL=$TRUE_AX_TOTAL
    FAIL_COUNT=$((TOTAL - PASS_COUNT))
    [ "$FAIL_COUNT" -lt 0 ] && FAIL_COUNT=0
    if [ "$TOTAL" -gt 0 ]; then
        PASS_RATE=$(python3 "$MACOS_RUNTIME" percentage "$PASS_COUNT" "$TOTAL" \
            2>/dev/null || echo "0")
    else
        PASS_RATE=0
    fi
    echo "  AX totals overridden: PASS=$PASS_COUNT FAIL=$FAIL_COUNT TOTAL=$TOTAL (from test definitions)"
fi
if [ "${TRUE_VLM_TOTAL:-0}" -gt 0 ] 2>/dev/null; then
    if [ "${VLM_JUDGED:-0}" -gt 0 ] 2>/dev/null; then
        # The frozen authored count is the right denominator ONCE the judge has graded
        # something: it keeps the divisor identical across recreations.
        VLM_TOTAL=$TRUE_VLM_TOTAL
        echo "  VLM total overridden: PASSED=${VLM_PASSED:-0} TOTAL=$VLM_TOTAL (from test definitions)"
    elif [ "${VLM_RUNTIME_ASSERTIONS:-0}" -eq 0 ] 2>/dev/null; then
        # Every frozen visual test failed before its screenshot_assert() call.  The
        # helper always writes assertions.json once called (even if screencapture
        # fails), so this is candidate behavior: none of the authored visual states
        # was reached.  Score the fixed frozen denominator as 0/N.  Treating this as
        # infra made weak recreations red and discarded a valid zero-dimensional
        # score, while faithful recreations in the same run stayed green.
        VLM_PASSED=0
        VLM_TOTAL=$TRUE_VLM_TOTAL
        echo "  VLM checkpoints reached: 0 of ${TRUE_VLM_TOTAL} — scoring candidate 0/${TRUE_VLM_TOTAL}"
    else
        # Nothing was judged. Applying the authored count here is what turned a judge
        # that never ran into a flat "0 of 9" — indistinguishable from a model that
        # satisfied no assertion.  Runtime assertion files prove that the judge had
        # work, so leave the total at 0 and fail as infrastructure.  This is distinct
        # from the no-file branch above, where screenshot_assert() was never reached.
        VLM_TOTAL=0
        VLM_INFRA_FAILURE=1
        echo "  VLM NOT MEASURED: 0 of ${TRUE_VLM_TOTAL} authored assertions were judged" \
             "— failing eval as infrastructure instead of reporting success"
    fi
fi

# ── summary.json — final combined score ──────────────────────────────────

python3 "$MACOS_RUNTIME" write-results \
    --results-dir "$RESULTS_DIR" --app "$APP_NAME" --model "$MODEL" \
    --config "${RESULTS_MODEL_DIR:-}" --test-format "$TEST_FORMAT" \
    --passed "$PASS_COUNT" --failed "${FAIL_COUNT:-0}" --total "$TOTAL" \
    --vlm-passed "${VLM_PASSED:-0}" --vlm-total "${VLM_TOTAL:-0}" \
    --official-count "$OFFICIAL_COUNT" --recreated-count "$RECREATED_COUNT" \
    --assertion-count "$ASSERTION_COUNT"

# ── Standardized output files (aligned from Linux pipeline.py verify_eval) ──
# pipeline.py verify_eval() checks for programmatic_results.json and vlm_results.json


echo "  Standardized outputs: programmatic_results.json, vlm_results.json"

# Remove score.json (replaced by summary.json)
[ -f "$RESULTS_DIR/score.json" ] && mv "$RESULTS_DIR/score.json" "$RESULTS_DIR/scores/score.json" 2>/dev/null || true

# Clean up redundant flat copies that are now organized.
rm -f "$RESULTS_DIR/trajectory.log" "$RESULTS_DIR/build_result.json" 2>/dev/null || true
rm -f "$RESULTS_DIR/verifier.py" "$RESULTS_DIR/ax_helpers.py" "$RESULTS_DIR/run_ax_tests.py" "$RESULTS_DIR/verifier_helpers.py" 2>/dev/null || true
rm -f "$RESULTS_DIR/"test_ax_*.py 2>/dev/null || true
rm -f "$RESULTS_DIR/feature_list.md" "$RESULTS_DIR/interaction_log.md" 2>/dev/null || true
rm -rf "$RESULTS_DIR/recreated_source" "$RESULTS_DIR/official_screenshots" 2>/dev/null || true

echo "  Results archived to: $RESULTS_DIR"

# Save eval result env
echo "RESULTS_DIR=$RESULTS_DIR" > "$WORK_DIR/eval_result.env"
echo "PASS_COUNT=$PASS_COUNT" >> "$WORK_DIR/eval_result.env"
echo "FAIL_COUNT=$FAIL_COUNT" >> "$WORK_DIR/eval_result.env"
echo "TOTAL=$TOTAL" >> "$WORK_DIR/eval_result.env"
echo "RECREATED_APP=$RECREATED_APP" >> "$WORK_DIR/eval_result.env"
echo "TEST_FORMAT=$TEST_FORMAT" >> "$WORK_DIR/eval_result.env"
echo "VLM_PASSED=${VLM_PASSED:-0}" >> "$WORK_DIR/eval_result.env"
echo "VLM_TOTAL=${VLM_TOTAL:-0}" >> "$WORK_DIR/eval_result.env"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║  Eval Complete                    ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  App:          $APP_NAME"
echo "  Model:        $MODEL"
echo "  Format:       $TEST_FORMAT"
echo "  AX Score:     $PASS_COUNT / $TOTAL"
echo "  VLM Score:    ${VLM_PASSED:-0} / ${VLM_TOTAL:-0}"
echo "  Results:      $RESULTS_DIR"
echo "  Finished:     $(date)"

# Save eval trajectory (save_stage_results in run_full.sh may use old code in memory)
echo "  Saving eval transcripts..."
# ONE shared collector (core/trajectory.py), replacing two more hand-rolled walks — the
# -newer one plus its "if -newer matched nothing, copy everything" fallback. The module's
# --since-file keeps that same degradation (a missing/unmatched marker means no lower bound,
# so everything is collected rather than nothing) without a second copy of the loop, and it
# drops the derived .checkpoint. / trajectory.jsonl files the walks were preserving.
sudo PYTHONPATH="$PIPELINE_DIR/scripts" python3 -m core.trajectory collect \
    --agent-home "$HOME:root" \
    --dest "$RESULTS_DIR/sessions" \
    --stage eval \
    --since-file "$WORK_DIR/logs/.eval_start_marker" \
    --stream "$WORK_DIR/logs/eval_trajectory.jsonl" 2>&1 || \
    echo "  WARN: eval transcript collect failed (non-fatal)"
sudo cp "$WORK_DIR/logs/eval_vlm.log" "$RESULTS_DIR/logs/" 2>/dev/null || true

# Lock results dir back to root-only (agent cannot read)
sudo chown -R root:wheel "/var/tmp/pipeline_results/${APP_NAME}" 2>/dev/null || true
sudo chmod -R 700 "/var/tmp/pipeline_results/${APP_NAME}" 2>/dev/null || true

if [ "${VLM_INFRA_FAILURE:-0}" = "1" ]; then
    echo "ERROR: frozen suite contains ${TRUE_VLM_TOTAL} VLM assertion(s), but canonical judging was incomplete"
    exit 1
fi
