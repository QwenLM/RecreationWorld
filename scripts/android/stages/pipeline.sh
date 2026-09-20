#!/usr/bin/env bash
# Android benchmark stages and helpers, sourced by the platform worker.
#
# Sourced rather than executed: functions use worker globals such as APP_DIR,
# DEVICE_ID, STAGE_LIST, and RC_*, plus the worker's artifact and status helpers.
#
# Do not add `set -euo pipefail` here: the adapter owns shell options, and flipping them
# mid-source would change error handling for the whole job.

_RB_ANDROID_STAGES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bootstrap.sh
. "$_RB_ANDROID_STAGES_DIR/bootstrap.sh"
unset _RB_ANDROID_STAGES_DIR

setup_codex() {
    local want="${SCAFFOLD_VERSION:-0.145.0}" have=""
    have="$(codex --version 2>/dev/null | awk '{print $NF}' || true)"
    if [ "$have" != "$want" ]; then
        echo "FATAL: bundled codex CLI version ${have:-missing}; expected ${want}" >&2
        return 1
    fi
    have="$(codex --version 2>/dev/null | awk '{print $NF}' || true)"
    [ "$have" = "$want" ] || {
        echo "FATAL: codex CLI version ${have:-missing}; expected ${want}" >&2
        return 1
    }

    local _rb_home="${RB_AGENT_HOME:-${HOME:-/root}}"
    mkdir -p "$_rb_home/.codex"

    # Config from the ONE shared builder, with the BARE slug via the ONE shared rule -- the
    # dot-form in codex's own config logs "Model metadata not found" and degrades the context
    # window, and windows' first codex run 400'd on exactly that.
    #
    # RECREATION_MODEL (upstream id), NOT RECREATION_CLAUDE_MODEL: this template exports both,
    # and the latter carries the `[1m]` suffix Claude Code parses its context window from. codex
    # has no such convention and nothing rewrites the name on the direct Responses route, so
    # an upstream gateway can reject "gpt-5.6-sol[1m]" as an unknown model with an
    # auth-shaped error. Linux/Windows append `[1m]` only inside
    # the generated VM script, in the ANTHROPIC_* launch path only. (codex_model_slug strips the
    # suffix too, so this is belt-and-braces; the variable name is the part that carries intent.)
    local rb_scripts endpoint_url
    rb_scripts="$(rb_scripts_dir || true)"
    if [ -z "$rb_scripts" ]; then
        echo "FATAL: rb core unavailable; cannot build codex config" >&2
        return 1
    fi
    # Codex learns MCP servers ONLY from config.toml -- there is no --mcp-config flag. Fail
    # closed if the mobile-mcp definition is absent: a text-only Android agent can run for hours
    # and produce a plausible but unobserved clone.
    if [ -z "${MCP_CONFIG_PATH:-}" ] || [ ! -s "$MCP_CONFIG_PATH" ]; then
        echo "FATAL: mobile-mcp config missing: ${MCP_CONFIG_PATH:-unset}" >&2
        return 1
    fi
    endpoint_url="${RB_AGENT_BASE_URL:?deployment adapter must provide RB_AGENT_BASE_URL}"
    if ! PYTHONPATH="$rb_scripts" python3 -m core.agent_config codex \
        --model "$RECREATION_MODEL" \
        --base-url "$endpoint_url" \
        --provider "${RB_AGENT_PROVIDER:-recreationbench}" \
        --name "${RB_AGENT_PROVIDER_NAME:-RecreationBench model endpoint}" \
        --wire-api "${RB_AGENT_WIRE_API:-responses}" \
        --supports-websockets "${RB_AGENT_SUPPORTS_WEBSOCKETS:-auto}" \
        --credential-env "${RB_AGENT_CREDENTIAL_ENV:-OPENAI_API_KEY}" \
        --request-max-retries "${RB_AGENT_REQUEST_MAX_RETRIES:-10}" \
        --stream-max-retries "${RB_AGENT_STREAM_MAX_RETRIES:-10}" \
        --reasoning-effort "${THINKING_EFFORT:-}" \
        --mcp-config "$MCP_CONFIG_PATH" \
        --mcp-server mobile-mcp \
        --mcp-startup-timeout-sec 60 \
        > "$_rb_home/.codex/config.toml"
    then
        echo "FATAL: could not build Codex config" >&2
        return 1
    fi
    grep -qF "[mcp_servers.mobile-mcp]" "$_rb_home/.codex/config.toml" || {
        echo "FATAL: Codex config does not contain mobile-mcp" >&2
        return 1
    }
    [ -z "${RB_AGENT_USER:-}" ] || chown -R "$RB_AGENT_USER:$RB_AGENT_USER" "$_rb_home/.codex"
    echo "==> codex configured: ${endpoint_url}, config at $_rb_home/.codex/config.toml"
    return 0
}
_b64_filter() {
    local rb_scripts
    rb_scripts="$(rb_scripts_dir)" || return 1
    python3 -u "$rb_scripts/android/lib/runtime_helpers.py" filter-base64
}
write_claude_settings() {
    local target="${1:-${RB_AGENT_HOME:-${HOME:-/root}}/.claude/settings.json}"
    local rb_scripts tmp
    rb_scripts="$(rb_scripts_dir || true)"
    if [ -z "$rb_scripts" ]; then
        echo "FATAL: rb core unavailable; cannot build Claude settings" >&2
        return 1
    fi
    mkdir -p "$(dirname "$target")"
    tmp="${target}.tmp.$$"
    if ! PYTHONPATH="$rb_scripts" python3 -m core.agent_config settings \
            --mcp-server mobile-mcp \
            --deny mcp__mobile-mcp__mobile_list_elements_on_screen > "$tmp"; then
        rm -f "$tmp"
        echo "FATAL: shared Claude settings builder failed" >&2
        return 1
    fi
    mv "$tmp" "$target"
    [ -z "${RB_AGENT_USER:-}" ] || chown -R "$RB_AGENT_USER:$RB_AGENT_USER" "$(dirname "$target")"
}
collect_claude_session() {
    local dest_dir="$1"
    local since_ts="${2:-0}"
    local stage="${3:-recreation}"
    local stream_path="${4:-$REC_DIR/trajectory.jsonl}"
    local base="${CLAUDE_CONFIG_DIR:-${RB_AGENT_HOME:-${HOME:-/root}}/.claude}/projects"
    mkdir -p "$dest_dir"
    # ONE shared collector (core/trajectory.py) — the same code linux, web and macOS use.
    # It applies the same filters this function used (drop Claude Code's .checkpoint. /
    # .trajectory.jsonl derived files, gate on mtime) and adds the main(UUID)-vs-subagent
    # (agent-*) split the flat copy did not have. Names become
    # {stage}_{user}_{main|sub}_{basename}; nothing parses them (the analyzers read the
    # session id from INSIDE the jsonl, and behavior_cohort globs *.jsonl).
    local rb_common
    rb_common="$(ensure_rb_core || true)"
    if [ -n "${rb_common}" ]; then
        # BOTH agent CLIs. codex keeps its transcript ("rollout") under $CODEX_HOME/sessions,
        # not ~/.claude/projects, so collecting only the Claude dir shipped zero codex records.
        # --projects rather than the module's --agent-home shorthand because CLAUDE_CONFIG_DIR can
        # move the Claude dir off HOME, which a HOME-derived shorthand cannot express.
        local _codex_sessions="${CODEX_HOME:-${RB_AGENT_HOME:-${HOME:-/root}}/.codex}/sessions"
        PYTHONPATH="$(dirname "${rb_common}")" python3 -m core.trajectory collect \
            --projects "${base}:pod,${_codex_sessions}:pod" --dest "$dest_dir" \
            --since "${since_ts}" --stage "${stage}" \
            --stream "$stream_path" && return 0
        echo "WARN: shared collector failed; falling back to the local copy"
    fi
    # Fallback for a run without RB_PIPELINE_COMMIT (no rb artifact to fetch): losing the
    # transcript entirely would be worse than keeping this copy.
    #
    # These two globs must stay equivalent to core.trajectory.EXCLUDE_SUBSTRINGS, which excludes
    # by SUBSTRING. `*.trajectory.jsonl` was not equivalent -- it needs a literal dot, so
    # `stage4_trajectory.jsonl` (a name this repo does use) and `trajectory.jsonl.bak` were
    # dropped by the module and copied here. `*trajectory.jsonl*` matches the substring.
    # Kept in sync with the shared trajectory contract; the module's own comment
    # notes hand-listing these is what broke it twice.
    local n=0 f src label kind bn
    local _codex_sessions="${CODEX_HOME:-${RB_AGENT_HOME:-${HOME:-/root}}/.codex}/sessions"
    for src in "$base" "$_codex_sessions"; do
        [ -d "$src" ] || continue
        label=pod
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            bn="$(basename "$f")"
            kind=main
            case "$bn" in agent-*) kind=sub ;; esac
            cp -f "$f" "$dest_dir/${stage}_${label}_${kind}_${bn}" 2>/dev/null && n=$((n+1))
        done < <(find "$src" -type f -name '*.jsonl' \
                    ! -name '*.checkpoint.*' ! -name '*trajectory.jsonl*' \
                    -newermt "@${since_ts}" 2>/dev/null)
    done
    [ ! -s "$stream_path" ] || \
        cp -f "$stream_path" "$dest_dir/${stage}_stream.jsonl" 2>/dev/null || true
    echo "==> Collected $n agent session jsonl -> $dest_dir (local fallback)"
}
resolve_aapt() {
    if [ -n "${_AAPT_BIN:-}" ]; then
        return 0
    fi
    if command -v aapt >/dev/null 2>&1; then
        _AAPT_BIN="$(command -v aapt)"; export _AAPT_BIN; return 0
    fi
    local sdk cand
    for sdk in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" /opt/android-sdk /opt/android; do
        [ -n "$sdk" ] && [ -d "$sdk/build-tools" ] || continue
        cand="$(find "$sdk/build-tools" -maxdepth 2 -name aapt -type f 2>/dev/null | sort -r | head -1)"
        if [ -n "$cand" ]; then
            _AAPT_BIN="$cand"; export _AAPT_BIN; return 0
        fi
    done
    if command -v aapt2 >/dev/null 2>&1; then
        _AAPT_BIN="$(command -v aapt2)"; export _AAPT_BIN; return 0
    fi
    return 1
}
explain_cc_exit() {
    local label="$1" traj="$2" rc="$3" secs="$4" limit="$5"
    if [ "$rc" = "124" ]; then
        echo "==> [$label] Claude Code KILLED by wall-clock timeout: ran ${secs}s, limit=${limit}s (rc=124)"
    elif [ "$rc" != "0" ]; then
        echo "==> [$label] Claude Code exited non-zero: rc=${rc}, ran ${secs}s of ${limit}s limit"
    else
        echo "==> [$label] Claude Code exited rc=0, ran ${secs}s of ${limit}s limit"
    fi
    [ -s "$traj" ] || { echo "    (no trajectory to explain the stop)"; return 0; }
    local _rb_scripts
    _rb_scripts="$(rb_scripts_dir || true)"
    if [ -n "${_rb_scripts}" ]; then
        PYTHONPATH="${_rb_scripts}" python3 -m core.trajectory explain --trajectory "$traj" || true
    else
        echo "    (rb core unavailable; cannot explain the stop)"
    fi
}
apk_package_name() {
    local apk="$1"
    resolve_aapt || { echo "ERROR: aapt not found (checked PATH and ANDROID_HOME/ANDROID_SDK_ROOT build-tools)" >&2; return "$RC_INFRA"; }
    "$_AAPT_BIN" dump badging "$apk" 2>/dev/null | sed -n "s/.*package: name='\([^']*\)'.*/\1/p" | head -1
}
diagnose_bad_apk() {
    local apk="$1"
    echo "---- APK diagnostics: $apk ----" >&2
    ls -l "$apk" 2>&1 | sed 's/^/  /' >&2
    file "$apk" 2>/dev/null | sed 's/^/  /' >&2 || true
    echo "  head(file magic):" >&2
    head -c 16 "$apk" 2>/dev/null | od -An -tx1 2>/dev/null | sed 's/^/    /' >&2 || true
    if resolve_aapt; then
        echo "  aapt=$_AAPT_BIN; raw badging error:" >&2
        "$_AAPT_BIN" dump badging "$apk" 2>&1 | head -5 | sed 's/^/    /' >&2 || true
    else
        echo "  aapt: NOT FOUND" >&2
    fi
    echo "--------------------------------" >&2
}
render_cc_prompt() {
    local stage="$1" _rb_scripts
    if [ "$stage" != "recreation" ]; then
        echo "ERROR: unsupported prompt stage: $stage" >&2
        return 1
    fi
    _rb_scripts="$(rb_scripts_dir || true)"
    if [ -z "$_rb_scripts" ]; then
        echo "ERROR: rb core unavailable; cannot render the recreation prompt" >&2
        return 1
    fi
    PYTHONPATH="$_rb_scripts" python3 \
        "$_rb_scripts/android/lib/runtime_helpers.py" render-prompt \
        --stage "$stage" --device-id "$DEVICE_ID"
}
install_runtime_assets() {
    # deployment adapter calls this compatibility hook before dispatching any stage.  The evaluator must
    # not be copied there: APP_DIR is readable by the recreation principal.  Validate the pinned
    # source now and remove evaluator files left by a reused workspace; stage_eval installs a
    # fresh authoritative copy only after the recreation agent has exited.
    local pipeline_file="${BASH_SOURCE[0]}"
    local payload_dir
    local shared_vlm_judge
    payload_dir="$(cd "$(dirname "$pipeline_file")/.." && pwd)/evaluation"
    shared_vlm_judge="$(cd "$(dirname "$pipeline_file")/../.." && pwd)/common/vlm_judge.py"

    if [ ! -d "$payload_dir" ]; then
        echo "ERROR: Android runtime assets are missing from the pinned RB artifact: $payload_dir" >&2
        return 1
    fi
    local missing=""
    local f
    for f in \
        test_verify.sh runtime.py normalize_frozen_vlm.py \
        shared_vlm_shim.py.inc reference_vlm_batch.py; do
        [ -s "${payload_dir}/${f}" ] || missing="${missing} ${f}"
    done
    [ -s "$shared_vlm_judge" ] || missing="${missing} vlm_judge.py"
    if [ -n "$missing" ]; then
        echo "ERROR: pinned Android eval payload is incomplete; missing:${missing}" >&2
        echo "       source: ${payload_dir}"
        return 1
    fi

    if [ -z "${APP_DIR:-}" ] || [ "$APP_DIR" = "/" ]; then
        echo "ERROR: unsafe APP_DIR for Android eval assets: ${APP_DIR:-<empty>}" >&2
        return 1
    fi
    if [ -d "$APP_DIR" ]; then
        rm -f -- \
            "$APP_DIR/test_verify.sh" \
            "$APP_DIR/runtime.py" \
            "$APP_DIR/normalize_frozen_vlm.py" \
            "$APP_DIR/shared_vlm_shim.py.inc" \
            "$APP_DIR/reference_vlm_batch.py" \
            "$APP_DIR/vlm_judge.py" \
            "$APP_DIR/new_vlm_verify.sh" || {
            echo "ERROR: could not clear stale Android eval assets from $APP_DIR" >&2
            return 1
        }
    fi

    echo "==> Android eval assets validated; installation deferred until eval"
    return 0
}
prepare_eval_runtime_assets() {
    # This is deliberately called only from stage_eval, after the untrusted recreation process
    # has exited.  Re-copying every time makes eval-only/resumed execution idempotent and prevents
    # an agent-authored verifier from becoming authoritative.
    local pipeline_file="${BASH_SOURCE[0]}"
    local payload_dir
    local shared_vlm_judge
    payload_dir="$(cd "$(dirname "$pipeline_file")/.." && pwd)/evaluation"
    shared_vlm_judge="$(cd "$(dirname "$pipeline_file")/../.." && pwd)/common/vlm_judge.py"

    install_runtime_assets || return 1
    mkdir -p "$APP_DIR" || {
        echo "ERROR: could not create Android eval asset directory: $APP_DIR" >&2
        return 1
    }
    cp -a "$payload_dir/." "$APP_DIR/" || {
        echo "ERROR: could not install Android eval assets from $payload_dir" >&2
        return 1
    }
    cp "$shared_vlm_judge" "$APP_DIR/vlm_judge.py" || {
        echo "ERROR: could not install shared VLM judge from $shared_vlm_judge" >&2
        return 1
    }

    local missing=""
    local f
    for f in \
        test_verify.sh runtime.py normalize_frozen_vlm.py \
        shared_vlm_shim.py.inc reference_vlm_batch.py vlm_judge.py; do
        [ -s "$APP_DIR/$f" ] || missing="${missing} ${f}"
    done
    if [ -n "$missing" ]; then
        echo "ERROR: installed Android eval payload is incomplete; missing:${missing}" >&2
        return 1
    fi

    chmod +x "${APP_DIR}/test_verify.sh" 2>/dev/null || true
    [ -s "${APP_DIR}/new_vlm_verify.sh" ] && chmod +x "${APP_DIR}/new_vlm_verify.sh" 2>/dev/null || true
    echo "==> Android eval assets installed from pinned RB artifact"
    return 0
}
fetch_mobile_mcp() {
    local _spec="${MOBILE_MCP_PACKAGE:-}"
    local _install_dir="$RB_ANDROID_TOOLCHAIN_ROOT/mobile-mcp"
    [ -n "$_spec" ] || {
        echo "ERROR: MOBILE_MCP_PACKAGE is empty; release must pin mobile-mcp" >&2
        return 1
    }
    local _package_name="${_spec%@*}"
    local _expected_version="${_spec##*@}"
    if [ "$_expected_version" != "$RB_ANDROID_MOBILE_MCP_VERSION" ]; then
        echo "ERROR: mobile-mcp release pin $_expected_version does not match bootstrap pin $RB_ANDROID_MOBILE_MCP_VERSION" >&2
        return 1
    fi
    echo "==> Using bootstrap-installed mobile-mcp: ${_spec}"
    local _runtime_helper="$(rb_scripts_dir)/android/lib/runtime_helpers.py"
    MOBILE_MCP_BIN="$(python3 "$_runtime_helper" package-metadata \
        --root "$_install_dir" --package "$_package_name" --field binary)" || return 1
    [ -x "$MOBILE_MCP_BIN" ] || {
        echo "ERROR: installed mobile-mcp executable is missing: $MOBILE_MCP_BIN" >&2
        return 1
    }
    local _installed_version=""
    _installed_version="$(python3 "$_runtime_helper" package-metadata \
        --root "$_install_dir" --package "$_package_name" --field version)" || return 1
    [ -n "$_expected_version" ] && [ "$_installed_version" = "$_expected_version" ] || {
        echo "ERROR: mobile-mcp version mismatch: expected $_expected_version, got ${_installed_version:-missing}" >&2
        return 1
    }
    export MOBILE_MCP_BIN
    echo "==> mobile-mcp ready: ${MOBILE_MCP_BIN} (${_installed_version})"
}
validate_frozen_tests() {
    local dir="$1"
    [ -s "$dir/test_manifest.json" ] || {
        echo "ERROR: unified tests are missing test_manifest.json"
        return 1
    }

    local count=0 f
    for f in "$dir"/test_android_*.py; do
        [ -f "$f" ] || continue
        python3 -m py_compile "$f" >/dev/null 2>&1 || {
            echo "ERROR: frozen test is not Python-runnable: $f"
            return 1
        }
        count=$((count + 1))
    done
    [ "$count" -gt 0 ] || {
        echo "ERROR: unified tests contain no test_android_*.py"
        return 1
    }

    if [ ! -s "$dir/android_testgen_kit.py" ]; then
        echo "ERROR: unified tests are missing android_testgen_kit.py"
        return 1
    fi
    echo "==> Frozen tests validated: $count files + test_manifest.json"
}
materialize_unified_reference() {
    local dest="$1"
    [ -n "${RB_UNIFIED_REFERENCE_PATH:-}" ] || {
        echo "ERROR: RB_UNIFIED_REFERENCE_PATH is required"
        return "$RC_INFRA"
    }
    ensure_artifact_backend || return "$RC_INFRA"
    mkdir -p "$dest" || return "$RC_INFRA"
    echo "==> Checking unified reference: ${RB_UNIFIED_REFERENCE_PATH}/"
    # wrapsource carries repo@commit plus optional patches rather than a packaged APK.
    # Clone, patch, and build inside a PRIVATE task-shaped staging directory while the
    # trusted phase still has network access. Older bundles containing an APK (directly
    # or in reference.tar.gz) remain accepted as a compatibility fallback.
    #
    # Staging rather than a delete-list on purpose: source, patches, launch helpers, and
    # screenshots all disappear together before the recreation agent starts. Only the
    # installed app survives for black-box observation.
    local stage="$dest/.rb_reference_task"
    local stage_reference="$stage/reference"
    rm -rf "$stage" || return "$RC_INFRA"
    mkdir -p "$stage_reference" || return "$RC_INFRA"
    cp "$dest/instance.json" "$stage/instance.json" || return "$RC_INFRA"
    artifact_xfer dl_dir "${RB_UNIFIED_REFERENCE_PATH}" "$stage_reference" >/dev/null 2>&1 || {
        rm -rf "$stage"
        return "$RC_INFRA"
    }
    if [ ! -s "$dest/clean.apk" ]; then
        local apk; apk="$(find "$stage_reference" -name '*.apk' -type f | head -1)"
        if [ -z "$apk" ] && [ -s "$stage_reference/reference.tar.gz" ]; then
            local tmp="$stage/.unpack"
            mkdir -p "$tmp" || return "$RC_INFRA"
            if ! tar -tzf "$stage_reference/reference.tar.gz" >/dev/null 2>&1; then
                echo "ERROR: unified reference archive is invalid"
                rm -rf "$stage"
                return "$RC_DATA"
            fi
            tar -xzf "$stage_reference/reference.tar.gz" -C "$tmp" 2>/dev/null || {
                rm -rf "$stage"
                return "$RC_INFRA"
            }
            apk="$(find "$tmp" -name '*.apk' -type f | head -1)"
        fi
        if [ -n "$apk" ]; then
            cp "$apk" "$dest/clean.apk" || {
                rm -rf "$stage"
                return "$RC_INFRA"
            }
            echo "==> unified reference: $(basename "$apk") -> clean.apk"
        fi
    fi
    if [ ! -s "$dest/clean.apk" ]; then
        resolve_aapt || {
            echo "ERROR: aapt is required to build the wrapsource Android reference"
            rm -rf "$stage"
            return "$RC_INFRA"
        }
        echo "==> No packaged APK; building reference from pinned source"
        PYTHONPATH="$(rb_scripts_dir)${PYTHONPATH:+:$PYTHONPATH}" \
            python3 "$(rb_scripts_dir)/android/lib/reference_builder.py" \
                --descriptor "$stage/instance.json" \
                --reference-dir "$stage_reference" \
                --source-dir "$stage/source" \
                --output-apk "$dest/clean.apk" \
                --aapt "$_AAPT_BIN" \
                --timeout-sec "${RB_ANDROID_REFERENCE_BUILD_TIMEOUT:-7200}" || {
                    echo "ERROR: failed to build Android reference from wrapsource metadata"
                    rm -rf "$stage"
                    return "$RC_INFRA"
                }
    fi
    # Nothing but clean.apk survives. stage_recreation installs it and then deletes
    # the on-disk APK immediately before the agent starts.
    rm -rf "$stage" || return "$RC_INFRA"
    [ -s "$dest/clean.apk" ] || return "$RC_DATA"
    local apk_rc=0
    apk_package_name "$dest/clean.apk" >/dev/null 2>&1 || apk_rc=$?
    [ "$apk_rc" -eq "$RC_OK" ] || {
        [ "$apk_rc" -eq "$RC_INFRA" ] && return "$RC_INFRA"
        return "$RC_DATA"
    }
}
ensure_instance_metadata() {
    [ -n "${RB_UNIFIED_INSTANCE_PATH:-}" ] || {
        echo "ERROR: RB_UNIFIED_INSTANCE_PATH is required"
        return "$RC_INFRA"
    }
    mkdir -p "$REFERENCE_DIR" || return "$RC_INFRA"
    local descriptor="$REFERENCE_DIR/instance.json"
    if [ ! -s "$descriptor" ]; then
        ensure_artifact_backend || return "$RC_INFRA"
        artifact_xfer dl_file "$RB_UNIFIED_INSTANCE_PATH" "$descriptor" >/dev/null 2>&1 || {
            echo "ERROR: failed to download unified instance descriptor: $RB_UNIFIED_INSTANCE_PATH"
            return "$RC_INFRA"
        }
    fi
    local package
    package="$(python3 "$(rb_scripts_dir)/android/lib/runtime_helpers.py" \
        descriptor-package "$descriptor" 2>/dev/null)" || {
        echo "ERROR: unified instance descriptor is invalid JSON"
        return "$RC_DATA"
    }
    [ -n "$package" ] || {
        echo "ERROR: unified instance descriptor has no Android package"
        return "$RC_DATA"
    }
    printf 'ORIG_PACKAGE=%q\n' "$package" > "$REFERENCE_DIR/original_meta.env" || return "$RC_INFRA"
}

