# RecreationBench Linux Sandbox Service

This directory contains the Linux-only path for running RecreationBench inside
FC Agent Sandbox with a custom image template.

The goal is an end-to-end sandbox run:

```text
Docker image -> FC Agent Sandbox template -> sandbox.commands.run()
  -> recreation -> eval -> /tmp/recreationbench-output/metrics.json
```

## Build image

For ECS/Docker self-hosted runs, build the image directly from this repository.
This path does not require ACR access:

```bash
cd <repo>
docker build -f providers/linux/Dockerfile -t recreationbench-linux-ecs:test .
```

For FC Agent Sandbox templates, build with public download sources and push to a
registry that the template builder can pull:

```bash
docker buildx build --platform linux/amd64 \
  --provenance=false --sbom=false \
  -t <your-registry>/recreationbench-linux:<immutable-tag> \
  -f providers/linux/Dockerfile \
  . \
  --push
```

Use a unique immutable tag for every published image. Do not reuse `latest` for
templates.
Self-hosted ECS users can skip the registry path and use the local
`recreationbench-linux-ecs:test` tag built above.

## Build sandbox template

```bash
cd <repo>/providers/linux/template
cp .env.example .env
# edit .env
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
python build_template.py
```

Set `E2B_DESKTOP_TEMPLATE` to the printed `template_id`.
The helper scripts also load `<repo>/.env.linux` by default. To
use another account file, set `RB_ENV_FILE=/path/to/env`.

The scripts also accept these template settings:

- `E2B_DESKTOP_IMAGE` as `FROM_IMAGE`
- `E2B_DESKTOP_TEMPLATE` as the sandbox template id
- `E2B_TEMPLATE_CPU_COUNT`
- `E2B_TEMPLATE_MEMORY_MB`

## Reproduction template mode

The default runner mode is for user reproduction. It keeps network behavior
simple and explicit:

- The sandbox can access the public internet by default so the agent CLI, model
  endpoint, apt, git, npm, pip, and VLM judge can work.
- The controller validates a local unified benchmark archive and uploads it
  into `/tmp/recreationbench-unified`.

For private model endpoints, pass VPC configuration through metadata:

```bash
export RB_SANDBOX_VPC_CONFIG='{"vpcId":"vpc-xxx","securityGroupId":"sg-xxx","vSwitchIds":["vsw-xxx"]}'
```

## Run one task

```bash
cd <repo>/providers/linux/template
source .venv/bin/activate
python run_one.py \
  --task-id pencil2d-pencil \
  --agent-cli codex \
  --unified-cache-dir /path/to/released-dataset
```

By default, the runner:

1. uploads the bundled `repository-root runtime` source archive;
2. validates the selected task in the local released dataset;
3. uploads that input into the sandbox in chunks;
4. runs recreation and eval inside the sandbox;
5. prints `/tmp/recreationbench-output/metrics.json`.

You can point at one archive directly:

```bash
python run_one.py \
  --task-id pencil2d-pencil \
  --agent-cli codex \
  --unified-archive /path/to/pencil2d-pencil.ubuntu.tar.gz
```

Accepted cache layouts:

- `/path/to/cache/<task-id>.ubuntu.tar.gz`
- `/path/to/cache/ubuntu/<task-id>.tar.gz`
- `/path/to/cache/ubuntu/<task-id>/instance.json` plus `reference/` and `tests/`

For compatibility, task IDs and cache paths also accept the `bench50-` prefix,
for example `bench50-pencil2d-pencil`.

For a public source tarball instead:

```bash
python run_one.py \
  --task-id pencil2d-pencil \
  --source-url https://example.com/recreationbench-runtime.tar.gz
```

## Visual debugging

Enable noVNC only when you need to watch or manually inspect a task:

```bash
python run_one.py \
  --task-id pencil2d-pencil \
  --agent-cli claude \
  --visual
```

By default the runner prints one supported viewer URL:

```text
visual_url=http://127.0.0.1:<port>/
```

Some FC gateways add `Content-Disposition: attachment` to the sandbox-hosted
`/vnc.html`, which makes browsers download the HTML instead of rendering it.
For reliable inspection, open the printed local `visual_url`:

```text
http://127.0.0.1:<port>/
```

