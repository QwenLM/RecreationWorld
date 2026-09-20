#!/bin/bash
# macOS release runner. The public lifecycle is exactly recreation -> eval.
#
# The controller places one validated frozen instance at
# .rb_unified_instance.tar.gz. Reference materialization and isolation are
# deterministic prerequisites, not benchmark stages.
set -euo pipefail
trap '' SIGPIPE

readonly RC_OK=0 RC_DATA=1 RC_INFRA=2 RC_TIMEOUT=143

export PATH="$HOME/.local/bin:$HOME/local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PIPELINE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
export SCAFFOLD_DIR="${SCAFFOLD_DIR:-$PIPELINE_DIR/scripts/macos/scaffold}"

APP_NAME="${APP_NAME:-}"
MODEL="${MODEL:-}"
export API_KEY="${ANTHROPIC_API_KEY:-}"
export BASE_URL="${ANTHROPIC_BASE_URL:-}"
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-}"
export VLM_API_KEY="${VLM_API_KEY:-}"
export VLM_BASE_URL="${VLM_BASE_URL:-}"
export VLM_MODEL="${VLM_MODEL:-}"
export EFFORT="${EFFORT:-max}"
export ENABLE_CUA_DRIVER="${ENABLE_CUA_DRIVER:-true}"
export MAX_SCREENSHOT_DIM="${MAX_SCREENSHOT_DIM:-1920}"
export RECREATION_TIMEOUT="${RECREATION_TIMEOUT:-72000}"
export RECREATION_MAX_TURNS="${RECREATION_MAX_TURNS:-}"
export STAGE="${STAGE:-recreation_eval}"
export RB_EVAL_TARGET="${RB_EVAL_TARGET:-recreation}"
export RB_RESTORED_RECREATION_ARCHIVE="${RB_RESTORED_RECREATION_ARCHIVE:-}"
export RB_RESTORE_BUILD_TIMEOUT="${RB_RESTORE_BUILD_TIMEOUT:-3600}"

if [ -z "$APP_NAME" ] || [ -z "$MODEL" ]; then
    echo "ERROR: APP_NAME and MODEL are required"
    exit "$RC_INFRA"
fi
case "$STAGE" in
    recreation|eval|recreation_eval) ;;
    *) echo "ERROR: unsupported release stage: $STAGE"; exit "$RC_INFRA" ;;
esac
case "$RB_EVAL_TARGET" in
    recreation|reference) ;;
    *) echo "ERROR: unsupported eval target: $RB_EVAL_TARGET"; exit 2 ;;
esac
if [ "$RB_EVAL_TARGET" = reference ] && [ "$STAGE" != eval ]; then
    echo "ERROR: RB_EVAL_TARGET=reference requires STAGE=eval"
    exit 2
fi
if [ "$STAGE" != eval ] && { [ -z "$API_KEY" ] || [ -z "$BASE_URL" ]; }; then
    echo "ERROR: ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL are required for recreation"
    exit 2
fi

export WORK_DIR="${WORK_DIR:-$HOME/pipeline_work/$APP_NAME}"
export RECREATION_DIR="${RECREATION_DIR:-$HOME/Recreation/$APP_NAME}"
export RESULTS_MODEL_DIR="${RESULTS_MODEL_DIR:-${MODEL}}"
export SOURCE_DIR="$WORK_DIR/reference/source"
export REFERENCE_APP_DIR="$WORK_DIR/reference/app"
INSTANCE_DIR="$WORK_DIR/instance"
RESULTS_DIR="/var/tmp/pipeline_results/$APP_NAME/$RESULTS_MODEL_DIR"
mkdir -p "$WORK_DIR/logs" "$RESULTS_DIR/logs" "$RESULTS_DIR/sessions"
STAGE_OUTCOMES_FILE="$RESULTS_DIR/stage_outcomes.json"
rm -f "$STAGE_OUTCOMES_FILE"

record_stage_outcome() {
    local stage="$1" status="$2" outcome="$3" reason="$4" native="${5:-}"
    # prepare_recreation deliberately makes the result tree root-only before the
    # untrusted agent starts.  Keep that boundary intact and have this trusted runner
    # update its own outcome ledger through sudo instead of reopening the directory.
    sudo python3 "$PIPELINE_DIR/scripts/macos/runtime.py" record-stage \
        "$STAGE_OUTCOMES_FILE" "$stage" "$status" "$outcome" "$reason" "$native"
}

