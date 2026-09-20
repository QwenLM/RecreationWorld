#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  adb ca-certificates curl docker.io ffmpeg git jq \
  python3-pip python3-venv qemu-kvm

systemctl enable --now docker

if [[ ! -c /dev/kvm || ! -r /dev/kvm || ! -w /dev/kvm ]]; then
  echo "/dev/kvm is unavailable. Use an ECS instance with nested virtualization/KVM." >&2
  exit 1
fi

if [[ "${BUILD_IMAGES:-true}" == "true" ]]; then
  "${SCRIPT_DIR}/build-images.sh"
fi

install -d -m 755 \
  /opt/recreationbench/runs \
  /opt/recreationbench/shared \
  /opt/recreationbench/candidates

echo "Android POC host is ready. Next: copy .env.example, then run start-emulator.sh."
