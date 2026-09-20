# Running RecreationBench Web

Web tasks use frozen RecreationBench Web input, Playwright MCP, and the runtime at the repository
root. The controller reads evaluation data only from local files.

## Dataset layout

```text
<dataset-root>/web/<task-id>/instance.json
<dataset-root>/web/<task-id>/reference/
<dataset-root>/web/<task-id>/tests/
```

The controller also accepts `<task-id>.web.tar.gz`,
`web/<task-id>.tar.gz`, or an explicit `--unified-archive`.

## Build the image and template

```bash
cd <repo>
docker build -f providers/web/Dockerfile -t recreationbench-web:release-v1 .

cd providers/web/template
cp .env.example .env.web
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
python build_template.py
```

For Sandbox, push the image to a reachable registry first. Configure the Sandbox
endpoint, image, model, judge, local input directory, and generated
`RB_WEB_TEMPLATE` in `.env.web`.

## Run one task

```bash
python run_one.py \
  --task-id <web-task-id> \
  --agent-cli codex \
  --mcp-provider playwright \
  --unified-cache-dir /path/to/released-dataset \
  --result-dir results
```

The reference, tests, upload staging copies, and scorer remain root-only inside the
Sandbox. Agent and build processes run without ambient capabilities. Scored runs
require an isolated network namespace and fail if that boundary cannot be created.

## Run a group

```bash
python run_many.py \
  --tasks-file ../../../tasks/web.jsonl \
  --unified-cache-dir /path/to/released-dataset \
  --group-id web-release \
  --concurrency 5 \
  --agent-cli codex \
  --mcp-provider playwright
```

Results include per-task `metrics.json`, trajectory, workspace, evaluation artifacts,
tool-use screenshots, and group-level `group_metrics.json`.

Add `--visual` to start noVNC. To display an existing static output without running
the agent or evaluator:

```bash
python run_one.py \
  --task-id <web-task-id> \
  --recreated-app-dir /path/to/artifacts/workspace/output \
  --visual-only
```

Direct Docker runs that expose the user's model key to Codex must set
`RB_DIRECT_MODEL_KEY_TO_AGENT=1`. The default Sandbox path uses a root-owned local
forwarder and does not expose judge or control-plane credentials to the agent.
Set `USE_VLM_JUDGE=true` to enable the visual judge; configuring its key alone does
not enable it.