CURRENT_STAGE=""
on_term() {
    local stage="${CURRENT_STAGE:-${STAGE%%_*}}"
    [ -n "$stage" ] || stage=recreation
    record_stage_outcome "$stage" timeout terminated external_termination "$RC_TIMEOUT" || true
    if [ "$STAGE" = recreation_eval ] && [ "$stage" = recreation ]; then
        record_stage_outcome eval not_run terminated upstream_stage_failed || true
    fi
    exit "$RC_TIMEOUT"
}
trap on_term TERM INT

_TRAJECTORIES_SAVED=false
cleanup_runtime() {
    local rc=$?
    if [ "$_TRAJECTORIES_SAVED" != true ]; then
        sudo PYTHONPATH="$PIPELINE_DIR/scripts" python3 -m core.trajectory collect \
            --agent-home "$HOME:root,/Users/${DEVAGENT_USER:-devagent}:devagent" \
            --dest "$RESULTS_DIR/sessions" --stage emergency 2>/dev/null || true
    fi
    bash "$SCRIPT_DIR/stages/cleanup.sh" >/dev/null 2>&1 || true
    exit "$rc"
}
trap cleanup_runtime EXIT

clean_previous_isolation() {
    sudo pfctl -d 2>/dev/null || true
    sudo pfctl -f /etc/pf.conf 2>/dev/null || true
    if [ -f /etc/hosts.pipeline_backup ]; then
        sudo mv /etc/hosts.pipeline_backup /etc/hosts 2>/dev/null || true
    fi
    if [ -f /usr/local/bin/git.real ]; then
        sudo mv /usr/local/bin/git.real /usr/local/bin/git 2>/dev/null || true
    fi
    for tool in curl wget pip pip3 npm npx yarn brew; do
        if [ -f "/usr/local/bin/$tool" ] && grep -q "BLOCKED:" "/usr/local/bin/$tool" 2>/dev/null; then
            sudo rm -f "/usr/local/bin/$tool"
        fi
        for dir in /usr/local/bin /opt/homebrew/bin; do
            [ -f "$dir/$tool.blocked" ] && sudo mv "$dir/$tool.blocked" "$dir/$tool" 2>/dev/null || true
        done
    done
    # The controller has already started and function-tested the CUA daemon in
    # the GUI launchd domain. Preserve that exact process for recreation; a
    # replacement launched from this SSH stage may not inherit the GUI session.
}

