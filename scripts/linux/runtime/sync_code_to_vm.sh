
set -eu
ARCHIVE=__REMOTE_ARCHIVE__
DEST=__VM_CODE_DIR__
AGENT_USER=__AGENT_USER__
IFS= read -r RB_SYNC_SUDO_PASSWORD || true
cleanup() { rm -f "$ARCHIVE"; }
trap cleanup EXIT
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif [ -n "$RB_SYNC_SUDO_PASSWORD" ]; then
        printf '%s\n' "$RB_SYNC_SUDO_PASSWORD" | sudo -S -p '' "$@"
    else
        sudo -n "$@"
    fi
}
as_root rm -rf "$DEST"
as_root mkdir -p "$DEST"
as_root tar xzf "$ARCHIVE" -C "$DEST"
as_root chown -R root:root "$DEST"
as_root chmod -R go-rwx "$DEST"
as_root chmod 0700 "$DEST"
HELPER="$DEST/scripts/core/reference_source.py"
as_root test -r "$HELPER"
if as_root runuser -u "$AGENT_USER" -- test -r "$HELPER"; then
    echo "ERROR: recreation agent can read protected RB code: $HELPER" >&2
    exit 1
fi
echo "RB code synced and isolated at $DEST"
