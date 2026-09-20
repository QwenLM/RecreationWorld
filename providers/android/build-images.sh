#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_ROOT="$(cd "${SERVICE_ROOT}/.." && pwd)"
EMULATOR_IMAGE="${EMULATOR_IMAGE:-recreationbench/android-emulator:api35-google-apis-r09}"
WORKER_IMAGE="${WORKER_IMAGE:-recreationbench/android-worker:public-v0.1.0}"

docker build --pull \
  --tag "${EMULATOR_IMAGE}" \
  --file "${SCRIPT_DIR}/docker/emulator/Dockerfile" \
  "${SCRIPT_DIR}/docker/emulator"

DOCKER_BUILDKIT=1 docker build --pull \
  --tag "${WORKER_IMAGE}" \
  --file "${SCRIPT_DIR}/docker/worker/Dockerfile" \
  "${RUNTIME_ROOT}"

docker image inspect "${EMULATOR_IMAGE}" "${WORKER_IMAGE}" >/dev/null
printf 'Built public images:\n  %s\n  %s\n' "${EMULATOR_IMAGE}" "${WORKER_IMAGE}"