materialize_frozen_instance() {
    local payload="$PIPELINE_DIR/.rb_unified_instance.tar.gz"
    [ -f "$payload" ] || {
        echo "ERROR: frozen unified instance payload is missing: $payload"
        return "$RC_DATA"
    }

    # Archive corruption is deterministic task data. Once it passes validation,
    # failures to materialize it are host/filesystem failures.
    if ! tar tzf "$payload" >/dev/null 2>&1; then
        echo "ERROR: frozen unified instance payload is not a valid gzip tar archive"
        return "$RC_DATA"
    fi
    if ! rm -rf "$INSTANCE_DIR" "$WORK_DIR/tests" "$SOURCE_DIR" "$REFERENCE_APP_DIR"; then
        echo "ERROR: failed to clear the macOS instance workspace"
        return "$RC_INFRA"
    fi
    if ! mkdir -p "$INSTANCE_DIR" "$WORK_DIR/tests" "$WORK_DIR/logs" "$REFERENCE_APP_DIR"; then
        echo "ERROR: failed to create the macOS instance workspace"
        return "$RC_INFRA"
    fi
    if ! tar xzf "$payload" -C "$INSTANCE_DIR"; then
        echo "ERROR: failed to extract the validated frozen instance"
        return "$RC_INFRA"
    fi

    if ! PYTHONPATH="$PIPELINE_DIR/scripts/common" \
        python3 "$PIPELINE_DIR/scripts/macos/runtime.py" check-instance-runtime \
        >/dev/null 2>&1; then
        echo "ERROR: shared frozen-instance validator is unavailable"
        return "$RC_INFRA"
    fi
    if ! PYTHONPATH="$PIPELINE_DIR/scripts/common" \
        python3 "$PIPELINE_DIR/scripts/macos/runtime.py" \
            validate-instance "$INSTANCE_DIR"; then
        return "$RC_DATA"
    fi

    if ! cp -R "$INSTANCE_DIR/tests/." "$WORK_DIR/tests/"; then
        echo "ERROR: failed to copy frozen macOS tests"
        return "$RC_INFRA"
    fi
    if [ -f "$INSTANCE_DIR/vlm_assertions.json" ] && ! \
       python3 "$PIPELINE_DIR/scripts/common/rb_unify/eval_bridge.py" manifest \
           "$INSTANCE_DIR" "$WORK_DIR/tests/test_manifest.json"; then
        echo "ERROR: failed to apply the canonical macOS VLM manifest"
        return "$RC_DATA"
    fi
    [ -f "$WORK_DIR/tests/testcase_counts.json" ] || {
        echo "ERROR: frozen tests lack testcase_counts.json"
        return "$RC_DATA"
    }
    local lanes
    lanes=$(find "$WORK_DIR/tests" -maxdepth 1 -name "test_ax_*.py" | wc -l | tr -d ' ')
    [ "$lanes" -gt 0 ] || {
        echo "ERROR: frozen tests contain no test_ax_*.py lanes"
        return "$RC_DATA"
    }

    if ! python3 "$PIPELINE_DIR/scripts/core/reference_source.py" \
        --descriptor "$INSTANCE_DIR/instance.json" \
        --source-dir "$SOURCE_DIR" \
        --archive "$INSTANCE_DIR/reference/reference.tar.gz"; then
        echo "ERROR: failed to materialize the frozen macOS reference source"
        return "$RC_INFRA"
    fi
    if ! cp "$INSTANCE_DIR/reference/build.sh" "$SOURCE_DIR/build.sh" ||
       ! cp "$INSTANCE_DIR/reference/launch.sh" "$SOURCE_DIR/launch.sh" ||
       ! chmod +x "$SOURCE_DIR/build.sh" "$SOURCE_DIR/launch.sh"; then
        echo "ERROR: failed to install the frozen macOS reference recipes"
        return "$RC_INFRA"
    fi

    echo "==> Preparing frozen reference"
    # A reused environment may retain a root-owned ~/.npm from earlier setup.
    # Give every job a clean, task-local cache instead of mutating or depending
    # on machine-global state.
    mkdir -p "$WORK_DIR/.npm-cache"
    : >"$WORK_DIR/logs/reference_prepare.log"
    local build_ok=0 attempt attempt_log build_rc
    for attempt in 1 2 3; do
        attempt_log="$WORK_DIR/logs/reference_prepare.attempt-${attempt}.log"
        echo "Reference build attempt ${attempt}/3..." | tee -a "$WORK_DIR/logs/reference_prepare.log"
        if (
            cd "$SOURCE_DIR"
            NPM_CONFIG_CACHE="$WORK_DIR/.npm-cache" \
                APP_OUTPUT_DIR="$REFERENCE_APP_DIR" \
                RB_APP_OUTPUT_DIR="$REFERENCE_APP_DIR" \
                bash ./build.sh
        ) >"$attempt_log" 2>&1; then
            cat "$attempt_log" >>"$WORK_DIR/logs/reference_prepare.log"
            build_ok=1
            break
        else
            build_rc=$?
            cat "$attempt_log" >>"$WORK_DIR/logs/reference_prepare.log"
            echo "Reference build attempt ${attempt}/3 failed (exit ${build_rc})" | tee -a "$WORK_DIR/logs/reference_prepare.log"
            tail -40 "$attempt_log" 2>/dev/null || true
            if [ "$attempt" -lt 3 ]; then
                sleep $((attempt * 10))
            fi
        fi
    done
    if [ "$build_ok" != 1 ]; then
        tail -100 "$WORK_DIR/logs/reference_prepare.log" 2>/dev/null || true
        echo "ERROR: frozen reference recipe failed after 3 attempts"
        return "$RC_INFRA"
    fi

    # Every released macOS build recipe honors APP_OUTPUT_DIR. Resolve the app
    # only from that clean staging directory: recursively scanning the source or
    # DerivedData can select an embedded dependency helper (for example
    # Sparkle's sparkle.app / Autoupdate.app) instead of the product under test.
    local official_app="" candidate=""
    local -a official_apps=()
    while IFS= read -r -d '' candidate; do
        official_apps+=("$candidate")
    done < <(find "$REFERENCE_APP_DIR" -mindepth 1 -maxdepth 1 -name "*.app" -type d -print0 2>/dev/null)
    if [ "${#official_apps[@]}" -ne 1 ]; then
        echo "ERROR: frozen reference recipe must publish exactly one top-level .app to $REFERENCE_APP_DIR; found ${#official_apps[@]}"
        find "$REFERENCE_APP_DIR" -mindepth 1 -maxdepth 2 -name "*.app" -type d -print 2>/dev/null || true
        return "$RC_INFRA"
    fi
    official_app="${official_apps[0]}"

    # The validated archive is only a staging source.  Tests, source recipes,
    # and the built product now have canonical homes outside INSTANCE_DIR, so
    # keeping instance/tests would expose a second copy of every fixture to
    # system-wide indexers.  Cardinal, for example, then returned 14 rows for
    # the seven-file frozen corpus.  Remove staging before the reference app is
    # launched so eval observes exactly one fixture tree.
    if ! rm -rf "$INSTANCE_DIR"; then
        echo "ERROR: failed to remove the materialized macOS instance staging directory"
        return "$RC_INFRA"
    fi

    (
        cd "$SOURCE_DIR"
        APP_OUTPUT_DIR="$REFERENCE_APP_DIR" \
            RB_APP_OUTPUT_DIR="$REFERENCE_APP_DIR" \
            bash "$SCRIPT_DIR/tools/run_launch.sh" ./launch.sh
    ) >>"$WORK_DIR/logs/reference_prepare.log" 2>&1 &
    sleep 5

    local app_binary="$APP_NAME"
    local bundle_id=""
    if [ -n "$official_app" ] && [ -d "$official_app" ]; then
        app_binary=$(defaults read "$official_app/Contents/Info.plist" CFBundleExecutable 2>/dev/null || basename "$official_app" .app)
        bundle_id=$(defaults read "$official_app/Contents/Info.plist" CFBundleIdentifier 2>/dev/null || true)
    fi
    if ! {
        printf 'OFFICIAL_APP="%s"\n' "${official_app//\"/\\\"}"
        printf 'APP_BINARY="%s"\n' "${app_binary//\"/\\\"}"
        printf 'BUNDLE_ID="%s"\n' "${bundle_id//\"/\\\"}"
        printf 'SOURCE_DIR="%s"\n' "${SOURCE_DIR//\"/\\\"}"
    } > "$WORK_DIR/reference_result.env"; then
        echo "ERROR: failed to record the frozen macOS reference result"
        return "$RC_INFRA"
    fi
    echo "==> Frozen instance ready: $lanes AX lanes"
}