The runner starts the local proxy automatically. Before printing the URL it
checks the upstream noVNC assets, the sandbox WebSocket and RFB greeting, the
local session, and the proxied JavaScript/WebSocket path. The printed URL serves
`<result-dir>/<task-id>/visual_index.html` through the local proxy; the page
labels the package name and embeds the live noVNC viewer. The raw noVNC URL is
kept in `<result-dir>/<task-id>/local_novnc.url` for diagnostics. To disable
the automatic proxy and print the sandbox noVNC URL/password, pass
`--no-local-viewer`.

The VNC websocket is served from inside the sandbox on port `6080`; x11vnc
captures the benchmark X display on `:99`.  `run_many.py --visual` is supported,
but use low concurrency because every task creates its own viewer URL.

## Save and reuse recreated app artifacts

`run_one.py` can save `metrics.json`, logs, eval outputs, and the generated
recreation artifact into a local result directory:

```bash
python run_one.py \
  --task-id alphaonex86-ultracopier \
  --unified-cache-dir /path/to/recreationbench-unified-cache \
  --agent-cli claude \
  --result-dir /tmp/rb-results
```

The per-task output is:

```text
/tmp/rb-results/alphaonex86-ultracopier/
  metrics.json
  run.log
  sandbox_info.json
  result_artifacts.tar.gz
  recreation/
    build.sh
    launch.sh
    src/...
  eval/
  pipeline_state/
```

To reuse a generated app without running the AI recreation step again, upload the
saved `recreation/` directory into a new sandbox:

```bash
python run_one.py \
  --task-id alphaonex86-ultracopier \
  --unified-cache-dir /path/to/recreationbench-unified-cache \
  --recreated-app-dir /tmp/rb-results/alphaonex86-ultracopier/recreation \
  --agent-cli claude \
  --visual \
  --result-dir /tmp/rb-reuse-results
```

For viewing only, skip eval and keep the sandbox alive:

```bash
python run_one.py \
  --task-id alphaonex86-ultracopier \
  --visual-only \
  --result-dir /tmp/rb-results
```

`--visual-only` implies `--visual`: it starts the app through
`build.sh`/`launch.sh`, prints the checked local `visual_url`, writes a
non-scored `metrics.json`, and does not kill the sandbox automatically. If
`--recreated-app-dir` is omitted, the runner reuses
`<result-dir>/<task-id>/recreation` when that directory exists.

Group runs use the same layout under the group output directory. JSONL task
entries may specify a per-task artifact:

```json
{"task_id":"alphaonex86-ultracopier","recreated_app_dir":"/tmp/rb-results/alphaonex86-ultracopier/recreation"}
```

```bash
python run_many.py \
  --tasks-file group.jsonl \
  --group-id linux-visual-reuse \
  --result-dir /tmp/rb-group-results \
  --visual
```

## Run tasks in parallel

Each task uses an independent sandbox. Start with low concurrency and raise it
only after confirming sandbox quota and model/VLM rate limits.

```bash
cd <repo>/providers/linux/template
source .venv/bin/activate

RB_MODEL=gpt-5.6-sol python run_many.py \
  --task-id pencil2d-pencil,ksnip-ksnip \
  --unified-cache-dir /path/to/released-dataset \
  --concurrency 2
```

For a larger set:

```bash
RB_MODEL=gpt-5.6-sol python run_many.py \
  --group-file ../../../tasks/ubuntu.jsonl \
  --group-id ubuntu-suite \
  --unified-cache-dir /path/to/released-dataset \
  --concurrency 5 \
  --retries 1 \
  --out-dir parallel_results
```

Validate a group without creating sandboxes:

```bash
python run_many.py \
  --group-file ../../../tasks/ubuntu.jsonl \
  --group-id ubuntu-suite \
  --dry-run
```

Use the unprefixed application IDs from the released dataset in group files:

```text
# groups/my-linux-group.txt
pencil2d-pencil
ksnip-ksnip
adrienverge-photocollage
```

JSONL is also supported when a task needs overrides:

```jsonl
{"task_id":"pencil2d-pencil"}
{"task_id":"ksnip-ksnip","agent_cli":"claude","timeout_sec":86400}
{"task_id":"adrienverge-photocollage","model":"claude-opus-5","env":{"RB_POLL_INTERVAL_SEC":"30"}}
```

Supported JSONL keys:

- `task_id`, `id`, or `instance_id`: required released dataset instance id.
- `model`: per-task `RB_MODEL` override.
- `agent_cli`: per-task agent CLI override, for example `codex` or `claude`.
- `mcp_provider`: per-task MCP provider override.
- `timeout_sec`: per-task sandbox timeout.
- `template`: per-task sandbox template override.
- `env` or `extra_env`: per-task environment variable overrides.

