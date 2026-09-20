#!/usr/bin/env bash
# Evaluate one recreated Android APK against the frozen unified test suite.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_PY="$SCRIPT_DIR/runtime.py"

RECREATED_APK=""
EVAL_DIR=""
OUTPUT_DIR=""
META_FILE=""
PACKAGE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --recreated-apk) RECREATED_APK="$2"; shift 2 ;;
    --eval-dir)      EVAL_DIR="$2"; shift 2 ;;
    --output-dir)    OUTPUT_DIR="$2"; shift 2 ;;
    --meta)          META_FILE="$2"; shift 2 ;;
    --package)       PACKAGE="$2"; shift 2 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$OUTPUT_DIR" ] || { echo "ERROR: --output-dir is required" >&2; exit 2; }
mkdir -p "$OUTPUT_DIR"
METRICS_FILE="$OUTPUT_DIR/metrics.json"
RESULTS_DIR="$OUTPUT_DIR/eval_results"
mkdir -p "$RESULTS_DIR"

if [ -z "$EVAL_DIR" ] || [ ! -d "$EVAL_DIR" ]; then
  echo "ERROR: frozen tests directory not found: $EVAL_DIR" >&2
  exit 2
fi
if [ ! -s "$EVAL_DIR/test_manifest.json" ]; then
  echo "ERROR: frozen tests are missing test_manifest.json" >&2
  exit 2
fi
if [ ! -s "$EVAL_DIR/android_testgen_kit.py" ]; then
  echo "ERROR: frozen tests are missing android_testgen_kit.py" >&2
  exit 2
fi
if [ ! -s "$RUNTIME_PY" ]; then
  echo "ERROR: Android evaluator runtime is missing: $RUNTIME_PY" >&2
  exit 2
fi

export ADB_PATH="${ADB_PATH:-$(command -v adb || echo adb)}"
export DEVICE_SERIAL="${DEVICE_SERIAL:-$(adb get-serialno 2>/dev/null | tr -d '\r')}"
if [ -z "$DEVICE_SERIAL" ] || [ "$DEVICE_SERIAL" = "unknown" ]; then
  DEVICE_SERIAL="$(adb devices | awk 'NR>1 && $2=="device"{print $1; exit}')"
  export DEVICE_SERIAL
fi
echo "==> ADB_PATH=$ADB_PATH, DEVICE_SERIAL=$DEVICE_SERIAL"


write_fail_metrics() {
  local reason="$1"
  "${PYTHON_BIN:-python3}" "$RUNTIME_PY" write-failure \
    --manifest "$EVAL_DIR/test_manifest.json" \
    --metrics "$METRICS_FILE" --reason "$reason" >/dev/null
  echo "==> metrics.json (FAIL): $(cat "$METRICS_FILE")"
}

prepare_special_app_access() {
  local apk="$1" package="$2" permissions
  permissions="$(aapt dump permissions "$apk" 2>/dev/null || true)"

  # `adb install -g` grants runtime permissions only.  Android 12+ keeps exact
  # alarms behind a special app-op; apps which request it can otherwise open
  # Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM on every launch.  The frozen
  # tests then observe the system "Alarms & reminders" screen instead of the
  # candidate app.  Normalize the declared capability before any testcase runs,
  # just as `-g` normalizes ordinary runtime permissions.
  if printf '%s\n' "$permissions" | grep -q "android.permission.SCHEDULE_EXACT_ALARM"; then
    echo "==> Granting declared special app access: SCHEDULE_EXACT_ALARM"
    if ! adb -s "$DEVICE_SERIAL" shell appops set --uid "$package" \
        SCHEDULE_EXACT_ALARM allow >/dev/null 2>&1; then
      echo "ERROR: could not grant SCHEDULE_EXACT_ALARM app-op to $package" >&2
      return 1
    fi
    if ! adb -s "$DEVICE_SERIAL" shell appops get "$package" \
        SCHEDULE_EXACT_ALARM 2>/dev/null | grep -q 'allow'; then
      echo "ERROR: SCHEDULE_EXACT_ALARM app-op did not become allowed for $package" >&2
      return 1
    fi
  fi
}