save_stage_results() {
    local stage="$1"
    sudo mkdir -p "$RESULTS_DIR/logs" "$RESULTS_DIR/sessions" "$RESULTS_DIR/source" 2>/dev/null || true
    sudo cp "$WORK_DIR/logs/$stage.log" "$RESULTS_DIR/logs/" 2>/dev/null || true
    sudo cp "$WORK_DIR/logs/${stage}_agent.log" "$RESULTS_DIR/logs/" 2>/dev/null || true
    sudo cp "$WORK_DIR/${stage}_result.env" "$RESULTS_DIR/logs/" 2>/dev/null || true
    # The controller downloads RESULTS_DIR, not WORK_DIR. Preserve the
    # recreation screenshots alongside the other stage diagnostics before
    # eval starts, so they also survive a later evaluation failure.
    if [ "$stage" = recreation ] && [ -d "$WORK_DIR/logs/tool_use_screenshots" ]; then
        sudo mkdir -p "$RESULTS_DIR/logs/tool_use_screenshots" 2>/dev/null || true
        sudo cp -R "$WORK_DIR/logs/tool_use_screenshots/." \
            "$RESULTS_DIR/logs/tool_use_screenshots/" 2>/dev/null || true
    fi
    local homes="$HOME:root"
    [ "$stage" = recreation ] && homes="$homes,/Users/${DEVAGENT_USER:-devagent}:devagent"
    sudo PYTHONPATH="$PIPELINE_DIR/scripts" python3 -m core.trajectory collect \
        --agent-home "$homes" --dest "$RESULTS_DIR/sessions" \
        --since-file "$WORK_DIR/logs/.${stage}_start_marker" --stage "$stage" \
        --stream "$WORK_DIR/logs/${stage}_trajectory.jsonl" 2>/dev/null || true
    _TRAJECTORIES_SAVED=true
}