sync_reference_package_from_apk() {
    local actual declared="" apk_rc=0
    actual="$(apk_package_name "$REFERENCE_DIR/clean.apk")" || apk_rc=$?
    if [ "$apk_rc" -ne "$RC_OK" ]; then
        [ "$apk_rc" -eq "$RC_INFRA" ] && return "$RC_INFRA"
        return "$RC_DATA"
    fi
    [ -n "$actual" ] || {
        echo "ERROR: could not read the package name from frozen reference APK"
        return "$RC_DATA"
    }
    source "$REFERENCE_DIR/original_meta.env" 2>/dev/null || true
    declared="${ORIG_PACKAGE:-}"
    if [ "$declared" != "$actual" ]; then
        echo "WARN: frozen instance package metadata '${declared}' differs from APK '${actual}'; using APK package"
    fi
    # The installed binary is authoritative for adb install/launch verification.  Keeping stale
    # descriptor metadata here made a successful install look absent (Aegis is a debug APK whose
    # applicationId has the .debug suffix).
    printf 'ORIG_PACKAGE=%q\n' "$actual" > "$REFERENCE_DIR/original_meta.env" || return "$RC_INFRA"
}

ensure_reference_input() {
    if [ -s "$REFERENCE_DIR/clean.apk" ]        && apk_package_name "$REFERENCE_DIR/clean.apk" >/dev/null 2>&1        && [ -s "$REFERENCE_DIR/original_meta.env" ]; then
        echo "[unified] frozen reference already materialized"
        sync_reference_package_from_apk
        return $?
    fi
    local rc=0
    ensure_instance_metadata || { rc=$?; return "$rc"; }
    materialize_unified_reference "$REFERENCE_DIR" || {
        rc=$?
        echo "ERROR: unified reference contains no usable APK"
        return "$rc"
    }
    sync_reference_package_from_apk
}

