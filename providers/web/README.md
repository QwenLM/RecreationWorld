# RecreationBench Web provider

This directory contains the complete RecreationBench Web path for FC Agent Sandbox and
user-managed Docker environments:

```text
Docker image -> Sandbox template -> run_one.py
  -> runtime and local input upload -> agent -> evaluation -> metrics.json
```

## Build the image

Run from the repository root. Sandbox images must be pushed to a registry reachable by
the service:

```bash
docker buildx build --platform linux/amd64 \
  --provenance=false --sbom=false \
  -t <registry>/recreationbench:web-<immutable-version> \
  -f providers/web/Dockerfile . --push
```

For a self-hosted Docker run:

```bash
docker build -f providers/web/Dockerfile -t recreationbench-web:local .
```

The image contains Chromium, Playwright and Playwright MCP, Claude Code, Codex, CPU
LPIPS, the Web evaluator, and an unprivileged `agent` account. It copies the
provider-neutral runtime from this repository. Frozen input and user credentials are
never baked into the image.

## Create a Sandbox template

```bash
cd <repo>/providers/web/template
cp .env.example .env.web
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
python build_template.py
```

Configure the Sandbox endpoint, `FROM_IMAGE`, and `RB_WEB_TEMPLATE_NAME` first.
Save the returned template ID as `RB_WEB_TEMPLATE`.

## Run one task

```bash
python run_one.py \
  --task-id <web-task-id> \
  --agent-cli codex \
  --mcp-provider playwright \
  --unified-cache-dir /path/to/released-dataset
```

The controller validates the local Web input before uploading it. Inside the Sandbox,
the reference, tests, and scorer remain root-only. The unprivileged agent can access
only the local reference site and its workspace.

Large inputs can be prepared once and reused:

```bash
python run_one.py --task-id <web-task-id> \
  --unified-cache-dir /path/to/released-dataset \
  --write-unified-archive /data/<web-task-id>.tar.gz

python run_one.py --task-id <web-task-id> \
  --unified-archive /data/<web-task-id>.tar.gz \
  --agent-cli codex
```

A cache directory may contain `<task-id>.web.tar.gz`,
`web/<task-id>.tar.gz`, or `web/<task-id>/instance.json` with `reference/` and
`tests/`. A cache miss fails immediately. Set `RB_UNIFIED_PLATFORM=web`.

## Visual debugging

Add `--visual` to start Xvfb, Openbox, x11vnc, and noVNC. The controller verifies the
Sandbox noVNC, WebSocket, RFB, local session, static-resource, and forwarding paths
before printing a local `visual_url`.

Use `--no-local-viewer` to print the manual proxy command without starting the local
viewer. To inspect an existing static output without running the agent or evaluator:

```bash
python run_one.py \
  --task-id <web-task-id> \
  --recreated-app-dir /path/to/artifacts/workspace/output \
  --visual-only \
  --result-dir /tmp/rb-web-visual-results
```

The recreated-app directory must contain `index.html` or be a task result containing
`artifacts/workspace/output/index.html`.

## Run a group

```bash
cd <repo>/providers/web/template
source .venv/bin/activate
RB_UNIFIED_PLATFORM=web python run_many.py \
  --tasks-file ../../../tasks/web.jsonl \
  --group-id web-suite \
  --concurrency 5 \
  --agent-cli codex \
  --mcp-provider playwright \
  --unified-cache-dir /path/to/released-dataset
```

Each task runs in an independent Sandbox. `group_metrics.json` reports macro averages,
failure counts, exclusions, and a failure-inclusive score for fixed-size task groups.

## Constraints

- The default public reproduction mode can reach user model and judge APIs.
- Codex uses `RB_MODEL_BASE_URL` and `RB_MODEL_API_KEY`. Direct Docker runs must set
  `RB_DIRECT_MODEL_KEY_TO_AGENT=1`; the default Sandbox path keeps upstream, judge,
  and control-plane credentials from the agent.
- Dataset copies, upload staging paths, and the evaluator remain root-only, and
  the effective agent identity is checked before a run starts.
- Agent and build subprocesses have no ambient capabilities. Scored runs fail if
  the required network namespace cannot be created.
- Claude Code uses the image's local protocol adapter when the upstream endpoint is
  OpenAI Chat Completions-compatible.
- The controller writes its state immediately and reclaims the Sandbox on
  `SIGINT`/`SIGTERM`. After an abnormal controller exit, run
  `template/.venv/bin/python template/cleanup_run.py <state-file>`.
- The visual judge runs only when `USE_VLM_JUDGE=true`; a configured `RB_VLM_KEY`
  alone does not enable it. Otherwise Web visual scoring uses local SSIM, LPIPS,
  and layout IoU.
- Playwright is the default verified MCP provider.

Changes to the provider runner require rebuilding the Web image and Sandbox template.

See the [Web run guide](../../docs/providers/web.md) for the concise setup path.