run_permission_setup() {
    local permission_work="$WORK_DIR/.permission_setup"
    local hidden_dir="" official_app="" rc=0
    local -a protected=() cli=()

    hidden_dir="$(sudo cat /var/tmp/.pipeline_hidden_path 2>/dev/null || true)"
    if [ -f "$WORK_DIR/prepare_result.env" ]; then
        # Trusted output of prepare_recreation.sh; source only to recover the actual bundle path.
        # shellcheck disable=SC1090
        source "$WORK_DIR/prepare_result.env"
        official_app="${OFFICIAL_APP:-}"
    fi

    for path in "$SOURCE_DIR" "$INSTANCE_DIR" "$hidden_dir"; do
        [ -n "$path" ] && [ -e "$path" ] || continue
        protected+=(--protected "$path")
    done
    case "$official_app" in
        ""|"$SOURCE_DIR"|"$SOURCE_DIR"/*) ;;
        *) [ ! -e "$official_app" ] || protected+=(--protected "$official_app") ;;
    esac
    [ "${#protected[@]}" -gt 0 ] || {
        echo "ERROR: unified permission setup found no protected macOS paths"
        return "$RC_INFRA"
    }

    mkdir -p "$permission_work"
    cli=(
        python3 "$PIPELINE_DIR/scripts/core/permission_probe.py"
        --platform macos
        --run-id "$APP_NAME"
        --user "${DEVAGENT_USER:-devagent}"
        "${protected[@]}"
        --writable "/Users/Shared/workspace/recreation"
        --writable-mode 0755
        --spec-out "$permission_work/spec.json"
        --report "$permission_work/report.json"
        --env-out "$permission_work/permission_env.sh"
    )
    sudo -E env PYTHONPATH="$PIPELINE_DIR/scripts" "${cli[@]}" || rc=$?
    if sudo test -f "$permission_work/report.json"; then
        sudo cp "$permission_work/report.json" "$RESULTS_DIR/permission_report.json"
        sudo cp "$permission_work/report.json" "$WORK_DIR/permission_report.json"
    fi
    [ "$rc" -eq 0 ] || {
        echo "ERROR: unified permission setup did not attest; refusing to launch the agent"
        return "$RC_INFRA"
    }
    # ── macOS-only: reference must stay readable by its OWN running process ──
    # The shared permission engine locks every --protected path root:0700. That is right for
    # inert inputs (INSTANCE_DIR, hidden tests), but the reference bundle hosts a LIVE process
    # running as MACOS_USER; root:0700 makes it EACCES its own bundle, so a menu-bar app
    # (LSUIElement) SIGILLs when opening its Settings window (SwiftUI .ultraThinMaterial ->
    # CoreImage/CGImageProvider lazy read of the bundle) -> reference dies -> agent cannot observe
    # Settings -> low AX. Re-own ONLY the reference paths to MACOS_USER, keeping mode 0700: the
    # still-running reference regains read access while the devagent (different uid, no group/other
    # bits) stays denied, so the attested isolation is preserved. Does NOT touch the shared engine
    # or any other platform.
    local _ref_user="${MACOS_USER:-$(whoami)}"
    local -a _ref_paths=("$SOURCE_DIR")
    local -a _ref_probes=("$SOURCE_DIR/launch.sh")
    if [ -n "$official_app" ]; then
        _ref_paths+=("$official_app")
        _ref_probes+=("$official_app/Contents/Info.plist")
    fi
    local _refp _probe _i
    for (( _i=0; _i<${#_ref_paths[@]}; _i++ )); do
        _refp="${_ref_paths[$_i]}"
        _probe="${_ref_probes[$_i]}"
        if ! sudo test -e "$_refp" 2>/dev/null || ! sudo test -r "$_probe" 2>/dev/null; then
            echo "  [ref-own] ERROR: reference path or probe is missing: $_refp ($_probe)"
            return "$RC_INFRA"
        fi
        if ! sudo chown -R "$_ref_user:staff" "$_refp" 2>/dev/null; then
            echo "  [ref-own] ERROR: could not return reference ownership to $_ref_user: $_refp"
            return "$RC_INFRA"
        fi
        if ! sudo -u "$_ref_user" test -r "$_probe" 2>/dev/null; then
            echo "  [ref-own] ERROR: $_ref_user cannot read reference after re-own: $_probe"
            return "$RC_INFRA"
        fi
        if sudo -u "${DEVAGENT_USER:-devagent}" test -r "$_probe" 2>/dev/null; then
            echo "  [ref-own] ERROR: devagent CAN read reference after re-own -- anti-cheat broken: $_probe"
            return "$RC_INFRA"
        fi
        echo "  [ref-own] $_probe readable by $_ref_user; devagent still denied"
    done
    echo "==> Unified permission setup attested"
}

run_stage() {
    local stage="$1" script="$2" start rc duration log
    start=$(date +%s)
    log="$WORK_DIR/logs/$stage.log"
    touch "$WORK_DIR/logs/.${stage}_start_marker" "$log"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Running: $stage"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    tail -f -n +1 "$log" 2>/dev/null &
    local tail_pid=$!
    rc=0
    bash "$script" >>"$log" 2>&1 || rc=$?
    duration=$(( $(date +%s) - start ))
    if [ "$rc" -eq 0 ]; then
        echo "$stage completed in ${duration}s" >>"$log"
    else
        echo "$stage failed after ${duration}s (exit $rc)" >>"$log"
    fi
    sleep 0.2
    kill "$tail_pid" 2>/dev/null || true
    wait "$tail_pid" 2>/dev/null || true
    save_stage_results "$stage"
    return "$rc"
}

restore_recreation_for_eval() {
    local archive="$RB_RESTORED_RECREATION_ARCHIVE"
    local restore_log="$WORK_DIR/logs/recreation_restore.log"
    local build_rc=0 recreated_app="" launch_script=""
    [ -n "$archive" ] && [ -s "$archive" ] || {
        echo "ERROR: eval-only candidate archive is missing: ${archive:-<unset>}"
        return "$RC_DATA"
    }
    if ! tar tzf "$archive" >/dev/null 2>&1; then
        echo "ERROR: eval-only candidate archive is invalid: $archive"
        return "$RC_DATA"
    fi

    rm -rf "$RECREATION_DIR"
    mkdir -p "$RECREATION_DIR"
    if ! tar xzf "$archive" -C "$RECREATION_DIR"; then
        echo "ERROR: failed to extract restored recreation"
        return "$RC_INFRA"
    fi
    chmod +x "$RECREATION_DIR/build.sh" "$RECREATION_DIR/launch.sh" \
        "$RECREATION_DIR/src/launch.sh" 2>/dev/null || true
    if [ ! -f "$RECREATION_DIR/build.sh" ] && \
       [ ! -f "$RECREATION_DIR/launch.sh" ] && \
       [ ! -f "$RECREATION_DIR/src/launch.sh" ]; then
        echo "ERROR: restored recreation contains neither build.sh nor launch.sh"
        return "$RC_DATA"
    fi

    : >"$restore_log"
    echo "==> Restored candidate source for eval-only run" | tee -a "$restore_log"
    if [ -f "$RECREATION_DIR/build.sh" ]; then
        # A recreation archive can contain SwiftPM's .build directory from the
        # original target. Its compiled modules and PCH files embed that target's
        # absolute workspace path, so reusing them from the eval-only path can
        # fail with "PCH was compiled with module cache path ...".  Keep the
        # downloaded dependency material needed by the offline rebuild, but
        # discard every other (path-bound) SwiftPM build artifact.
        while IFS= read -r -d '' swift_build_dir; do
            echo "==> Cleaning restored SwiftPM build state: $swift_build_dir" \
                | tee -a "$restore_log"
            find "$swift_build_dir" -mindepth 1 -maxdepth 1 \
                ! -name artifacts \
                ! -name checkouts \
                ! -name repositories \
                ! -name workspace-state.json \
                -exec rm -rf {} +
        done < <(find "$RECREATION_DIR" -type d -name .build -print0)

        # Reproduce the original release boundary: rebuilding the candidate is
        # offline and cannot repair model output by fetching new dependencies.
        VLM_BASE_URL="${VLM_BASE_URL:-}" BASE_URL="${BASE_URL:-}" \
          MODEL_API_ENDPOINTS="${MODEL_API_ENDPOINTS:-}" \
          DEVAGENT_USER="${DEVAGENT_USER:-devagent}" WORK_DIR="$WORK_DIR" \
          BLOCK_HARNESS_NET=true bash "$SCRIPT_DIR/stages/restrict_network.sh" \
          >>"$restore_log" 2>&1 || {
            echo "ERROR: could not apply offline network restriction before restored build"
            return "$RC_INFRA"
          }
        python3 "$PIPELINE_DIR/scripts/macos/runtime.py" restore-build \
            "$RECREATION_DIR" "$restore_log" "$RB_RESTORE_BUILD_TIMEOUT" \
            || build_rc=$?
        if [ "$build_rc" -ne 0 ]; then
            tail -100 "$restore_log" 2>/dev/null || true
            echo "ERROR: restored candidate build failed (exit $build_rc)"
            return "$RC_INFRA"
        fi
    fi

    for search_dir in "$RECREATION_DIR/build" "$RECREATION_DIR" \
        "$HOME/Library/Developer/Xcode/DerivedData"; do
        [ -d "$search_dir" ] || continue
        recreated_app=$(find "$search_dir" -name "*.app" -type d 2>/dev/null | head -1 || true)
        [ -z "$recreated_app" ] || break
    done
    for candidate in "$RECREATION_DIR/launch.sh" "$RECREATION_DIR/src/launch.sh"; do
        [ -f "$candidate" ] && launch_script="$candidate" && break
    done
    if [ -z "$recreated_app" ] && [ -z "$launch_script" ]; then
        echo "ERROR: restored candidate build produced no .app or launch.sh"
        return "$RC_INFRA"
    fi
    {
        printf 'RECREATED_APP="%s"\n' "${recreated_app//\"/\\\"}"
        printf 'RECREATION_DIR="%s"\n' "${RECREATION_DIR//\"/\\\"}"
        [ ! -f "$RECREATION_DIR/build.sh" ] || \
            printf 'REC_BUILD_SH="%s"\n' "${RECREATION_DIR//\"/\\\"}/build.sh"
        [ -z "$launch_script" ] || \
            printf 'REC_LAUNCH_SH="%s"\n' "${launch_script//\"/\\\"}"
    } >"$WORK_DIR/recreation_result.env"
    sudo cp "$restore_log" "$RESULTS_DIR/logs/" 2>/dev/null || true
    sudo cp "$WORK_DIR/recreation_result.env" "$RESULTS_DIR/logs/" 2>/dev/null || true
    record_stage_outcome recreation pass completed restored_prerequisite "$RC_OK"
    echo "==> Eval-only candidate restored: ${recreated_app:-$launch_script}"
}

clean_previous_isolation
recreation_rc=0

if [ "$STAGE" != eval ] || [ "$RB_EVAL_TARGET" = reference ] || \
   [ -n "$RB_RESTORED_RECREATION_ARCHIVE" ]; then
    materialize_rc=0
    materialize_frozen_instance || materialize_rc=$?
    if [ "$materialize_rc" -ne 0 ]; then
        if [ "$STAGE" = eval ]; then
            if [ "$materialize_rc" -eq "$RC_DATA" ]; then
                record_stage_outcome eval fail data_error invalid_frozen_input "$materialize_rc"
                exit "$RC_DATA"
            fi
            record_stage_outcome eval error infra_error frozen_instance_materialization_failed "$materialize_rc"
            exit "$RC_INFRA"
        fi
        if [ "$materialize_rc" -eq "$RC_DATA" ]; then
            record_stage_outcome recreation fail data_error invalid_frozen_input "$materialize_rc"
            if [ "$STAGE" = recreation_eval ]; then
                record_stage_outcome eval not_run data_error upstream_stage_failed
            fi
            exit "$RC_DATA"
        fi
        record_stage_outcome recreation error infra_error frozen_instance_materialization_failed "$materialize_rc"
        if [ "$STAGE" = recreation_eval ]; then
            record_stage_outcome eval not_run infra_error upstream_stage_failed
        fi
        exit "$RC_INFRA"
    fi

fi

if [ "$STAGE" != eval ]; then

    # Isolation is a deterministic prerequisite of recreation, not a public stage.
    bash "$SCRIPT_DIR/stages/prepare_recreation.sh" >"$WORK_DIR/logs/prepare.log" 2>&1 || {
        tail -100 "$WORK_DIR/logs/prepare.log" 2>/dev/null || true
        sudo cp "$WORK_DIR/logs/prepare.log" "$RESULTS_DIR/logs/" 2>/dev/null || true
        echo "ERROR: recreation preparation failed"
        record_stage_outcome recreation error infra_error recreation_prepare_failed "$RC_INFRA"
        if [ "$STAGE" = recreation_eval ]; then
            record_stage_outcome eval not_run infra_error upstream_stage_failed
        fi
        exit "$RC_INFRA"
    }

    # Preparation must consume/build/launch the frozen reference first.  The shared boundary is
    # then applied at the last trusted point before recreation and fails closed.
    run_permission_setup >"$WORK_DIR/logs/permission_setup.log" 2>&1 || {
        tail -100 "$WORK_DIR/logs/permission_setup.log" 2>/dev/null || true
        record_stage_outcome recreation error infra_error permission_setup_failed "$RC_INFRA"
        if [ "$STAGE" = recreation_eval ]; then
            record_stage_outcome eval not_run infra_error upstream_stage_failed
        fi
        exit "$RC_INFRA"
    }

    export MAX_TURNS="$RECREATION_MAX_TURNS"
    export TIMEOUT="$RECREATION_TIMEOUT"
    CURRENT_STAGE=recreation
    run_stage recreation "$SCRIPT_DIR/stages/recreation.sh" || recreation_rc=$?
    case "$recreation_rc" in
        "$RC_OK")      record_stage_outcome recreation pass completed recreation_completed "$RC_OK" ;;
        "$RC_DATA")    record_stage_outcome recreation fail completed no_usable_artifact "$RC_DATA" ;;
        "$RC_TIMEOUT") record_stage_outcome recreation timeout terminated external_termination "$RC_TIMEOUT" ;;
        *)   record_stage_outcome recreation error infra_error recreation_runner_failed "$recreation_rc" ;;
    esac
    if [ "$STAGE" = recreation ]; then
        case "$recreation_rc" in
            "$RC_OK"|"$RC_DATA") exit "$RC_OK" ;;
            "$RC_TIMEOUT") exit "$RC_TIMEOUT" ;;
            *) exit "$RC_INFRA" ;;
        esac
    fi
    if [ "$recreation_rc" -ne 0 ]; then
        case "$recreation_rc" in
            "$RC_DATA")    record_stage_outcome eval not_run completed upstream_stage_failed; exit "$RC_OK" ;;
            "$RC_TIMEOUT") record_stage_outcome eval not_run terminated upstream_stage_failed; exit "$RC_TIMEOUT" ;;
            *)             record_stage_outcome eval not_run infra_error upstream_stage_failed; exit "$RC_INFRA" ;;
        esac
    fi
else
    if [ "$RB_EVAL_TARGET" = reference ]; then
        source "$WORK_DIR/reference_result.env"
        export RECREATED_APP="$OFFICIAL_APP"
        export RECREATION_DIR="$SOURCE_DIR"
        export APP_OUTPUT_DIR="$REFERENCE_APP_DIR"
        export RB_APP_OUTPUT_DIR="$REFERENCE_APP_DIR"
        echo "==> Eval target: frozen reference ($RECREATED_APP)"
    else
        if [ -n "$RB_RESTORED_RECREATION_ARCHIVE" ]; then
            restore_rc=0
            restore_recreation_for_eval || restore_rc=$?
            if [ "$restore_rc" -ne 0 ]; then
                case "$restore_rc" in
                    "$RC_DATA") record_stage_outcome eval fail data_error candidate_recreation_missing "$restore_rc" ;;
                    *) record_stage_outcome eval error infra_error candidate_recreation_restore_failed "$restore_rc" ;;
                esac
                exit "$restore_rc"
            fi
        fi
        [ -f "$WORK_DIR/recreation_result.env" ] || {
            echo "ERROR: eval-only requires an existing recreation_result.env"
            record_stage_outcome eval fail data_error candidate_recreation_missing "$RC_DATA"
            exit "$RC_DATA"
        }
    fi
fi

eval_rc=0
CURRENT_STAGE=eval
run_stage eval "$SCRIPT_DIR/stages/ax_eval.sh" || eval_rc=$?
eval_reason=eval_runner_failed
if [ "$eval_rc" -ne 0 ] && [ -s "$RESULTS_DIR/vlm_results.json" ]; then
    if python3 "$PIPELINE_DIR/scripts/macos/runtime.py" has-vlm-errors \
        "$RESULTS_DIR/vlm_results.json" >/dev/null 2>&1
    then
        eval_reason=eval_vlm_failed
    fi
fi
case "$eval_rc" in
    "$RC_OK")      record_stage_outcome eval pass completed eval_completed "$RC_OK" ;;
    "$RC_TIMEOUT") record_stage_outcome eval timeout terminated external_termination "$RC_TIMEOUT" ;;
    *)   record_stage_outcome eval error infra_error "$eval_reason" "$eval_rc" ;;
esac
echo "Pipeline complete: recreation -> eval"
case "$eval_rc" in
    "$RC_OK") exit "$RC_OK" ;;
    "$RC_TIMEOUT") exit "$RC_TIMEOUT" ;;
    *) exit "$RC_INFRA" ;;
esac