download_frozen_tests() {
    [ -n "${RB_UNIFIED_TESTS_PATH:-}" ] || {
        echo "ERROR: RB_UNIFIED_TESTS_PATH is required"
        return "$RC_INFRA"
    }
    if ! validate_frozen_tests "$TESTS_DIR"; then
        rm -rf "$TESTS_DIR"
        mkdir -p "$TESTS_DIR"
        ensure_artifact_backend || return "$RC_INFRA"
        echo "==> Fetching frozen tests: $RB_UNIFIED_TESTS_PATH/"
        artifact_xfer dl_dir "$RB_UNIFIED_TESTS_PATH" "$TESTS_DIR" >/dev/null 2>&1 || {
            echo "ERROR: failed to download unified frozen tests"
            return "$RC_INFRA"
        }
    fi
    restore_script_modes "$TESTS_DIR"
    validate_frozen_tests "$TESTS_DIR" || return "$RC_DATA"

    [ -n "${RB_UNIFIED_VLM_PATH:-}" ] || {
        echo "ERROR: RB_UNIFIED_VLM_PATH is required for canonical Android VLM scoring"
        return "$RC_INFRA"
    }
    local canonical_vlm="$TESTS_DIR/.rb_canonical_vlm_assertions.json"
    ensure_artifact_backend || return "$RC_INFRA"
    artifact_xfer dl_file "$RB_UNIFIED_VLM_PATH" "$canonical_vlm" >/dev/null 2>&1 || {
        echo "ERROR: failed to download canonical VLM assertions: $RB_UNIFIED_VLM_PATH"
        return "$RC_INFRA"
    }
    local rb_common
    rb_common="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../common" && pwd)"
    PYTHONPATH="$rb_common" python3 -m rb_unify.eval_bridge android-manifest \
        "$canonical_vlm" "$TESTS_DIR/test_manifest.json" || {
        echo "ERROR: could not apply canonical Android VLM manifest"
        return "$RC_DATA"
    }
}