configure_frozen_device_date() {
  local manifest="$1" requested encoded observed
  requested="$(${PYTHON_BIN:-python3} "$RUNTIME_PY" device-date \
    "$manifest" --encoded)" || {
    echo "ERROR: invalid device_date in frozen test manifest" >&2
    return 1
  }
  [ -n "$requested" ] || return 0
  encoded="$requested"

  adb -s "$DEVICE_SERIAL" shell settings put global auto_time 0 >/dev/null 2>&1 || true
  if ! adb -s "$DEVICE_SERIAL" shell date "$encoded" >/dev/null 2>&1; then
    if ! adb -s "$DEVICE_SERIAL" shell su 0 date "$encoded" >/dev/null 2>&1; then
      echo "ERROR: could not apply frozen device_date from test manifest" >&2
      return 1
    fi
  fi
  observed="$(adb -s "$DEVICE_SERIAL" shell date +%Y-%m-%d 2>/dev/null | tr -d '\r')"
  requested="$(${PYTHON_BIN:-python3} "$RUNTIME_PY" device-date "$manifest")"
  if [ "$observed" != "$requested" ]; then
    echo "ERROR: frozen device_date did not stick (want=$requested got=$observed)" >&2
    return 1
  fi
  echo "==> Frozen device date: $observed"
}

reset_eval_module_state() {
  local notification_key

  # Android test modules are independent evaluator lanes, but they share one
  # emulator.  Re-anchor the frozen clock before every lane so alarms created by
  # an earlier lane cannot become due while a later lane is navigating.  Etar,
  # for example, creates 12:30 events; a single clock setup at the beginning of
  # an hour-long evaluation let their heads-up notifications cover the drawer.
  configure_frozen_device_date "$EVAL_DIR/test_manifest.json" || return 1

  # Remove any already-visible notification from this candidate without
  # touching system notifications or changing the app's notification setting.
  # The shell API has no cancel subcommand on the benchmark emulator, but snooze
  # is scoped by the exact notification key and collapses an active heads-up.
  while IFS= read -r notification_key; do
    case "$notification_key" in
      *"|$PACKAGE|"*)
        adb -s "$DEVICE_SERIAL" shell cmd notification snooze \
          --for 86400000 "$notification_key" >/dev/null 2>&1 || true
        ;;
    esac
  done < <(adb -s "$DEVICE_SERIAL" shell cmd notification list 2>/dev/null || true)
  adb -s "$DEVICE_SERIAL" shell cmd statusbar collapse >/dev/null 2>&1 || true
}

echo "=== [eval 1/3] validate recreated APK ==="
if [ -z "$RECREATED_APK" ] || [ ! -s "$RECREATED_APK" ]; then
  write_fail_metrics "recreated apk not found"
  exit 0
fi
if [ -z "$PACKAGE" ]; then
  PACKAGE="$(aapt dump badging "$RECREATED_APK" 2>/dev/null | sed -n "s/.*package: name='\([^']*\)'.*/\1/p" | head -1)"
fi
if [ -z "$PACKAGE" ]; then
  write_fail_metrics "invalid apk: cannot parse package"
  exit 0
fi

echo "=== [eval 2/3] install recreated APK ==="
if [ -n "$META_FILE" ] && [ -f "$META_FILE" ]; then
  # shellcheck disable=SC1090
  source "$META_FILE"
  [ -n "${ORIG_PACKAGE:-}" ] && adb uninstall "$ORIG_PACKAGE" >/dev/null 2>&1 || true
fi
adb uninstall "$PACKAGE" >/dev/null 2>&1 || true
if ! adb install -t --bypass-low-target-sdk-block -r -g "$RECREATED_APK" \
    2>&1 | tee "$OUTPUT_DIR/install.log" | grep -q "Success"; then
  write_fail_metrics "apk install failed"
  exit 0
