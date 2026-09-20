#!/bin/bash
# Execute a generated macOS launch script with the interpreter declared by its
# shebang.  Older recreations sometimes omit a shebang; keep bash as the
# compatibility fallback for those scripts.
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "usage: run_launch.sh <launch-script> [args...]" >&2
    exit 2
fi

launch_script="$1"
shift

if [ ! -f "$launch_script" ]; then
    echo "launch script not found: $launch_script" >&2
    exit 2
fi

# Run the launcher with its OWN directory as cwd.
#
# An agent-authored launch.sh is written as a pair with build.sh and refers to the product by
# relative path (`./build/App.app`, `cd src`, ...).  build.sh runs with the recreation workspace
# as cwd, so the product really is at that relative path -- but a launcher started from any other
# cwd then reports `App not found at ./build/App.app. Run build.sh first.` and the app never
# appears.  ax_eval.sh only WARNs on a launcher that exits (normal for GUI launchers), so the
# verifier goes on to score an app that was never started: a working recreation is graded 0 and
# the log is indistinguishable from a bad one.
#
# Resolve to an absolute path BEFORE the cd, because callers may pass a relative path
# (run_full.sh: `run_launch.sh ./launch.sh`).  An already-absolute path is left spelled exactly
# as the caller wrote it, so what the launcher sees in "$0" does not change.
case "$launch_script" in
    /*) : ;;
    *) launch_script="$(cd "$(dirname "$launch_script")" && pwd)/$(basename "$launch_script")" ;;
esac
cd "$(dirname "$launch_script")"

first_line="$(head -n 1 "$launch_script" 2>/dev/null || true)"
if [[ "$first_line" == '#!'* ]]; then
    chmod u+x "$launch_script"
    exec "$launch_script" "$@"
fi

exec bash "$launch_script" "$@"