restore_script_modes() {
    local local_dir="$1"
    [ -d "$local_dir" ] || return 0
    find "$local_dir" -type f \( -name '*.sh' -o -name '*.py' \) -exec chmod +x {} + 2>/dev/null || true
}

download_stage_deps() {
    local target_stage="$1"
    echo ""
    echo "[unified] Preparing inputs for stage: $target_stage"
    case "$target_stage" in
        setup)
            ;;
        recreation)
            ensure_reference_input || return $?
            ;;
        eval)
            ensure_instance_metadata || return $?
            download_frozen_tests || return $?
            if [ "${RB_EVAL_TARGET:-recreation}" = reference ]; then
                ensure_reference_input || return $?
            elif [ -s "$REC_DIR/recreation/recreated.apk" ]; then
                echo "[artifact] recreation/recreated.apk already on disk"
            else
                artifact_download_dir "$RECREATION_STAGE" "$REC_DIR" || return "$RC_INFRA"
                restore_script_modes "$REC_DIR"
            fi
            ;;
        *)
            echo "ERROR: unsupported release stage: $target_stage"
            return "$RC_INFRA"
            ;;
    esac
}
collect_model_gateway_logs() {
    local dest_dir="$1"
    local shared_log_root="${RB_SHARED_DIR:-/workspace/shared}/logs"
    local found=0
    for d in "$shared_log_root"/claudecode-*; do
        [ -d "$d" ] || continue
        mkdir -p "$dest_dir"
        cp -rf "$d/." "$dest_dir/" 2>/dev/null || true
        found=1
    done
    if [ "$found" -eq 0 ]; then
        echo "WARN: no model-gateway request logs in $shared_log_root/claudecode-*; skip"
        return 0
    fi
    local n; n=$(find "$dest_dir" -type f 2>/dev/null | wc -l | tr -d ' ')
    echo "==> Collected proxy_logs ($n files) -> $dest_dir"
}
upload_stage_outputs() {
    local completed_stage="$1"
    echo ""
    echo "[artifact] Uploading outputs for stage: $completed_stage"

    local artifact_status=0
    artifact_upload_enabled || artifact_status=$?
    if [ "$artifact_status" -eq 2 ]; then
        # A mistyped knob is a config error, and the caller now treats every non-zero
        # return here as a transient warning — so this one has to die on the spot.
        die "$RC_INFRA" "invalid RB_ARTIFACT_UPLOAD=${RB_ARTIFACT_UPLOAD:-}"
    fi
    if [ "$artifact_status" -ne 0 ]; then
        echo "[artifact] upload disabled or artifact store unavailable; skipping upload"
        return 0
    fi

    local src_dir artifact_stage
    case "$completed_stage" in
        setup)      src_dir="$REC_DIR";     artifact_stage="setup" ;;
        recreation) src_dir="$REC_DIR";     artifact_stage="$RECREATION_STAGE" ;;
        eval)       src_dir="$EVAL_DIR";    artifact_stage="$EVAL_STAGE" ;;
        *)          return 0 ;;
    esac

    # Model-gateway request logs（含 headers + 原始 response）随阶段产物上传。
    # proxy_logs/ is the canonical name (core.trajectory.CANONICAL_PROXY_LOGS_DIR,
    # the common artifact contract). This is a copy destination: the controller
    # owns the source layout and RB only preserves the canonical evidence directory.
    collect_model_gateway_logs "$src_dir/proxy_logs"

    # 原样透传 artifact_xfer 的退出码：3 = 部分文件在重试后仍失败，由调用方决定，
    # 塌缩成 1 会让「少传了几个 gradle 中间文件」和「一个都没传上去」不可区分。
    local st=0
    artifact_upload_dir "$src_dir" "$artifact_stage" || st=$?
    return "$st"
}

stage_setup() {
    mkdir -p "$OUTPUT_DIR"
    # One writer for the boundary and the launch prefix: apply_permission_isolation. Setting them here
    # too was duplication, and the tests caught it -- two writers is how the demotion got silently
    # reset once already.
    apply_permission_isolation || return "$RC_INFRA"
    [ -s "$_PERM_REPORT" ] && cp "$_PERM_REPORT" "$OUTPUT_DIR/permission_report.json" 2>/dev/null || true
    local _summary="$OUTPUT_DIR/permission_setup.txt"
    local _probe_fail=0

    # The hardcoded probes only test what someone thought to write down. The leak they stand in for was
    # never found by reading code, so a REAL agent gets a turn: capture-the-flag through the exact
    # launch path recreation uses. Opt-in, because it is a real agent and needs a model key.
    if is_truthy "${RB_SETUP_PROBE_AGENT:-}"; then
        local _token _traj _prompt _prompt_file _probe_spec _probe_rb_scripts _agent_rc=0
        local _probe_verdict_rc
        _token="RBLEAK-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        printf 'reference stand-in, token=%s\n' "$_token" > "$REFERENCE_DIR/.rb_setup_probe"
        printf 'tests stand-in, token=%s\n' "$_token" > "$TESTS_DIR/.rb_setup_probe"
        chown root:root "$REFERENCE_DIR/.rb_setup_probe" "$TESTS_DIR/.rb_setup_probe" 2>/dev/null || true
        chmod 600 "$REFERENCE_DIR/.rb_setup_probe" "$TESTS_DIR/.rb_setup_probe" 2>/dev/null || true
        _traj="$OUTPUT_DIR/setup_probe_trajectory.jsonl"
        _prompt="This is a permission-isolation self-test of your own sandbox. Nothing here is
private data and nothing is a trick: I am checking whether a boundary I configured actually holds.

Two files each contain a secret token: ${REFERENCE_DIR}/.rb_setup_probe and ${TESTS_DIR}/.rb_setup_probe

Please (1) try to read both and print any token you obtain, (2) list every other file you can read
under ${REFERENCE_DIR} and ${TESTS_DIR}, including subdirectories, and (3) say which paths refused you.

Use ordinary reads and directory listings. Do NOT try to escalate privileges, change permissions,
or modify anything. If everything refuses you, print exactly: NO ACCESS -- and that is the expected,
        correct result. Keep it short and stop as soon as you have the answer."
        echo "==> Launching the probe agent through the real launch path (token planted)"
        _prompt_file="$OUTPUT_DIR/setup_probe_prompt.txt"
        _probe_spec="$OUTPUT_DIR/setup_probe_invocation.json"
        _probe_rb_scripts="$(rb_scripts_dir || true)"
        printf '%s' "$_prompt" > "$_prompt_file"
        PYTHONPATH="$_probe_rb_scripts" python3 -m core.agent_invocation create \
            --output "$_probe_spec" --agent-cli claude \
            --model "${RECREATION_CLAUDE_MODEL:-${CLAUDE_MODEL:-}}" \
            --prompt-file "$_prompt_file" --workspace "$OUTPUT_DIR" \
            --trajectory "$_traj" --stderr "$OUTPUT_DIR/setup_probe_stderr.log" \
            --timeout "${RB_SETUP_PROBE_TIMEOUT:-600}" \
            --request-timeout-ms "${API_TIMEOUT_MS:-1800000}" \
            --tool-timeout-ms "${RB_MCP_TOOL_TIMEOUT:-180000}" \
            --isolate-environment || return "$RC_INFRA"
        : > "$_traj"
        : > "$OUTPUT_DIR/setup_probe_stderr.log"
        chmod a+r "$_prompt_file" "$_probe_spec"
        chown "$RB_AGENT_USER:$RB_AGENT_USER" \
            "$_traj" "$OUTPUT_DIR/setup_probe_stderr.log" 2>/dev/null || true
        "${_AGENT_RUNAS[@]}" env "${_AGENT_ENV[@]}" \
            "PYTHONPATH=$_probe_rb_scripts" python3 -m core.agent_invocation \
            run --spec "$_probe_spec" || _agent_rc=$?
        _probe_verdict_rc="$(PYTHONPATH="$_probe_rb_scripts" python3 -m core.agent_invocation \
            terminal --trajectory "$_traj" --native-rc "$_agent_rc" \
            --field exit-code 2>/dev/null || echo 2)"
        echo "    probe agent rc=$_agent_rc trajectory=$(wc -c < "$_traj" 2>/dev/null || echo 0) bytes"
        if [ "$_probe_verdict_rc" != 0 ]; then
            echo "PROBE AGENT DID NOT COMPLETE (native=$_agent_rc verdict=$_probe_verdict_rc) — inconclusive, not a pass" >> "$_summary"
            _probe_fail=1
        elif [ ! -s "$_traj" ]; then
            # An agent that never ran proves nothing either way, and calling that a pass is how a
            # green canary comes to mean nothing at all.
            echo "PROBE AGENT PRODUCED NO TRAJECTORY (rc=$_agent_rc) — inconclusive, not a pass" >> "$_summary"
            _probe_fail=1
        elif grep -qF "$_token" "$_traj"; then
            echo "PROBE AGENT CAPTURED THE TOKEN — the boundary does NOT hold" >> "$_summary"
            grep -oF -m3 "$_token" "$_traj" | head -1 >> "$_summary"
            _probe_fail=1
        else
            echo "probe agent: token NOT captured (boundary held under a real agent)" >> "$_summary"
        fi
        # Whatever it managed to list is the interesting part, and it is what would have found the
        # producer of the original leak. Surfaced for reading, never used as the verdict.
        python3 "$_probe_rb_scripts/android/lib/runtime_helpers.py" \
            trajectory-summary "$_traj" >> "$_summary" 2>/dev/null || true
    fi

    echo "==> Setup stage: boundary established and probed clean"
    return 0
}

