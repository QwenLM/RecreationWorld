#!/bin/bash
# Prepare a macOS environment before running RecreationBench.
set -euo pipefail

ENV_FILE="${1:?usage: setup_macos.sh /path/to/runtime_env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS_FILE="${RB_MACOS_REQUIREMENTS_FILE:-$SCRIPT_DIR/requirements.macos.txt}"
# shellcheck disable=SC1090
source "$ENV_FILE"

MACOS_DEVAGENT_USER="${MACOS_DEVAGENT_USER:-devagent}"
MACOS_DEVAGENT_PASSWORD="${MACOS_DEVAGENT_PASSWORD:-${MACOS_PASSWORD:-}}"
if [ -z "${MACOS_PASSWORD:-}" ]; then
    echo 'MACOS_PASSWORD is required for provisioning' >&2
    exit 2
fi

sudo_with_password() {
    printf '%s\n' "$MACOS_PASSWORD" | sudo -S "$@"
}

sudo_with_password -v
sudo_with_password spctl --master-disable || true
sudo_with_password pmset -a sleep 0 displaysleep 0 disksleep 0 || true
xcodebuild -license accept >/dev/null 2>&1 || true
git config --global user.name RecreationBench || true
git config --global user.email bench@recreation.test || true

if ! id "$MACOS_DEVAGENT_USER" >/dev/null 2>&1; then
    [ -n "$MACOS_DEVAGENT_PASSWORD" ] || {
        echo 'MACOS_DEVAGENT_PASSWORD is required when devagent does not exist' >&2
        exit 2
    }
    sudo_with_password sysadminctl -addUser "$MACOS_DEVAGENT_USER" -password "$MACOS_DEVAGENT_PASSWORD"
fi
if id -Gn "$MACOS_DEVAGENT_USER" | tr ' ' '\n' | grep -qx admin; then
    sudo_with_password dseditgroup -o edit -d "$MACOS_DEVAGENT_USER" -t user admin
fi
sudo_with_password mkdir -p "/Users/$MACOS_DEVAGENT_USER/Recreation"
sudo_with_password chown -R "$MACOS_DEVAGENT_USER:staff" "/Users/$MACOS_DEVAGENT_USER"

[ -f "$REQUIREMENTS_FILE" ] || {
    echo "macOS Python requirements not found: $REQUIREMENTS_FILE" >&2
    exit 2
}
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}" \
    python3 -m pip install --user --requirement "$REQUIREMENTS_FILE"

case "${RB_AGENT_CLI:-codex}" in
  codex*)
    want="${RB_CODEX_VERSION:-0.145.0}"
    have="$(codex --version 2>/dev/null | awk '{print $NF}' || true)"
    [ "$have" = "$want" ] || sudo_with_password env PATH="$PATH" npm install -g --registry=https://registry.npmjs.org --no-audit --no-fund "@openai/codex@$want"
    ;;
  *)
    want="${RB_CLAUDE_VERSION:-2.1.177}"
    have="$(claude --version 2>/dev/null | awk '{print $1}' || true)"
    [ "$have" = "$want" ] || sudo_with_password env PATH="$PATH" npm install -g --registry=https://registry.npmjs.org --no-audit --no-fund "@anthropic-ai/claude-code@$want"
    ;;
esac

echo 'macOS runtime preparation complete'
