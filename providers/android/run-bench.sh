#!/usr/bin/env bash

set -euo pipefail

APP_ID="${APP_ID:-${1:-}}"
[[ -n "${APP_ID}" ]] || { echo "Usage: APP_ID=<fixed-suite-app-id> $0" >&2; exit 1; }
[[ "${APP_ID}" =~ ^[a-zA-Z0-9._-]+$ ]] || { echo "Invalid APP_ID: ${APP_ID}" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASKS_FILE="${TASKS_FILE:-${SCRIPT_DIR}/../../tasks/android.jsonl}"
[[ -r "${TASKS_FILE}" ]] || { echo "Missing Android task manifest: ${TASKS_FILE}" >&2; exit 1; }
jq -e --arg app_id "${APP_ID}" \
  'select(.instance_id == $app_id) | true' "${TASKS_FILE}" >/dev/null || {
  echo "APP_ID is not part of the released fixed Android suite: ${APP_ID}" >&2
  exit 1
}

STAGE="${STAGE:-recreation_eval}"
case "${STAGE}" in
  setup|recreation|eval|recreation_eval) ;;
  *) echo "STAGE must be setup, recreation, eval, or recreation_eval" >&2; exit 1 ;;
esac

WORKER_IMAGE="${WORKER_IMAGE:-recreationbench/android-worker:public-v0.1.0}"
ENV_FILE="${ENV_FILE:-/etc/recreationbench/android.env}"
DEVICE_ID="${DEVICE_ID:-emulator-5554}"
EVAL_TARGET="${EVAL_TARGET:-recreation}"
RUN_NAME="${RUN_NAME:-${APP_ID}-${STAGE}-$(date -u +%Y%m%dT%H%M%SZ)}"
CONTAINER_NAME="${CONTAINER_NAME:-recreationbench-${RUN_NAME}}"
RUN_DIR="${RUN_DIR:-/opt/recreationbench/runs/${RUN_NAME}}"
SHARED_DIR="${SHARED_DIR:-/opt/recreationbench/shared}"
CANDIDATE_APK="${CANDIDATE_APK:-}"
DATASET_DIR="${DATASET_DIR:-${SCRIPT_DIR}/dataset}"
DATASET_APP_DIR="${DATASET_DIR}/android/${APP_ID}"
[[ "${DATASET_DIR}" = /* ]] || {
  echo "DATASET_DIR must be an absolute path: ${DATASET_DIR}" >&2
  exit 1
}

[[ -r "${ENV_FILE}" ]] || { echo "Missing Docker env file: ${ENV_FILE}" >&2; exit 1; }
required_env_names=()
case "${STAGE}" in
  recreation)
    required_env_names+=(RB_MODEL RB_MODEL_BASE_URL RB_MODEL_API_KEY RB_API_MODE RB_AGENT_CLI)
    ;;
  eval)
    required_env_names+=(RB_VLM_KEY RB_VLM_MODEL RB_VLM_BASE_URL)
    ;;
  recreation_eval)
    required_env_names+=(
      RB_MODEL RB_MODEL_BASE_URL RB_MODEL_API_KEY RB_API_MODE RB_AGENT_CLI
      RB_VLM_KEY RB_VLM_MODEL RB_VLM_BASE_URL
    )
    ;;
esac
for name in "${required_env_names[@]}"; do
  grep -Eq "^${name}=.+" "${ENV_FILE}" || {
    echo "Missing non-empty ${name} in ${ENV_FILE}" >&2
    exit 1
  }
done

if [[ "${STAGE}" != "setup" ]]; then
  [[ -d "${DATASET_APP_DIR}" ]] || {
    echo "Missing local evaluation data: ${DATASET_APP_DIR}" >&2
    echo "Place the released dataset under ${DATASET_DIR}/android/<APP_ID>." >&2
    exit 1
  }
  python3 "${SCRIPT_DIR}/validate-fixed-suite.py" \
    --dataset-dir "${DATASET_DIR}" --tasks-file "${TASKS_FILE}" --app-id "${APP_ID}"
fi

docker image inspect "${WORKER_IMAGE}" >/dev/null
if [[ "${STAGE}" != "setup" ]]; then
  adb -s "${DEVICE_ID}" get-state 2>/dev/null | grep -qx device || {
    echo "ADB device is not ready: ${DEVICE_ID}" >&2
    exit 1
  }
fi

install -d -m 755 "${RUN_DIR}" "${SHARED_DIR}"
if [[ "${STAGE}" == "eval" && "${EVAL_TARGET}" == "recreation" && -n "${CANDIDATE_APK}" ]]; then
  [[ "${CANDIDATE_APK}" = /* ]] || { echo "CANDIDATE_APK must be absolute" >&2; exit 1; }
  [[ -s "${CANDIDATE_APK}" ]] || { echo "Candidate APK is missing: ${CANDIDATE_APK}" >&2; exit 1; }
  install -D -m 644 "${CANDIDATE_APK}" \
    "${RUN_DIR}/stages/recreation/recreation/recreated.apk"
fi

docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
container_id="$(docker run --detach \
  --name "${CONTAINER_NAME}" \
  --init \
  --network host \
  --privileged \
  --cpus "${BENCH_CPUS:-4}" \
  --memory "${BENCH_MEMORY:-12g}" \
  --env-file "${ENV_FILE}" \
  --env RB_MODEL_TRANSPORT=direct \
  --env RB_DATASET_ROOT=/root/recreationbench-dataset \
  --env RB_UNIFIED_PLATFORM=android \
  --env RB_STAGE="${STAGE}" \
  --env RB_EVAL_TARGET="${EVAL_TARGET}" \
  --env RB_OUTPUT_DIR=/workspace \
  --env RB_DEVICE="${DEVICE_ID}" \
  --env RB_CUA_PREFLIGHT_MODE="${RB_CUA_PREFLIGHT_MODE:-strict}" \
  --env RB_MODEL_MAX_RETRIES="${RB_MODEL_MAX_RETRIES:-10}" \
  --env RB_VERSION="${RB_VERSION:-public-poc-v0.1.0}" \
  --env VERSION="${RB_VERSION:-public-poc-v0.1.0}" \
  --volume "${RUN_DIR}:/workspace" \
  --volume "${SHARED_DIR}:/shared" \
  --volume "${DATASET_DIR}:/root/recreationbench-dataset:ro" \
  --workdir /opt/recreationbench \
  "${WORKER_IMAGE}" \
  python3 -m recreation_bench.cli run \
    --platform android \
    --task-id "${APP_ID}" \
    --device "${DEVICE_ID}" \
    --stage "${STAGE}" \
    --eval-target "${EVAL_TARGET}" \
    --output-dir /workspace)"

printf 'Started Android benchmark\nContainer: %s\nRun directory: %s\nLogs: docker logs -f %s\n' \
  "${container_id}" "${RUN_DIR}" "${CONTAINER_NAME}"