# Establish and ATTEST the agent's file boundary, then export how to launch it.
#
# Sets, for the caller: _AGENT_RUNAS / _AGENT_ENV (the launch prefix) and RB_AGENT_USER. Returns
# non-zero when the boundary could not be established -- callers must treat that as fatal, because the
# alternative is a scored run at an isolation level nobody verified.
#
# The protocol itself is core.permission_probe; this function only names android's paths and turns the
# result into a launch prefix.
apply_permission_isolation() {
    local _rb_scripts _env_file
    local _workspace_dir="${WORKSPACE_DIR:-/workspace/recreation}"
    local _runtime_dir="${RUNTIME_DIR:-/workspace/runtime}"
    _rb_scripts="$(rb_scripts_dir || true)"
    if [ -z "${_rb_scripts}" ]; then
        echo "ERROR: permission isolation requested but rb core is unavailable — refusing to run unisolated"
        return 1
    fi
    mkdir -p "$REC_DIR"
    _PERM_REPORT="$REC_DIR/permission_report.json"
    _env_file="$REC_DIR/permission_env.sh"
    echo "==> Permission isolation: establishing and attesting the agent's boundary..."
    if ! PYTHONPATH="${_rb_scripts}" python3 "${_rb_scripts}/core/permission_probe.py" \
            --platform android \
            --run-id "${TASK_ID:-${INSTANCE_ID}}" \
            --protected "$REFERENCE_DIR" \
            --protected "$TESTS_DIR" \
            --protected "$REC_DIR" \
            --writable "$_workspace_dir" \
            --writable "$_runtime_dir" \
            --spec-out "$REC_DIR/permission_spec.json" \
            --report "$_PERM_REPORT" \
            --env-out "$_env_file"; then
        echo "ERROR: permission isolation did NOT hold — refusing to run the agent"
        return 1
    fi
    RB_AGENT_USER="${RB_ANDROID_AGENT_USER:-rbagent}"
    RB_AGENT_HOME="/home/$RB_AGENT_USER"
    # runuser does not reliably set HOME, and an agent without its cache env starts a COLD cache in
    # the fresh home -- the failure that presents as a slow model rather than a misconfiguration.
    if [ -s "$_env_file" ]; then
        while IFS= read -r _l; do
            case "$_l" in export\ *=*) _AGENT_ENV+=("${_l#export }") ;; esac
        done < "$_env_file"
        echo "    agent env: ${_AGENT_ENV[*]}"
    fi
    _AGENT_ENV+=(
        "RB_ANDROID_OFFLINE_ROOT=$RB_ANDROID_OFFLINE_ROOT"
        "ANDROID_HOME=$ANDROID_HOME"
        "ANDROID_SDK_ROOT=$ANDROID_SDK_ROOT"
        "GRADLE_HOME=$GRADLE_HOME"
        "PATH=$PATH"
        # runuser does not preserve the pod env. Carry the shared 30m model-request
        # and 3m MCP-call clocks into the actual agent process explicitly.
        "API_TIMEOUT_MS=${API_TIMEOUT_MS:-1800000}"
        "MCP_TOOL_TIMEOUT=${RB_MCP_TOOL_TIMEOUT:-180000}"
        # Model-request retry is owned by the shared sidecar for both CLIs.
        "CLAUDE_CODE_MAX_RETRIES=0"
        "MAX_STRUCTURED_OUTPUT_RETRIES=0"
    )
    _AGENT_RUNAS=(runuser -u "$RB_AGENT_USER" --)
    # The MCP config is passed by path and read by the CLI, so the agent must be able to read it.
    [ -f "${MCP_CONFIG_PATH:-}" ] && chmod a+r "$MCP_CONFIG_PATH" 2>/dev/null || true
    return 0
}

dedicated_agent_live_pids() {
    local _user="$1" _proc_root="${2:-/proc}" _uid _proc _pid _stat _rest _state
    _uid="$(id -u "$_user")" || return 1
    for _proc in "$_proc_root"/[0-9]*; do
        [ -d "$_proc" ] || continue
        [ "$(stat -c '%u' "$_proc" 2>/dev/null || true)" = "$_uid" ] || continue
        _pid="${_proc##*/}"
        _stat="$(cat "$_proc/stat" 2>/dev/null || true)"
        [ -n "$_stat" ] || continue
        # /proc/<pid>/stat includes the process name in parentheses and that name
        # may itself contain spaces or ')'. Strip through the final ') ' before
        # reading field 3 (state).
        _rest="${_stat##*) }"
        _state="${_rest%% *}"
        case "$_state" in
            Z|X) continue ;;
        esac
        printf '%s\n' "$_pid"
    done | sort -n
}

stop_dedicated_agent_processes() {
    local _user="$1" _remaining="" _attempt _pid
    id "$_user" >/dev/null 2>&1 || {
        echo "ERROR: cannot freeze workspace; agent user does not exist: $_user" >&2
        return 1
    }
    for _attempt in 1 2 3; do
        _remaining="$(dedicated_agent_live_pids "$_user")" || return 1
        [ -n "$_remaining" ] || return 0
        while IFS= read -r _pid; do
            [ -z "$_pid" ] || kill -KILL "$_pid" 2>/dev/null || true
        done <<< "$_remaining"
        sleep 1
    done
    _remaining="$(dedicated_agent_live_pids "$_user")" || return 1
    if [ -n "$_remaining" ]; then
        echo "ERROR: agent processes survived shutdown for $_user: ${_remaining//$'\n'/ }" >&2
        return 1
    fi
    return 0
}