Provided Linux group example:

- `groups/linux-example.jsonl`: JSONL format example with per-task overrides.

The full released suite is defined in [tasks/ubuntu.jsonl](../../tasks/ubuntu.jsonl).

`run_many.py` uses the shared template group runner under
`<repo>/providers/common/` and writes one log and one metrics file per
task, plus `group.json`, `summary.json`, and `group_metrics.json`. The output
layout is:

```text
parallel_results/<group_id>/<run_id>/
  group.json
  group_metrics.json
  summary.json
  tasks/<task_id>/
    run.log
    metrics.json
```

`group_metrics.json` contains macro averages over scored tasks:
`avg_task_score`, `avg_program_score`, `avg_vlm_score`, and
`avg_prog_vlm_avg`. It also writes `avg_task_score_with_failures`, where
non-excluded failed or missing-metrics tasks count as 0; use this for a fixed
N-task group score.

Retry behavior is intentionally conservative:

- `--retries` / `RB_TASK_RETRIES` retries a whole task only when the failure is
  classified as sandbox SDK, DNS, or remote command transport instability.
- `--retry-backoff-sec` / `RB_TASK_RETRY_BACKOFF_SEC` controls task retry delay.
- `RB_SDK_RETRIES` and `RB_SDK_RETRY_BACKOFF_SEC` control lower-level sandbox
  SDK retries inside `run_one.py`.
- Environment, runtime permission, missing artifact, recreation, and eval
  failures are classified but not automatically retried.

Per-task entries in `summary.json` and `group_metrics.json` include
`failure_category`, `retryable`, `retry_count`, and `last_error`. Main
categories are `scored`, `infra_network`, `env_missing_dependency`,
`runtime_permission`, `agent_artifact_missing`, `eval_failed`,
`recreation_failed`, and `no_metrics`.

`run_one.py` defaults to polling the sandbox log and
`metrics.json` instead of holding a long SDK stream open; use `--stream-run`
only when debugging a single task.

## Required runtime inputs

Model credentials:

- `RB_MODEL`
- `RB_MODEL_BASE_URL`
- `RB_MODEL_API_KEY`

VLM judge credentials:

- `RB_VLM_KEY`
- `RB_VLM_MODEL`
- `RB_VLM_BASE_URL`

Frozen unified input:

- `RB_UNIFIED_PLATFORM=ubuntu`
- `RB_UNIFIED_ARCHIVE=/path/to/one-task.tar.gz`
- `RB_UNIFIED_CACHE_DIR=/path/to/unified-cache`

Set one of the two local input paths. `run_many.py` forwards the selected cache
to every task so a group run can share one directory.

Recommended Claude desktop-task tuning:

- `CONTEXT_1M=true`
- `AUTO_COMPACT_WINDOW=1048576`
- `THINKING_EFFORT=max`
- `RECREATION_TIMEOUT=72000`
- `RB_CUA_DRIVER_REF=qwen:0.7.3`
- `RB_CUA_PREFLIGHT_MODE=warn`

Model proxy retry controls for ECS/Docker DNS instability:

- `RB_MODEL_PROXY_RETRIES=5`
- `RB_MODEL_PROXY_BACKOFF_SEC=2`
- `RB_MODEL_PROXY_RETRY_ERRORS=dns`

The default is intentionally conservative: retry only DNS/name-resolution
failures before a response is received. Set `RB_MODEL_PROXY_RETRY_ERRORS=network`
only when the model provider/proxy is known to tolerate duplicate retries for
broader transport failures.

Reproduction-mode controls:

- `RB_REPRO_MODE=true` by default.
- `RB_PREFETCH_UNIFIED=true` by default.
- `RB_SANDBOX_ALLOW_INTERNET=true` by default.
- `RB_SANDBOX_VPC_CONFIG` optional JSON string for FC VPC metadata.

## Display environment

The benchmark pipeline, including evaluation and `metrics.json` generation, runs
inside the sandbox. The image provides:

- Xvfb display for GUI applications.
- D-Bus and AT-SPI for accessibility inspection.
- Openbox window manager for normal window lifecycle behavior.
- CUA driver and SSH loopback at `127.0.0.1:22` for the Linux pipeline.
- Optional x11vnc/noVNC viewer when `--visual` or `RB_ENABLE_VISUAL=1` is set.

Use logs, screenshots collected by the benchmark, `metrics.json`, and optional
noVNC as the debugging surface.