fi
if ! prepare_special_app_access "$RECREATED_APK" "$PACKAGE"; then
  echo "ERROR: candidate special-app-access setup failed" >&2
  exit 2
fi
if ! configure_frozen_device_date "$EVAL_DIR/test_manifest.json"; then
  exit 2
fi

echo "=== [eval 3/3] run frozen tests ==="
PY_BIN="${PYTHON_BIN:-python3}"
# The suite is frozen for assertions, not for judge implementation. Wire only this downloaded
# eval copy to the same vlm_judge.py used by the other four platforms; the artifact store suite and its
# assertion/denominator definitions remain untouched.
if [ ! -s "$SCRIPT_DIR/vlm_judge.py" ]; then
  echo "ERROR: shared VLM judge is missing: $SCRIPT_DIR/vlm_judge.py" >&2
  exit 2
fi
if [ ! -s "$SCRIPT_DIR/reference_vlm_batch.py" ]; then
  echo "ERROR: Android reference VLM batcher is missing: $SCRIPT_DIR/reference_vlm_batch.py" >&2
  exit 2
fi
cp "$SCRIPT_DIR/vlm_judge.py" "$SCRIPT_DIR/reference_vlm_batch.py" "$EVAL_DIR/" || {
  echo "ERROR: could not install shared VLM runtime into eval directory" >&2
  exit 2
}
if ! "$PY_BIN" "$SCRIPT_DIR/normalize_frozen_vlm.py" \
    "$EVAL_DIR/android_testgen_kit.py"; then
  echo "ERROR: frozen Android VLM transport could not be normalized" >&2
  exit 2
fi
shopt -s nullglob
EVAL_SCRIPTS=("$EVAL_DIR"/test_android_*.py)
if [ ${#EVAL_SCRIPTS[@]} -eq 0 ]; then
  echo "ERROR: unified suite contains no test_android_*.py" >&2
  exit 2
fi

if ! "$PY_BIN" -c 'import pytest; from PIL import Image' >/dev/null 2>&1; then
  echo "ERROR: evaluator Python dependencies were not installed by startup bootstrap" >&2
  exit 2
fi

export RESULTS_DIR DEVICE_SERIAL
for script in "${EVAL_SCRIPTS[@]}"; do
  name="$(basename "$script")"
  if ! reset_eval_module_state; then
    echo "ERROR: could not reset frozen Android state before $name" >&2
    exit 2
  fi
  echo "==> running $name"
  "$PY_BIN" "$script" "$PACKAGE" "$RESULTS_DIR" \
    2>&1 | tee "$OUTPUT_DIR/$name.log" ||
    echo "WARN: $name exited non-zero"
done
shopt -u nullglob

"$PY_BIN" "$RUNTIME_PY" aggregate \
  --results-dir "$RESULTS_DIR" --metrics "$METRICS_FILE" \
  --package "$PACKAGE" --tests-dir "$EVAL_DIR"

# A judge outage is infrastructure, not evidence that the recreation failed its visual
# assertions.  Frozen kits record that condition in each result, but historically the wrapper
# collapsed it into an ordinary 0/N score and returned success.  Fail closed while preserving
# metrics.json for diagnosis; the platform adapter will mark eval failed and deployment platform will not accept
# the run as a healthy canary/full result.
VLM_JUDGE_ERRORS="$(grep -h -E \
  'setup_failed: (VLM API call failed|ambiguous VLM response)' \
  "$OUTPUT_DIR"/test_android_*.py.log 2>/dev/null | wc -l | tr -d ' ')"
if [ "${VLM_JUDGE_ERRORS:-0}" -gt 0 ]; then
  "$PY_BIN" "$RUNTIME_PY" mark-vlm-errors \
    --metrics "$METRICS_FILE" --count "$VLM_JUDGE_ERRORS"
  echo "ERROR: VLM judge failed for $VLM_JUDGE_ERRORS assertion(s)" >&2
  exit 3
fi