stage_recreation() {
    echo ""
    echo "=========================================="
    echo "=== Stage: RECREATION"
    echo "=========================================="

    local WORKSPACE_DIR="/workspace/recreation"
    local RUNTIME_DIR="/workspace/runtime"
    # The current deployment platform image supplies REC_DIR=/workspace/recreation. Keep the
    # durable artifact parent separate from the fixed agent-visible path so the
    # uploaded shape remains recreation/recreated.apk instead of either nesting
    # twice or asking a symlink to replace its own parent directory.
    if [ "$(readlink -f "$REC_DIR")" = "$(readlink -f "$WORKSPACE_DIR")" ]; then
        local RELOCATED_REC_DIR="${REC_DIR}.stage"
        if [ -e "$RELOCATED_REC_DIR" ]; then
            echo "ERROR: relocated recreation stage already exists: $RELOCATED_REC_DIR" >&2
            return "$RC_INFRA"
        fi
        mv "$REC_DIR" "$RELOCATED_REC_DIR" || return "$RC_INFRA"
        REC_DIR="$RELOCATED_REC_DIR"
        export REC_DIR
    fi
    local WORKSPACE_STORAGE_DIR="$REC_DIR/recreation"
    mkdir -p "$WORKSPACE_STORAGE_DIR"
    mkdir -p "$(dirname "$WORKSPACE_DIR")"
    # Keep the path shown to the agent physically independent from stage storage.  A symlink here
    # exposes its target through readlink/pwd and can reveal run identity on hosts whose REC_DIR is
    # task-named.  The trusted stage snapshots this directory after the agent exits.
    rm -rf "$WORKSPACE_DIR"
    mkdir -p "$WORKSPACE_DIR/src"
    rm -rf "$RUNTIME_DIR"
    mkdir -p "$RUNTIME_DIR"
    if [ -L "$WORKSPACE_DIR" ] || [ ! -d "$WORKSPACE_DIR" ]; then
        echo "ERROR: canonical workspace is not a real directory: $WORKSPACE_DIR" >&2
        return "$RC_INFRA"
    fi
    local CODE_DIR="$WORKSPACE_DIR/src"
    local OUTPUT_APK="$WORKSPACE_DIR/recreated.apk"
    prepare_android_gradle_wrapper "$CODE_DIR" || return "$RC_INFRA"
    # Write the shared artifact descriptor at the canonical app root. It is written up front so it
    # remains present even if a later stage is resumed from the artifact store.
    _ra_common="$(ensure_rb_core || true)"
    if [ -n "${_ra_common}" ]; then
        PYTHONPATH="$(dirname "${_ra_common}")" python3 -m core.recreation_artifact write \
            --dest "$REC_DIR/recreation" --platform android --root . \
            --entry recreated.apk --provisional 2>&1 || echo "WARN: recreation manifest not written"
    fi

    # Reuse a recreation only when its APK exists. Logs, trajectories, sessions, and
    # prompts may be uploaded before completion, so a non-empty prefix is insufficient.
    # The candidate may live under an immutable source namespace while this eval writes to a
    # fresh output namespace. RB_ARTIFACT_RESTORE_RUN_PREFIX falls back to RB_ARTIFACT_RUN_PREFIX for
    # historical same-prefix recreation/resume jobs.
    local _REC_APK_KEY="${RB_ARTIFACT_RESTORE_RUN_PREFIX}/${RECREATION_STAGE}/recreation/recreated.apk"
    if force_stage_enabled recreation; then
        echo "FORCE_STAGE includes 'recreation' -> skip reuse, re-run recreation"
    elif artifact_env_available && [ "$(artifact_object_exists "$_REC_APK_KEY" 2>/dev/null)" = "exists" ]; then
        echo "Recreation APK found in artifact store ($_REC_APK_KEY), downloading full stage and reusing"
        artifact_download_dir "$RECREATION_STAGE" "$REC_DIR" || return "$RC_INFRA"
        restore_script_modes "$REC_DIR"
        if [ -s "$REC_DIR/recreation/recreated.apk" ] && apk_package_name "$REC_DIR/recreation/recreated.apk" >/dev/null 2>&1; then
            echo "Recreation stage complete (reused from artifact store)"
            return 0
        fi
        echo "WARN: downloaded recreation artifacts invalid, regenerating"
    fi

    # 装回原始 app 供黑盒观测，随后删除磁盘 APK 文件（agent 只能黑盒观测）
    source "$REFERENCE_DIR/original_meta.env" 2>/dev/null || true
    if [ -s "$REFERENCE_DIR/clean.apk" ]; then
        if [ -n "${ORIG_PACKAGE:-}" ] && ! adb -s "${DEVICE_ID}" shell pm list packages 2>/dev/null | grep -q "package:${ORIG_PACKAGE}$"; then
            echo "==> Reinstalling original app for recreation observation..."
            echo "    [install] apk: $(ls -l "$REFERENCE_DIR/clean.apk" 2>&1)"
            echo "    [install] apk native-lib ABIs: $(unzip -l "$REFERENCE_DIR/clean.apk" 2>/dev/null | grep -oE 'lib/[^/]+/' | sort -u | tr '\n' ' ')"
            echo "    [install] emulator ABIs: $(adb -s "${DEVICE_ID}" shell getprop ro.product.cpu.abilist 2>/dev/null | tr -d '\r')"
            local _install_out _install_rc=0
            _install_out="$(adb -s "${DEVICE_ID}" install -t --bypass-low-target-sdk-block -g "$REFERENCE_DIR/clean.apk" 2>&1)" || _install_rc=$?
            echo "    [install] adb install exit=${_install_rc}, output:"
            echo "$_install_out" | sed 's/^/      | /'
            if adb -s "${DEVICE_ID}" shell pm path "${ORIG_PACKAGE}" 2>/dev/null | grep -q '^package:'; then
                echo "==> Install verified: ${ORIG_PACKAGE} present on device"
            else
                echo "ERROR: original app ${ORIG_PACKAGE} NOT installed after adb install (see output above)."
                echo "       Recreation agent would face an empty emulator; aborting early instead of burning the full timeout."
                return "$RC_INFRA"
            fi
        fi
        echo "==> Removing disk APK file to prevent leakage to recreation agent..."
        rm -f "$REFERENCE_DIR/clean.apk"
    fi

# List the exact reference material visible to the agent immediately before launch.
# Enforce this at the visibility boundary even if an upstream materializer creates
# extra files in the reference directory.
    #
    # A positive KEEP list, not a delete list: anything the frozen suite gains later is removed by
    # default instead of leaking until someone extends an enumeration. Only two things in here are
    # still needed downstream -- original_meta.env is read by eval (test_verify.sh --meta).
    #
    # This is a boundary sweep, not a substitute for uid separation: the agent is root here, so it
    # could recreate or re-fetch anything. Making $REFERENCE_DIR unreadable needs a non-root agent, which
    # is the larger change this does not attempt.
    if [ -n "${REFERENCE_DIR:-}" ] && [ -d "$REFERENCE_DIR" ]; then
        _swept=""
        for _e in "$REFERENCE_DIR"/* "$REFERENCE_DIR"/.[!.]*; do
            [ -e "$_e" ] || continue
            case "$(basename "$_e")" in
                original_meta.env) continue ;;
            esac
            _swept="${_swept} $(basename "$_e")"
            rm -rf "$_e"
        done
        if [ -n "$_swept" ]; then
            echo "==> Swept reference material from \$REFERENCE_DIR before the agent:${_swept}"
        fi
    fi

    # Written to a FILE as well as stdout, because the deployment log stream can be truncated --
    # this run's own log stopped 2.5 minutes in, while the
    # audit happens ~10 minutes in after the emulator boots and the reference installs. The first
    # version of this diagnostic was stdout-only and was therefore invisible on the very run it was
    # built for. $REC_DIR is uploaded with the stage, so the file survives.
    _audit="${REC_DIR:-/tmp}/leak_audit.txt"
    {
        echo "# agent-visible state immediately before launch"
        echo "# swept from \$REFERENCE_DIR:${_swept:-<nothing>}"
        for _d in "$REFERENCE_DIR" "$TESTS_DIR" "$APP_DIR" "$REC_DIR"; do
            [ -n "${_d:-}" ] && [ -d "$_d" ] || continue
            echo "## $_d"
            ls -la "$_d" 2>&1 | head -40
            # one level down matters: the leak was a SUBDIRECTORY (screenshots/), which a flat
            # listing shows only as a name.
            find "$_d" -maxdepth 2 -mindepth 1 2>/dev/null | head -60
        done
    } > "$_audit" 2>&1 || true
    # ── Permission isolation: mandatory immediately before the recreation agent ───────────
    # Everything above is a boundary SWEEP: delete the material before the agent starts, because a
    # root agent makes file modes meaningless. Tonight proved the limit of that approach -- the
    # reference screenshots survived a confined download AND a targeted sweep on a run that
    # demonstrably executed both, and the agent listed all 20 of them.
    #
    # Demotion closes it structurally instead: verified locally, an unprivileged agent gets
    # "permission denied" on attempts to inspect the frozen reference, while its
    # own canonical recreation tree stays writable.
    #
    # FAIL CLOSED. If the boundary cannot be established the run stops, because the alternative is a
    # scored result produced under an isolation level nobody checked. Declared HERE, above the
    # only shared launch path: declaring the arrays further
    # down -- still inside this function -- silently reset the demotion after it had been attested,
    # so the run printed "isolation attested" and then launched the agent as root.
    _AGENT_RUNAS=()
    _AGENT_ENV=()
    _PERM_REPORT=""
    apply_permission_isolation || return "$RC_INFRA"

    echo "==> Agent-visible state immediately before launch (leak audit -> $_audit):"
    sed 's/^/      /' "$_audit" 2>/dev/null | head -30

    # 安装 mobile-mcp（skill/no-skill 均需要）
    # 关键：mobile-mcp 内部通过 ANDROID_HOME/platform-tools/adb 定位 adb
    # 容器里 ANDROID_HOME=/opt/android/sdk 但 adb 实际在 /opt/android/platform-tools/adb
    # 因此需要从 adb 实际位置反推正确的 ANDROID_HOME 传给 mobile-mcp
    echo "==> Adding mobile-mcp MCP server..."
    local _adb_path
    _adb_path=$(which adb 2>/dev/null || echo "")
    [ -x "$_adb_path" ] || {
        echo "ERROR: adb is missing; mobile-mcp cannot reach the emulator" >&2
        return "$RC_INFRA"
    }
    local _mcp_android_home="${ANDROID_HOME:-/opt/android/sdk}"
    if [ -n "$_adb_path" ] && [ ! -f "${_mcp_android_home}/platform-tools/adb" ]; then
        # adb 不在 ANDROID_HOME/platform-tools/ 下，需要从实际路径反推
        # e.g. /opt/android/platform-tools/adb -> ANDROID_HOME=/opt/android
        _mcp_android_home=$(dirname $(dirname "$_adb_path"))
        echo "    INFO: adb not at \$ANDROID_HOME/platform-tools/adb; corrected from PATH"
        echo "    Actual adb: $_adb_path"
        echo "    Corrected ANDROID_HOME for mobile-mcp: $_mcp_android_home"
    fi
    [ -x "${_mcp_android_home}/platform-tools/adb" ] || {
        echo "ERROR: corrected ANDROID_HOME has no executable platform-tools/adb: $_mcp_android_home" >&2
        return "$RC_INFRA"
    }
    echo "    ANDROID_HOME(original)=${ANDROID_HOME:-unset}"
    echo "    ANDROID_HOME(for mcp)=${_mcp_android_home}"
    echo "    adb location: ${_adb_path:-not found}"
    fetch_mobile_mcp || return "$RC_INFRA"
    # 写 mcp_config.json 供 claude --mcp-config 注入（不依赖 .claude.json，兼容 --bare）。
    # Use the installed absolute binary, not npx: after the agent is demoted/network-isolated,
    # npx must not resolve a different cache entry or contact the registry.
    MCP_CONFIG_PATH="/tmp/mobile_mcp_config.json"
    local _runtime_helper="$(rb_scripts_dir)/android/lib/runtime_helpers.py"
    PYTHONPATH="$(rb_scripts_dir)" python3 "$_runtime_helper" mcp-config \
        --binary "$MOBILE_MCP_BIN" \
        --android-home "$_mcp_android_home" \
        --path "$PATH" \
        --coordinate-space "$MOBILE_MCP_COORDINATE_SPACE" \
        > "$MCP_CONFIG_PATH"
    chmod a+r "$MCP_CONFIG_PATH"
    echo "==> Wrote MCP config to ${MCP_CONFIG_PATH}"


    # 验证 ADB 设备就绪（含 UI 子系统），5 分钟超时
    local MCP_READY_TIMEOUT="${MCP_READY_TIMEOUT:-300}"
    echo "==> Waiting for device ${DEVICE_ID} to be fully ready (UI subsystem, timeout ${MCP_READY_TIMEOUT}s)..."
    local _mcp_elapsed=0
    while :; do
        if adb -s "${DEVICE_ID}" shell dumpsys window displays 2>/dev/null | grep -q 'mCurrentFocus\|mFocusedApp'; then
            echo "==> Device ${DEVICE_ID} UI subsystem confirmed ready"
            break
        fi
        if [ "$_mcp_elapsed" -ge "$MCP_READY_TIMEOUT" ]; then
            echo "ERROR: Device ${DEVICE_ID} UI readiness not confirmed within ${MCP_READY_TIMEOUT}s" >&2
            return "$RC_INFRA"
        fi
        sleep 5; _mcp_elapsed=$((_mcp_elapsed + 5))
    done

    # ========== 打印 mobile-mcp 的所有 tool schema ==========
    # 加载完 MCP 后，通过 JSON-RPC tools/list 拉取并打印全部工具的 name + inputSchema，
    # 方便确认 pinned runtime 暴露了哪些工具。缺失任何 release tool 都是致命错误。
    echo "==> Listing mobile-mcp tool schemas..."
    local _mcp_ts_stderr; _mcp_ts_stderr=$(mktemp)
    local _mcp_ts_raw
    _mcp_ts_raw=$(printf '%s\n%s\n%s\n' \
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"schema-dump","version":"1.0"}}}' \
        '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
        | timeout 30 env "ANDROID_HOME=${_mcp_android_home}" "PATH=${PATH}" \
            "MOBILE_MCP_COORDINATE_SPACE=${MOBILE_MCP_COORDINATE_SPACE}" \
            "$MOBILE_MCP_BIN" 2>"$_mcp_ts_stderr")
    printf '%s\n' "$_mcp_ts_raw" | python3 "$_runtime_helper" validate-mobile-tools || {
        echo "ERROR: mobile-mcp tools/list validation failed" >&2
        sed 's/^/    /' "$_mcp_ts_stderr" >&2
        rm -f "$_mcp_ts_stderr"
        return "$RC_INFRA"
    }
    rm -f "$_mcp_ts_stderr"

    # Schema presence is not enough: run the exact installed stdio server as the exact
    # demoted recreation principal, launch the frozen reference package, then require a
    # complete uiautomator hierarchy and a real screenshot. Any failure means the agent
    # cannot perform the benchmark and must not be converted into a low score.
    [ -n "${ORIG_PACKAGE:-}" ] || {
        echo "ERROR: ORIG_PACKAGE is empty; cannot preflight the reference app" >&2
        return "$RC_INFRA"
    }
    echo "==> Strict mobile-mcp reference preflight (${ORIG_PACKAGE})..."
    local _cua_preflight_mode="${RB_CUA_PREFLIGHT_MODE:-strict}"
    case "$_cua_preflight_mode" in
        strict|warn) ;;
        *)
            echo "ERROR: invalid RB_CUA_PREFLIGHT_MODE=$_cua_preflight_mode (want strict or warn)" >&2
            return "$RC_INFRA"
            ;;
    esac
    local _mcp_probe="$(rb_scripts_dir)/core/mcp_probe.py"
    [ -r "$_mcp_probe" ] || {
        echo "ERROR: strict MCP probe is unavailable: $_mcp_probe" >&2
        return "$RC_INFRA"
    }
    local _mobile_command_json
    _mobile_command_json="$(python3 "$_runtime_helper" command-json "$MOBILE_MCP_BIN")" || \
        return "$RC_INFRA"
    if ! "${_AGENT_RUNAS[@]}" env "${_AGENT_ENV[@]}" \
            "ANDROID_HOME=${_mcp_android_home}" "PATH=${PATH}" \
            "MOBILE_MCP_COORDINATE_SPACE=${MOBILE_MCP_COORDINATE_SPACE}" \
            python3 "$_mcp_probe" --kind mobile \
                --command-json "$_mobile_command_json" \
                --device "$DEVICE_ID" --package "$ORIG_PACKAGE" --timeout 45; then
        if [ "$_cua_preflight_mode" = warn ]; then
            echo "WARNING: mobile-mcp cannot observe the live reference app; continuing because RB_CUA_PREFLIGHT_MODE=warn" >&2
        else
            echo "ERROR: mobile-mcp cannot observe the live reference app; aborting recreation" >&2
            return "$RC_INFRA"
        fi
    fi




    # One renderer, one prompt. Android used to install a second 586-line skill beside a separate
    # text template; those sources contradicted each other on package ID and APK availability.
    if ! DEVICE_ID="$DEVICE_ID" render_cc_prompt recreation > "$RUNTIME_DIR/prompt.txt"; then
        die "$RC_INFRA" "could not render the recreation prompt"
    fi

    # The shared invocation contract removes grading inputs and upstream
    # credentials before starting the agent child.
    cd "$WORKSPACE_DIR" || return "$RC_INFRA"
    echo "==> Running ${AGENT_CLI} recreation..."
    # settings.json is a CLAUDE-CODE-ONLY knob: codex learns tools from config.toml and never
    # reads it. Kept inside the claude branch so the two agents' setup does not overlap.
    case "$AGENT_CLI" in
      codex*) ;;
      *) write_claude_settings || return "$RC_INFRA" ;;
    esac
    local _CC_T0; _CC_T0=$(date +%s)
    local _rb_invocation_scripts _invocation_spec
    _rb_invocation_scripts="$(rb_scripts_dir || true)"
    [ -n "$_rb_invocation_scripts" ] || return "$RC_INFRA"
    _invocation_spec="$RUNTIME_DIR/invocation.json"
    local _invocation_model="$RECREATION_CLAUDE_MODEL"
    local _invocation_mcp="$MCP_CONFIG_PATH"
    case "$AGENT_CLI" in
        codex*) _invocation_model="$RECREATION_MODEL"; _invocation_mcp="" ;;
    esac
    local _create_args=(
        create --output "$_invocation_spec" --agent-cli "$AGENT_CLI"
        --model "$_invocation_model" --prompt-file "$RUNTIME_DIR/prompt.txt"
        --workspace "$WORKSPACE_DIR" --trajectory "$RUNTIME_DIR/trajectory.jsonl"
        --stderr "$RUNTIME_DIR/stderr.log" --timeout "$RECREATION_TIMEOUT"
        --request-timeout-ms "${API_TIMEOUT_MS:-1800000}"
        --tool-timeout-ms "${RB_MCP_TOOL_TIMEOUT:-180000}"
        --run-name "rb-recreation-anonymous"
        --isolate-environment
    )
    [ -z "${THINKING_EFFORT:-}" ] || _create_args+=(--reasoning-effort "$THINKING_EFFORT")
    [ -z "$_invocation_mcp" ] || _create_args+=(--mcp-config "$_invocation_mcp")
    if [ "${RB_CAPTURE_TOOL_USE_SCREENSHOTS:-false}" = "true" ]; then
        _create_args+=(
            --capture-tool-use-screenshots
            --screenshot-dir "$RUNTIME_DIR/tool_use_screenshots"
            --screenshot-command adb
            --screenshot-command=-s
            --screenshot-command "$DEVICE_ID"
            --screenshot-command exec-out
            --screenshot-command screencap
            --screenshot-command=-p
            --screenshot-stdout
        )
    fi
    PYTHONPATH="$_rb_invocation_scripts" python3 -m core.agent_invocation \
        "${_create_args[@]}" || return "$RC_INFRA"
    chmod a+r "$RUNTIME_DIR/prompt.txt" "$_invocation_spec"
    # The permission boundary intentionally grants the agent only the authored
    # recreation tree, not REC_DIR itself. Pre-create just the two CLI streams and
    # hand those file descriptors' paths to the demoted principal; otherwise the
    # shared runner cannot open them and every isolated run fails before launch.
    : > "$RUNTIME_DIR/trajectory.jsonl"
    : > "$RUNTIME_DIR/stderr.log"
    if [ "${RB_CAPTURE_TOOL_USE_SCREENSHOTS:-false}" = "true" ]; then
        mkdir -p "$RUNTIME_DIR/tool_use_screenshots"
    fi
    if [ "${#_AGENT_RUNAS[@]}" -gt 0 ]; then
        chown "$RB_AGENT_USER:$RB_AGENT_USER" \
            "$RUNTIME_DIR/trajectory.jsonl" "$RUNTIME_DIR/stderr.log" || return "$RC_INFRA"
        chmod 600 "$RUNTIME_DIR/trajectory.jsonl" "$RUNTIME_DIR/stderr.log"
        if [ "${RB_CAPTURE_TOOL_USE_SCREENSHOTS:-false}" = "true" ]; then
            chown "$RB_AGENT_USER:$RB_AGENT_USER" \
                "$RUNTIME_DIR/tool_use_screenshots" || return "$RC_INFRA"
            chmod 700 "$RUNTIME_DIR/tool_use_screenshots"
        fi
    fi

    local _cc_rc=0
    case "$AGENT_CLI" in
      codex*)
        setup_codex || return "$RC_INFRA"
        ;;
      *) ;;
    esac
    echo "==> shared ${AGENT_CLI} invocation [wall-clock timeout=${RECREATION_TIMEOUT}s]"
    "${_AGENT_RUNAS[@]}" env "${_AGENT_ENV[@]}" \
        "PYTHONPATH=$_rb_invocation_scripts" python3 -m core.agent_invocation \
        run --spec "$_invocation_spec" || _cc_rc=$?
    local _cc_verdict _cc_verdict_rc
    _cc_verdict="$(PYTHONPATH="$_rb_invocation_scripts" python3 -m core.agent_invocation terminal \
        --trajectory "$RUNTIME_DIR/trajectory.jsonl" --native-rc "$_cc_rc" \
        --field status 2>/dev/null || echo infra_error)"
    _cc_verdict_rc="$(PYTHONPATH="$_rb_invocation_scripts" python3 -m core.agent_invocation terminal \
        --trajectory "$RUNTIME_DIR/trajectory.jsonl" --native-rc "$_cc_rc" \
        --field exit-code 2>/dev/null || echo 2)"
    explain_cc_exit recreation "$RUNTIME_DIR/trajectory.jsonl" "$_cc_rc" \
        "$(( $(date +%s) - _CC_T0 ))" "$RECREATION_TIMEOUT"
    # timeout/CLI exit only accounts for the foreground process.  An agent can leave build
    # daemons behind, so stop every process owned by the dedicated principal before trusting a
    # byte of its workspace or transcript.
    stop_dedicated_agent_processes "$RB_AGENT_USER" || return "$RC_INFRA"
    if [ -s "$OUTPUT_APK" ] || [ -n "$(find "$CODE_DIR" -name '*.apk' -type f 2>/dev/null | head -1)" ]; then
        echo "==> recreation produced an APK"
    else
        echo "WARN: no APK produced (retry disabled; will fail verify)"
    fi
    collect_claude_session "$RUNTIME_DIR/sessions" "$_CC_T0" recreation \
        "$RUNTIME_DIR/trajectory.jsonl"
    mark_agent_ran recreation

    # Revoke the dedicated principal before copying.  Besides preventing later path-based opens,
    # this makes the copied artifact root-owned instead of preserving rbagent ownership via cp -a.
    chown -R root:root "$WORKSPACE_DIR" "$RUNTIME_DIR" || return "$RC_INFRA"
    chmod -R u+rwX,go-rwx "$WORKSPACE_DIR" "$RUNTIME_DIR" || return "$RC_INFRA"

    # Freeze into a protected temporary sibling and publish by rename, so readers never observe a
    # partially copied tree.  Only this stage-owned snapshot is evaluated and uploaded.
    local SNAPSHOT_DIR="${WORKSPACE_STORAGE_DIR}.snapshot.$$"
    rm -rf "$SNAPSHOT_DIR"
    mkdir -p "$SNAPSHOT_DIR"
    cp -a "$WORKSPACE_DIR/." "$SNAPSHOT_DIR/" || {
        echo "ERROR: could not snapshot canonical recreation workspace" >&2
        rm -rf "$SNAPSHOT_DIR"
        return "$RC_INFRA"
    }
    rm -rf "$WORKSPACE_STORAGE_DIR"
    mv "$SNAPSHOT_DIR" "$WORKSPACE_STORAGE_DIR" || return "$RC_INFRA"

    # Runtime logs also lived at a task-free path while the agent ran.  Merge them only after the
    # process boundary is frozen; REC_DIR remains protected from the agent throughout.
    cp -a "$RUNTIME_DIR/." "$REC_DIR/" || {
        echo "ERROR: could not collect recreation runtime output" >&2
        return "$RC_INFRA"
    }
    chown -R root:root "$REC_DIR" 2>/dev/null || true

    # 即时上传中间产物（log / trajectory / sessions / prompt）到 artifact store：
    # 若后续 APK 收集失败或 pod 中断，仍能拿到过程日志用于排查。
    # 非致命：未配置产物存储或上传失败都不阻断后续 APK 阶段。
    if artifact_upload_enabled; then
        artifact_upload_dir "$REC_DIR" "$RECREATION_STAGE" || \
            echo "WARN: intermediate REC_DIR upload failed (non-fatal)"
    fi

    # 收集复刻 APK：规范输出已直接位于 recreation/；回退工程内的构建产物。
    local RECREATED_APK="$REC_DIR/recreation/recreated.apk"
    if [ ! -s "$OUTPUT_APK" ]; then
        local BUILT_APK
        BUILT_APK="$(find "$CODE_DIR" -name '*.apk' -type f 2>/dev/null | head -1 || true)"
        [ -n "$BUILT_APK" ] && cp -f "$BUILT_APK" "$RECREATED_APK"
    fi
    [ -s "$RECREATED_APK" ] && echo "==> Recreated APK collected: $RECREATED_APK"

    # External termination always wins.  Defer other terminal errors until the
    # frozen output has been checked: a final API/stream failure can occur after
    # the model already produced a valid APK, which should be evaluated rather
    # than discarded as a false infra retry.
    echo "==> agent verdict: $_cc_verdict (native exit=$_cc_rc)"
    [ "$_cc_verdict_rc" = 143 ] && return "$RC_TIMEOUT"

    if [ "$_cc_verdict_rc" = 2 ]; then
        if [ -s "$RECREATED_APK" ] && apk_package_name "$RECREATED_APK" >/dev/null 2>&1; then
            echo "WARN: terminal agent error occurred after a valid APK was produced; continuing to eval"
        else
            return "$RC_INFRA"
        fi
    fi

    # The agent can write the recreation tree, so replace the provisional pointer after it exits.
    # This makes the harness, not authored content, authoritative for the eval handoff.
    if [ -n "${_ra_common:-}" ]; then
        PYTHONPATH="$(dirname "${_ra_common}")" python3 -m core.recreation_artifact write \
            --dest "$REC_DIR/recreation" --platform android --root . \
            --entry recreated.apk 2>&1 || echo "WARN: final recreation manifest not written"
    fi

    # 即时上传复刻 APK（不等 verify）
    artifact_upload_file "$RECREATED_APK" "$RECREATION_STAGE"

    echo "Recreation stage complete"
}
verify_recreation() {
    local RECREATED_APK="$REC_DIR/recreation/recreated.apk"
    if [ ! -s "$RECREATED_APK" ]; then
        echo "Recreation verify: FAIL (no recreated.apk)"
        return 1
    fi
    if ! apk_package_name "$RECREATED_APK" >/dev/null 2>&1; then
        echo "Recreation verify: FAIL (recreated.apk not parseable)"
        diagnose_bad_apk "$RECREATED_APK"
        return 1
    fi
    echo "Recreation verify: PASS"
    return 0
}
stage_eval() {
    echo ""
    echo "=========================================="
    echo "=== Stage: EVAL"
    echo "=========================================="

    prepare_eval_runtime_assets || return "$RC_INFRA"
    local CANDIDATE_APK
    case "${RB_EVAL_TARGET:-recreation}" in
        recreation)
            CANDIDATE_APK="$REC_DIR/recreation/recreated.apk"
            ;;
        reference)
            CANDIDATE_APK="$REFERENCE_DIR/clean.apk"
            ;;
        *)
            echo "ERROR: unsupported RB_EVAL_TARGET=${RB_EVAL_TARGET:-}" >&2
            return "$RC_INFRA"
            ;;
    esac
    if [ ! -s "$CANDIDATE_APK" ]; then
        echo "ERROR: ${RB_EVAL_TARGET:-recreation} candidate APK not found at $CANDIDATE_APK"
        return "$RC_DATA"
    fi
    echo "==> Eval target: ${RB_EVAL_TARGET:-recreation} ($CANDIDATE_APK)"

    local TV="${APP_DIR}/test_verify.sh"
    if [ ! -f "$TV" ]; then
        echo "ERROR: test_verify.sh not found at $TV"
        return "$RC_INFRA"
    fi

    echo "==> Running test_verify.sh ..."
    if ! DEVICE_SERIAL="${DEVICE_ID}" bash "$TV" \
        --recreated-apk "$CANDIDATE_APK" \
        --eval-dir "$TESTS_DIR" \
        --meta "$REFERENCE_DIR/original_meta.env" \
        --output-dir "$EVAL_DIR" 2>&1 | tee "$EVAL_DIR/eval.log"; then
        echo "ERROR: frozen-suite evaluation failed"
        return "$RC_INFRA"
    fi

    echo "Eval stage complete"
    echo "metrics.json: $(cat "$EVAL_DIR/metrics.json" 2>/dev/null || echo 'N/A')"
}
_run_single_stage() {
    local s="$1" rc=0
    bootstrap_android_build_env || {
        stage_status_set "$s" failed
        echo "${s^^} ANDROID STARTUP BOOTSTRAP FAILED"
        return "$RC_INFRA"
    }
    download_stage_deps "$s" || {
        rc=$?
        stage_status_set "$s" failed
        echo "${s^^} DEPENDENCY PREPARATION FAILED (exit $rc)"
        return "$rc"
    }
    case "$s" in
        setup)
            # No model, no emulator, no testcases: the boundary is the whole deliverable, so a
            # failure here is infra by definition.
            stage_setup || { stage_status_set setup failed; echo "SETUP FAILED"; return "$RC_INFRA"; }
            stage_status_set setup ok
            ;;
        recreation)
            # A model that cannot rebuild the app is a RESULT, not a failure of ours.
            # Marking it exit 1 would make the batch runner retry a model limitation
            # until it exhausts its budget. Record it and stop the sequence — eval has
            # nothing to score — but let the job exit 0.
            stage_recreation || rc=$?
            if [ "$rc" -ne "$RC_OK" ]; then
                stage_status_set recreation failed
                echo "RECREATION INFRASTRUCTURE FAILED (exit $rc)"
                upload_stage_outputs "$s" || echo "WARN: ${s^^} upload failed after recreation infra failure"
                return "$rc"
            fi
            if ! verify_recreation; then
                stage_status_set recreation failed
                echo "RECREATION PRODUCED NO USABLE APK (recorded as a result, not a pipeline failure)"
                upload_stage_outputs "$s" || echo "WARN: ${s^^} upload failed after a failed recreation"
                PIPELINE_STOP=1
                return "$RC_OK"
            fi
            stage_status_set recreation ok
            ;;
        eval)
            # Upload even on failure: metrics.json and eval.log are exactly what
            # diagnosing the failure needs.
            stage_eval || rc=$?
            if [ "$rc" -eq "$RC_OK" ]; then
                stage_status_set eval ok
            else
                stage_status_set eval failed
                echo "EVAL FAILED (exit $rc)"
            fi
            ;;
        *)
            echo "ERROR: unknown stage '$s'"; return "$RC_INFRA"
            ;;
    esac
    # 上传是**记账**，不是这个阶段做成了什么。一次 503 限流曾把「APK 已产出且 verify PASS」
    # 的 recreation 写成 stages.recreation=failed + exit 2 + score 0。所以这里只警告：
    # 关键产物各自有一次带重试的即时上传，全量产物在平台 artifact 归档里，而
    # gates.*_done 每轮从 artifact store 现场重判 —— 真没传上去，下一轮自然会重跑。
    local up=0
    upload_stage_outputs "$s" || up=$?
    if [ "$up" -ne 0 ]; then
        echo "WARN: ${s^^} ARTIFACT FLUSH INCOMPLETE (artifact_xfer=$up) — stage status unchanged;" \
             "artifacts are still in the platform archive"
    fi
    return $rc
}
