#!/bin/bash
set -e
BASE="__BASE__"
VARIANT="__VARIANT__"
RB_CODE_DIR="__RB_CODE_DIR__"

# Elevate to root (build.sh may need apt-get)
if [ "$(id -u)" -ne 0 ]; then
    if sudo -n true 2>/dev/null; then
        exec sudo -E bash "$0" "$@"
    elif [ -n "${RB_SUDO_PASSWORD:-}" ]; then
        echo "$RB_SUDO_PASSWORD" | sudo -S -E bash "$0" "$@"
        exit $?
    fi
fi

TESTS_DIR="$BASE/tests"
RESULTS_DIR="$BASE/atspi_eval___VARIANT__"
EVAL_RUN_USER="__EVAL_RUN_USER__"
EVAL_CANDIDATE_DIR="__EVAL_CANDIDATE_DIR__"
EVAL_RESULTS_STAGING=""

mkdir -p "$RESULTS_DIR"
RESULTS_OWNER="$(stat -c '%u:%g' "$RESULTS_DIR" 2>/dev/null || true)"

# Frozen inputs are root-only while the recreation agent is active. Phase 1
# runs afterwards and copies the suite into RESULTS_DIR as root. Return those
# artifacts to the SSH caller on every exit: Phase 2 must overwrite the
# candidate-authored assertions map with the frozen manifest before invoking
# the VLM judge, and the later SFTP collector must be able to read everything.
# The original frozen tree remains root-only and is not touched here.
make_results_available() {
    if [ -n "$EVAL_RESULTS_STAGING" ] && [ -d "$EVAL_RESULTS_STAGING" ]; then
        cp -a "$EVAL_RESULTS_STAGING/." "$RESULTS_DIR/" 2>/dev/null || true
    fi
    if [ -n "$RESULTS_OWNER" ]; then
        chown -R "$RESULTS_OWNER" "$RESULTS_DIR" 2>/dev/null || true
    fi
    chmod -R u+rwX,go+rX "$RESULTS_DIR" 2>/dev/null || true
}
trap make_results_available EXIT

# Remove stale result artifacts before this eval run
find "$RESULTS_DIR" -maxdepth 1 -type f \( \
    -name '*_results.json' -o -name '*_eval.json' -o \
    -name 'programmatic_results.json' -o -name 'vlm_results.json' -o \
    -name 'vlm_results.partial.json' -o -name 'atspi_eval.json' \
\) -delete 2>/dev/null || true
rm -rf "$RESULTS_DIR/screenshots" "$RESULTS_DIR/results"
mkdir -p "$RESULTS_DIR/results"

