# Running RecreationBench on Linux

Linux supports FC Agent Sandbox and user-managed ECS/Docker environments. Both paths
use the runtime at the repository root and evaluation data already present on the
controller.

## Requirements

- Python 3.10+, Docker, and reachable model and visual-judge APIs.
- An FC Agent Sandbox account when using the Sandbox path.
- An extracted RecreationBench dataset or per-task `.tar.gz` archives.

Accepted local dataset layouts include:

```text
<dataset-root>/ubuntu/<task-id>/instance.json
<dataset-root>/ubuntu/<task-id>/reference/
<dataset-root>/ubuntu/<task-id>/tests/
```

The controller also accepts `<dataset-root>/<task-id>/...`,
`<task-id>.ubuntu.tar.gz`, and `ubuntu/<task-id>.tar.gz`.

## Build the image

Run from the repository root:

```bash
docker build -f providers/linux/Dockerfile -t recreationbench-linux:release-v1 .
```

For Sandbox, push an immutable image to a registry reachable by the service:

```bash
docker buildx build --platform linux/amd64 \
  --provenance=false --sbom=false \
  -t <your-registry>/recreationbench-linux:<immutable-tag> \
  -f providers/linux/Dockerfile . --push
```

## Create a Sandbox template

```bash
cd <repo>/providers/linux/template
cp .env.example .env
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
# Set the Sandbox endpoint, image, model, judge, and local dataset path in .env.
python build_template.py
```

Save the returned template ID as `RB_LINUX_TEMPLATE`.

## Run one task

```bash
python run_one.py \
  --task-id pencil2d-pencil \
  --agent-cli codex \
  --unified-cache-dir /path/to/released-dataset \
  --result-dir results
```

Use `--unified-archive /path/to/task.ubuntu.tar.gz` for a single-task archive.
The controller validates the input before uploading the runtime and task archive.

## Run a group

```bash
python run_many.py \
  --group-file ../../../tasks/ubuntu.jsonl \
  --unified-cache-dir /path/to/released-dataset \
  --group-id ubuntu-suite \
  --concurrency 5 \
  --retries 1 \
  --out-dir parallel_results
```

Each task retains its log, `metrics.json`, trajectory, recreated application, and
tool-use screenshots. `group_metrics.json` aggregates the fixed task set.

Add `--visual` to start noVNC for one task. To inspect an existing result, use
`--visual-only --recreated-app-dir <path>`.

The controller does not download evaluation data or publish results. Model, judge, and
Sandbox credentials are read only from the user's local environment.
