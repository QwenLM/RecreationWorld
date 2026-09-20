#!/bin/bash
# grant_tcc_permissions.sh — Pre-authorize macOS TCC permissions for an app
#
# Handles unsigned/ad-hoc apps by codesigning them first, extracting the csreq
# blob, and inserting TCC entries with proper code identity. Also grants
# AppleEvents to common target apps and fixes any previously denied entries.
#
# Usage: ./grant_tcc_permissions.sh /path/to/App.app
#        ./grant_tcc_permissions.sh --bundle-id com.example.app
#        ./grant_tcc_permissions.sh --bundle-id com.example.app --app-path /path/to/App.app
set -euo pipefail

APP_PATH=""
BUNDLE_ID=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bundle-id)
            BUNDLE_ID="${2:-}"
            shift 2
            ;;
        --app-path)
            APP_PATH="${2:-}"
            shift 2
            ;;
        *)
            if [[ -z "$APP_PATH" ]]; then
                APP_PATH="$1"
            fi
            shift
            ;;
    esac
done

if [[ -z "$BUNDLE_ID" && -z "$APP_PATH" ]]; then
    echo "Usage: $0 /path/to/App.app"
    echo "       $0 --bundle-id com.example.app [--app-path /path/to/App.app]"
    exit 1
fi

if [[ -z "$BUNDLE_ID" && -n "$APP_PATH" ]]; then
    BUNDLE_ID=$(defaults read "$APP_PATH/Contents/Info.plist" CFBundleIdentifier 2>/dev/null || echo "")
    if [[ -z "$BUNDLE_ID" ]]; then
        echo "ERROR: Cannot read bundle ID from $APP_PATH"
        exit 1
    fi
fi

TCC_DB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"

if [[ ! -f "$TCC_DB" ]]; then
    echo "ERROR: TCC database not found at $TCC_DB"
    exit 1
fi

echo "Granting TCC permissions for: $BUNDLE_ID"

# ── 1. Ensure app is code-signed (ad-hoc if needed) ─────────────────────
CSREQ_HEX=""
if [[ -n "$APP_PATH" && -d "$APP_PATH" ]]; then
    if ! codesign -v "$APP_PATH" 2>/dev/null; then
        echo "  App is unsigned — applying ad-hoc signature..."
        codesign --force --deep --sign - "$APP_PATH" 2>/dev/null || true
    fi

    REQ_LINE=$(codesign -d -r- "$APP_PATH" 2>&1 | grep 'designated =>' | head -1 || true)
    if [[ -n "$REQ_LINE" ]]; then
        REQ=$(echo "$REQ_LINE" | sed 's/.*designated => //')
        if [[ -n "$REQ" ]]; then
            CSREQ_HEX=$(echo "$REQ" | csreq -r- -b /dev/stdout 2>/dev/null | xxd -p | tr -d '\n' || true)
        fi
    fi

    if [[ -n "$CSREQ_HEX" ]]; then
        echo "  Extracted csreq (${#CSREQ_HEX} hex chars)"
    else
        echo "  WARNING: Could not extract csreq — TCC entries may not suppress all dialogs"
    fi
fi

# ── 2. Grant standard TCC services ──────────────────────────────────────
SERVICES=(
    kTCCServiceAccessibility
    kTCCServiceScreenCapture
    kTCCServiceAppleEvents
    kTCCServicePostEvent
    kTCCServiceListenEvent
    kTCCServiceSystemPolicyAllFiles
    kTCCServiceDeveloperTool
    kTCCServiceCalendar
    kTCCServiceAddressBook
    kTCCServiceContactsFull
    kTCCServiceContactsLimited
    kTCCServiceReminders
    kTCCServicePhotos
    kTCCServicePhotosAdd
    kTCCServiceCamera
    kTCCServiceMicrophone
    kTCCServiceBluetoothAlways
    kTCCServiceMediaLibrary
    kTCCServiceSpeechRecognition
    kTCCServiceMotion
    kTCCServiceLocation
    kTCCServiceFocusStatus
    kTCCServiceSystemPolicyDesktopFolder
    kTCCServiceSystemPolicyDocumentsFolder
    kTCCServiceSystemPolicyDownloadsFolder
    kTCCServiceSystemPolicyNetworkVolumes
    kTCCServiceSystemPolicyRemovableVolumes
    kTCCServiceFileProviderDomain
    kTCCServiceFileProviderPresence
)

for service in "${SERVICES[@]}"; do
    if [[ -n "$CSREQ_HEX" ]]; then
        sqlite3 "$TCC_DB" "INSERT OR REPLACE INTO access \
            (service, client, client_type, auth_value, auth_reason, auth_version, csreq, indirect_object_identifier) \
            VALUES ('$service', '$BUNDLE_ID', 0, 2, 3, 1, X'$CSREQ_HEX', 'UNUSED');" 2>/dev/null && \
            echo "  $service: granted (csreq)" || \
            echo "  $service: skipped"
    else
        sqlite3 "$TCC_DB" "INSERT OR REPLACE INTO access \
            (service, client, client_type, auth_value, auth_reason, auth_version, indirect_object_identifier) \
            VALUES ('$service', '$BUNDLE_ID', 0, 2, 3, 1, 'UNUSED');" 2>/dev/null && \
            echo "  $service: granted" || \
            echo "  $service: skipped"
    fi
done

# ── 3. Fix any DENIED entries (auth_value=0 → 2) ────────────────────────
DENIED_COUNT=$(sqlite3 "$TCC_DB" "SELECT COUNT(*) FROM access WHERE client='$BUNDLE_ID' AND auth_value=0;" 2>/dev/null || echo "0")
if [[ "$DENIED_COUNT" -gt 0 ]]; then
    sqlite3 "$TCC_DB" "UPDATE access SET auth_value=2 WHERE client='$BUNDLE_ID' AND auth_value=0;" 2>/dev/null || true
    echo "  Fixed $DENIED_COUNT previously denied entries"
fi

# ── 4. Grant AppleEvents to common target apps ──────────────────────────
APPLE_EVENT_TARGETS=(
    com.apple.iCal
    com.apple.systemevents
    com.apple.finder
    com.apple.Safari
)
for target in "${APPLE_EVENT_TARGETS[@]}"; do
    if [[ -n "$CSREQ_HEX" ]]; then
        sqlite3 "$TCC_DB" "INSERT OR REPLACE INTO access \
            (service, client, client_type, auth_value, auth_reason, auth_version, csreq, \
             indirect_object_identifier_type, indirect_object_identifier) \
            VALUES ('kTCCServiceAppleEvents', '$BUNDLE_ID', 0, 2, 3, 1, X'$CSREQ_HEX', \
                    0, '$target');" 2>/dev/null && \
            echo "  AppleEvents → $target: granted" || true
    else
        sqlite3 "$TCC_DB" "INSERT OR REPLACE INTO access \
            (service, client, client_type, auth_value, auth_reason, auth_version, \
             indirect_object_identifier_type, indirect_object_identifier) \
            VALUES ('kTCCServiceAppleEvents', '$BUNDLE_ID', 0, 2, 3, 1, \
                    0, '$target');" 2>/dev/null && \
            echo "  AppleEvents → $target: granted" || true
    fi
done

echo "Done."
