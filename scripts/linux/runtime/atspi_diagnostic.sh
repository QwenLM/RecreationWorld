#!/bin/bash
# Exercise same-user and cross-user accessibility against the recreation display.

D=/tmp/rb_atspi_diag.txt
HELPER_SOURCE="${RB_CODE_DIR:?RB_CODE_DIR is required}/scripts/linux/runtime/atspi_diagnostic.py"
HELPER=/tmp/rb_atspi_diagnostic.py
cp "$HELPER_SOURCE" "$HELPER"
chmod 644 "$HELPER"
exec > "$D" 2>&1
echo "DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY"
UUID=$(id -u user)
echo "procs: $(pgrep -a at-spi 2>/dev/null | tr '\n' '; ')"
echo "xprop: $(su - user -c "DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY xprop -root AT_SPI_BUS 2>&1")"
echo "dbus: $(su - user -c "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UUID/bus dbus-send --session --dest=org.a11y.Bus --print-reply /org/a11y/bus org.a11y.Bus.GetAddress 2>&1" | tr '\n' ' ')"
ATSPI=$(su - user -c "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UUID/bus dbus-send --session --dest=org.a11y.Bus --print-reply /org/a11y/bus org.a11y.Bus.GetAddress 2>/dev/null" | grep string | head -1 | sed 's/.*string "\(.*\)"/\1/')

nohup su - user -c "DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY GTK_MODULES=gail:atk-bridge python3 $HELPER create-window --title SAMEUSER --label SU --seconds 20" >/dev/null 2>&1 &
nohup su - ref_user -c "DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UUID/bus AT_SPI_BUS_ADDRESS=$ATSPI GTK_MODULES=gail:atk-bridge python3 $HELPER create-window --title CROSSUSER --label CU --seconds 20" >/dev/null 2>&1 &
sleep 5
su - user -c "DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UUID/bus python3 $HELPER list-apps --delay 1" 2>&1

CWIN=$(su user -c "DISPLAY=$DISPLAY cua-driver call list_windows '{}'" 2>/dev/null)
CUA_PID=$(printf '%s\n' "$CWIN" | python3 "$HELPER" find-window 2>/dev/null | head -1)
if [ -n "$CUA_PID" ]; then
    CP=$(echo "$CUA_PID" | awk '{print $1}')
    CW=$(echo "$CUA_PID" | awk '{print $2}')
    echo "{\"pid\":$CP,\"window_id\":$CW,\"capture_mode\":\"ax\"}" > /tmp/cua_p.json
    su user -c "DISPLAY=$DISPLAY cat /tmp/cua_p.json | cua-driver call get_window_state" 2>/dev/null > /tmp/cua_r.json
    python3 "$HELPER" summarize-cua /tmp/cua_r.json 2>&1
fi
pkill -f "SAMEUSER\|CROSSUSER" 2>/dev/null || true
