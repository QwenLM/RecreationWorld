#!/bin/bash
# Read-only preflight for a prepared macOS environment.
set -euo pipefail

missing=0
check() {
    if command -v "$1" >/dev/null 2>&1; then
        printf 'ok %s=%s\n' "$1" "$(command -v "$1")"
    else
        printf 'missing %s\n' "$1" >&2
        missing=1
    fi
}

check_python_package() {
    if python3 -m pip show "$1" >/dev/null 2>&1; then
        printf 'ok python-package=%s\n' "$1"
    else
        printf 'missing python-package=%s\n' "$1" >&2
        missing=1
    fi
}

printf 'macos=%s\n' "$(sw_vers -productVersion 2>/dev/null || echo unknown)"
printf 'console_user=%s\n' "$(stat -f '%Su' /dev/console 2>/dev/null || echo unknown)"
pgrep -x WindowServer >/dev/null 2>&1 && echo 'ok WindowServer' || {
    echo 'missing WindowServer (the macOS GUI session is not ready)' >&2
    missing=1
}

for command in bash git python3 node npm xcodebuild codesign sqlite3 timeout; do
    check "$command"
done

for package in pytest pytest-timeout pytest-json-report PyYAML pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa pyobjc-framework-Quartz; do
    check_python_package "$package"
done

sudo -n true >/dev/null 2>&1 || echo 'warn sudo needs an interactive password; the runner supplies MACOS_PASSWORD during provisioning' >&2
exit "$missing"