# Copy the frozen suite and helpers, but keep the current runtime runner authoritative.
# A copy failure is infrastructure failure; never turn it into an empty/partial run.
shopt -s nullglob
for _f in "$TESTS_DIR"/*.py "$TESTS_DIR"/tests/*.py; do
    [ "$(basename "$_f")" = "run_atspi_tests.py" ] && continue
    cp "$_f" "$RESULTS_DIR/"
done
shopt -u nullglob
TEST_COUNT=$(ls "$RESULTS_DIR"/test_atspi_*.py 2>/dev/null | wc -l || echo 0)
echo "Test files: $TEST_COUNT"

# Copy fixtures
rm -rf "$RESULTS_DIR/fixtures"
mkdir -p "$RESULTS_DIR/fixtures"
if [ -d "$TESTS_DIR/fixtures" ]; then
    cp -a "$TESTS_DIR/fixtures/." "$RESULTS_DIR/fixtures/"
fi
echo "Fixtures: $(find "$RESULTS_DIR/fixtures" -type f 2>/dev/null | wc -l)"

if [ "$TEST_COUNT" -eq 0 ]; then
    echo '{"error": "no test files"}' > "$RESULTS_DIR/atspi_eval.json"
    exit 1
fi

# ── Set up /workspace paths ──
mkdir -p /workspace
rm -rf /workspace/eval_results /workspace/frozen_tests
ln -sfn "$TESTS_DIR" /workspace/frozen_tests
__WORKSPACE_CLEANUP__

# Preserve the legacy physical output layout for every evaluation mode.  Frozen
# suites predate /workspace/eval_results and some intentionally pass paths below
# /workspace/output/fixtures to the application, whose UI then exposes that
# canonical path.  A symlink in the opposite direction is insufficient for
# those assertions, so stage the suite in a real /workspace/output directory
# and copy generated results back to RESULTS_DIR from the EXIT trap.
rm -rf /workspace/eval_results /workspace/output
mkdir -p /workspace/output
cp -a "$RESULTS_DIR/." /workspace/output/
ln -sfn /workspace/output /workspace/eval_results
EVAL_RESULTS_STAGING=/workspace/output
__WORKSPACE_SETUP__

# ── Source bootstrap env ──
[ -f /tmp/.rb_bootstrap_env ] && . /tmp/.rb_bootstrap_env
export DISPLAY="${DISPLAY:-:99}"
export GTK_MODULES=gail:atk-bridge
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
export QT_ACCESSIBILITY=1
export ELECTRON_ENABLE_ACCESSIBILITY=1
export EVAL_APP_NAME="__APP_NAME__"
export FIXTURES_DIR="/workspace/eval_results/fixtures"

# ── Clone repo if needed ──
__CLONE_CMD__

# ── Build and find launch target ──
__FIND_AND_LAUNCH__

echo "Binary: $BINARY"
if [ -z "$BINARY" ]; then
    echo '{"error": "no_binary"}' > /workspace/eval_results/atspi_eval.json
    exit 1
fi
echo "Launch: ${LAUNCH_SH:-}"
if [ -z "${LAUNCH_SH:-}" ] || [ ! -f "$LAUNCH_SH" ]; then
    echo '{"error": "no_launch_script"}' > /workspace/eval_results/atspi_eval.json
    exit 1
fi

# The frozen scored set is mandatory; there is no self-counted release mode.
[ -s "$TESTS_DIR/test_manifest.json" ] || { echo "ERROR: frozen test_manifest.json missing"; exit 1; }
# Keep the exact manifest used by Phase 1 beside the result files.  Phase 2 runs in a
# separate remote invocation, and relying on RESULTS_DIR/../tests made the frozen VLM
# denominator disappear in a real run even though Phase 1 had just scored against it.
# The colocated copy is also returned with the eval artifacts, making the denominator
# independently auditable after the sandbox is destroyed.
cp "$TESTS_DIR/test_manifest.json" "$RESULTS_DIR/test_manifest.json"
cp "$TESTS_DIR/test_manifest.json" /workspace/eval_results/test_manifest.json
MANIFEST_ARG="--manifest /workspace/eval_results/test_manifest.json"
echo "Scoring against frozen manifest: $TESTS_DIR/test_manifest.json"

# Eval display: reuse the native desktop (e.g. GNOME :0) from bootstrap when
# present (real-desktop fidelity + runs GNOME-dependent apps), else the Xvfb
# fallback. Must match the baseline display so the recreation is scored in the
# same environment the manifest was frozen in.
# Eval display: default to a self-contained Xvfb+dbus+at-spi stack on :110. The
# shared native :0 in the eval container does NOT reliably expose recreation apps'
# AT-SPI tree (a11y bus mismatch) and its screen-grab fails -> prog+vlm collapse
# to 0 (verified: native qdirstat 0/0 vs fresh prog 128/141 + vlm 17/31). Opt into
# the native desktop with RB_EVAL_USE_NATIVE=1 for apps needing a real GNOME
# session (e.g. spedread/keypunch); the manifest denominator is fixed either way.
EVAL_DISP="${DISPLAY:-:99}"
NATIVE_ARG=""
if [ "${RB_EVAL_USE_NATIVE:-0}" = "1" ]; then
    case "$EVAL_DISP" in
        :0|:0.*|:1|:1.*|:2|:2.*)
            NATIVE_ARG="--use-native-display"
            # A native desktop is one user session: its X server, session bus,
            # AT-SPI registry, test runner, and application must share the login
            # uid. Cross-uid socket permissions are insufficient because D-Bus
            # authenticates the peer uid (observed as 0/N no_tests_executed).
            EVAL_RUN_USER="user"
            ;;
    esac
else
    EVAL_DISP=":110"
fi
echo "[eval] display=$EVAL_DISP native=${NATIVE_ARG:-no}"
if [ -n "$EVAL_RUN_USER" ]; then
    id "$EVAL_RUN_USER" >/dev/null 2>&1 || {
        echo "ERROR: eval runtime user $EVAL_RUN_USER does not exist"
        exit 1
    }
    EVAL_RUN_HOME="$(getent passwd "$EVAL_RUN_USER" | cut -d: -f6)"
    [ -n "$EVAL_RUN_HOME" ] || EVAL_RUN_HOME="/home/$EVAL_RUN_USER"
    EVAL_RUN_UID="$(id -u "$EVAL_RUN_USER")"
    if [ -n "$NATIVE_ARG" ] && [ -d "/run/user/$EVAL_RUN_UID" ]; then
        EVAL_RUNTIME_DIR="/run/user/$EVAL_RUN_UID"
    else
        EVAL_RUNTIME_DIR="/tmp/rb-eval-runtime-$EVAL_RUN_UID"
        install -d -m 700 -o "$EVAL_RUN_USER" -g "$EVAL_RUN_USER" "$EVAL_RUNTIME_DIR"
    fi
    if [ -n "$EVAL_CANDIDATE_DIR" ]; then
        chown -R "$EVAL_RUN_USER:$EVAL_RUN_USER" "$EVAL_CANDIDATE_DIR"
    fi
    # The common setup above already staged the suite at the historical physical
    # output path.  Reference/native evaluation additionally hands ownership to
    # the desktop-session user; ordinary candidate evaluation continues as root.
    chown -R "$EVAL_RUN_USER:$EVAL_RUN_USER" /workspace/output

    EVAL_DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-}"
    EVAL_AT_SPI_BUS_ADDRESS="${AT_SPI_BUS_ADDRESS:-}"

    # Native evaluation deliberately runs as the desktop-session owner. Probe
    # both the display and accessibility buses as that exact uid before tests;
    # a reachable X socket alone does not imply a usable accessibility tree.
    if [ -n "$NATIVE_ARG" ]; then
        if ! DISPLAY="$EVAL_DISP" XAUTHORITY="${XAUTHORITY:-}" \
            xhost "+SI:localuser:$EVAL_RUN_USER" >/dev/null 2>&1; then
            echo "ERROR: cannot grant $EVAL_RUN_USER access to native display $EVAL_DISP"
            exit 1
        fi
        if ! runuser -u "$EVAL_RUN_USER" -- env \
            DISPLAY="$EVAL_DISP" XAUTHORITY="${XAUTHORITY:-}" \
            xdpyinfo -display "$EVAL_DISP" >/dev/null 2>&1; then
            echo "ERROR: native display $EVAL_DISP is not reachable by $EVAL_RUN_USER"
            exit 1
        fi
        echo "[eval] native display access granted to $EVAL_RUN_USER"

        NATIVE_SESSION_BUS="/run/user/$EVAL_RUN_UID/bus"
        if [ ! -S "$NATIVE_SESSION_BUS" ]; then
            echo "ERROR: native session bus missing: $NATIVE_SESSION_BUS"
            exit 1
        fi
        EVAL_DBUS_SESSION_BUS_ADDRESS="unix:path=$NATIVE_SESSION_BUS"
        if ! runuser -u "$EVAL_RUN_USER" -- env \
            DBUS_SESSION_BUS_ADDRESS="$EVAL_DBUS_SESSION_BUS_ADDRESS" \
            dbus-send --session --dest=org.freedesktop.DBus --print-reply \
            /org/freedesktop/DBus org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
            echo "ERROR: native session bus is not reachable by $EVAL_RUN_USER"
            exit 1
        fi
        EVAL_AT_SPI_BUS_ADDRESS="$(runuser -u "$EVAL_RUN_USER" -- env \
            DBUS_SESSION_BUS_ADDRESS="$EVAL_DBUS_SESSION_BUS_ADDRESS" \
            dbus-send --session --dest=org.a11y.Bus --print-reply \
            /org/a11y/bus org.a11y.Bus.GetAddress 2>/dev/null | \
            sed -n 's/.*string "\(.*\)"/\1/p' | head -1)"
        if [ -z "$EVAL_AT_SPI_BUS_ADDRESS" ]; then
            echo "ERROR: org.a11y.Bus is not reachable by $EVAL_RUN_USER"
            exit 1
        fi
        echo "[eval] native session and AT-SPI buses reachable by $EVAL_RUN_USER"
    fi
fi

RUNNER=(python3 /workspace/eval_results/run_atspi_tests.py \
    --launch-cmd "bash $LAUNCH_SH" \
    --reap-binary "$BINARY" \
    --app-name "${EVAL_APP_NAME:-$(basename "$BINARY")}" \
    --tests /workspace/eval_results \
    --results /workspace/eval_results \
    --fixtures /workspace/eval_results/fixtures \
    --summary programmatic_results.json \
    $MANIFEST_ARG \
    --display "$EVAL_DISP" $NATIVE_ARG \
    --screen 1920x1080x24 \
    --print-summary)

if [ -n "$EVAL_RUN_USER" ]; then
    echo "[eval] runtime_user=$EVAL_RUN_USER (runner + GUI stack + app)"
    runuser -u "$EVAL_RUN_USER" -- env \
        HOME="$EVAL_RUN_HOME" \
        PATH="$PATH" \
        DISPLAY="$EVAL_DISP" \
        XAUTHORITY="${XAUTHORITY:-}" \
        DBUS_SESSION_BUS_ADDRESS="${EVAL_DBUS_SESSION_BUS_ADDRESS:-}" \
        AT_SPI_BUS_ADDRESS="${EVAL_AT_SPI_BUS_ADDRESS:-}" \
        XDG_RUNTIME_DIR="$EVAL_RUNTIME_DIR" \
        "${RUNNER[@]}"
else
    "${RUNNER[@]}"
fi

# Count screenshots
SS_COUNT=$(ls /workspace/eval_results/screenshots/*.png 2>/dev/null | wc -l)
echo "Screenshots: $SS_COUNT"
ASSERT_COUNT=$(python3 "$RB_CODE_DIR/scripts/linux/runtime/eval_results.py" assertion-count \
    /workspace/eval_results/screenshots/assertions.json 2>/dev/null || echo 0)
echo "Assertions: $ASSERT_COUNT"

echo "=== Phase 1 Complete ==="
cat /workspace/eval_results/programmatic_results.json 2>/dev/null || echo "No programmatic results"
