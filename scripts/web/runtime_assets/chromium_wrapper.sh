#!/bin/sh
ARGS=""
for arg in "$@"; do
    case "$arg" in
        --disable-gpu|--disable-gpu-compositing|--headless|--headless=*) ;;
        *) ARGS="$ARGS $arg" ;;
    esac
done
exec google-chrome-stable \
    --no-sandbox \
    --disable-gpu \
    --disable-gpu-compositing \
    --disable-dev-shm-usage \
    --ozone-platform=x11 \
    --force-renderer-accessibility \
    --remote-debugging-port=9222 \
    --no-first-run \
    --no-default-browser-check \
    --window-size=1920,1080 \
    --user-data-dir=/tmp/cua-chrome-profile \
    $ARGS
