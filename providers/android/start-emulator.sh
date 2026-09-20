#!/usr/bin/env bash

set -euo pipefail

IMAGE_REFERENCE="${IMAGE_REFERENCE:-recreationbench/android-emulator:api35-google-apis-r09}"
CONTAINER_NAME="${CONTAINER_NAME:-recreationbench-emulator}"
EMULATOR_MEMORY_MB="${EMULATOR_MEMORY_MB:-16000}"
EMULATOR_PORT="${EMULATOR_PORT:-5554}"
GRPC_PORT="${GRPC_PORT:-8554}"
EMULATOR_GPU="${EMULATOR_GPU:-swiftshader_indirect}"
BOOT_TIMEOUT_SECONDS="${BOOT_TIMEOUT_SECONDS:-600}"
LOG_DIR="${LOG_DIR:-/var/log/recreationbench}"
EXPECTED_SERIAL="emulator-${EMULATOR_PORT}"

mkdir -p "${LOG_DIR}"

if [[ ! -c /dev/kvm || ! -r /dev/kvm || ! -w /dev/kvm ]]; then
  echo "/dev/kvm is unavailable; refusing to start an unaccelerated emulator." >&2
  exit 1
fi

docker image inspect "${IMAGE_REFERENCE}" >/dev/null
adb start-server
if [[ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null || true)" == "true" ]]; then
  echo "Android emulator container is already running: ${CONTAINER_NAME}"
else
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker run --detach \
    --name "${CONTAINER_NAME}" \
    --network host \
    --privileged \
    --device /dev/kvm:/dev/kvm \
    --restart unless-stopped \
    --env EMULATOR_NAME=AndroidEmulator \
    --env HW_ACCEL=on \
    --env EMULATOR_MEMORY="${EMULATOR_MEMORY_MB}" \
    --env EMULATOR_PORT="${EMULATOR_PORT}" \
    --env GRPC_PORT="${GRPC_PORT}" \
    --env EMULATOR_GPU="${EMULATOR_GPU}" \
    "${IMAGE_REFERENCE}" \
    /bin/bash -lc 'exec emulator "@${EMULATOR_NAME}" -no-window -no-audio -no-boot-anim -accel "${HW_ACCEL}" -memory "${EMULATOR_MEMORY}" -port "${EMULATOR_PORT}" -grpc "${GRPC_PORT}" -gpu "${EMULATOR_GPU}"'
fi

deadline=$((SECONDS + BOOT_TIMEOUT_SECONDS))
until adb devices | awk -v serial="${EXPECTED_SERIAL}" '$1 == serial && $2 == "device" {found=1} END {exit !found}'; do
  if (( SECONDS >= deadline )); then
    docker logs "${CONTAINER_NAME}" >"${LOG_DIR}/emulator.log" 2>&1 || true
    echo "Timed out waiting for ${EXPECTED_SERIAL}; see ${LOG_DIR}/emulator.log" >&2
    exit 1
  fi
  sleep 2
done

until [[ "$(adb -s "${EXPECTED_SERIAL}" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == "1" ]]; do
  if (( SECONDS >= deadline )); then
    docker logs "${CONTAINER_NAME}" >"${LOG_DIR}/emulator.log" 2>&1 || true
    echo "Timed out waiting for Android boot; see ${LOG_DIR}/emulator.log" >&2
    exit 1
  fi
  sleep 2
done

for scale in window_animation_scale transition_animation_scale animator_duration_scale; do
  adb -s "${EXPECTED_SERIAL}" shell settings put global "${scale}" 0.0
done
adb -s "${EXPECTED_SERIAL}" shell settings put global hidden_api_policy 1

echo "Android emulator ready: ${EXPECTED_SERIAL}"
