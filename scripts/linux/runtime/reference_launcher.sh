#!/bin/bash
set -e

USER_UID="${1:?reference session uid is required}"
ATSPI_ADDR="${2:-}"
REFERENCE_DISPLAY="${3:-:99}"
REFERENCE_XAUTHORITY="${4:-}"
shift 4

export HOME=/home/ref_user
export DISPLAY="$REFERENCE_DISPLAY"
if [ -n "$REFERENCE_XAUTHORITY" ]; then
    export XAUTHORITY="$REFERENCE_XAUTHORITY"
fi
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${USER_UID}/bus"
if [ -n "$ATSPI_ADDR" ]; then
    export AT_SPI_BUS_ADDRESS="$ATSPI_ADDR"
fi
export GTK_MODULES=gail:atk-bridge
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
export QT_ACCESSIBILITY=1
# su starts a login shell and drops bootstrap's software-rendering environment.
export GSK_RENDERER="${GSK_RENDERER:-cairo}"
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"
# Keep the compatibility variable, but directly invoked Electron only observes
# renderer accessibility flags delivered on its argv.
export ELECTRON_EXTRA_LAUNCH_ARGS="--force-renderer-accessibility"

rb_is_elf() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ -x "$1" ] || return 1
    head -c 4 "$1" 2>/dev/null | grep -qa ELF
}

RB_EL_FLAGS=""
if rb_is_elf /workspace/install/runtime/electron \
   || rb_is_elf /workspace/install/node_modules/electron/dist/electron \
   || [ -e /workspace/install/resources/app ] \
   || [ -e /workspace/install/resources/app.asar ] \
   || sed 's/#.*//' /workspace/install/launch.sh 2>/dev/null | grep -qiE 'electron|\.asar'; then
    RB_EL_FLAGS="--force-renderer-accessibility --no-zygote"
    echo "isolaunch: Electron reference detected; appending $RB_EL_FLAGS" >&2
fi
exec bash /workspace/install/launch.sh $RB_EL_FLAGS "$@"
