# Web runtime

Web evaluation runs in a prepared runtime with the RecreationBench browser harness. The
shared pipeline launches one native worker, preserves Web's authoritative score and
maps it into the five-platform result contract.

## Entry and ownership

```text
core/pipeline.py
  -> platforms/web/pipeline.py          launch worker and normalize native result
  -> platforms/web/worker.sh            runtime-pod worker boundary
  -> web/runner/                         agent execution and browser capture
  -> web/evaluation/                     functional, visual and diagnostic scoring
  -> web/serving/                        reference and candidate servers
  -> web/runtime_assets/                 browser/network scripts and templates
```

`SiteServer` uses the HTTP handler in `web/serving/static_handler.py`.
Browser and network helpers live under `runtime_assets/`.

## Setup

Local development requires Python 3.10+ and Node.js 18+. Set
`PLAYWRIGHT_WITH_DEPS=1` when the host also needs Chromium system libraries.

```bash
uv venv --python 3.10
uv pip install -r scripts/web/requirements.txt
npm --prefix scripts/web ci
npm --prefix scripts/web/template ci
```

Production receives Playwright and browsers from its pinned runtime image through
`MOCKWEB_NODE_MODULES`. The root npm package supports local evaluator execution.

## Scoring

`config.py` is the authoritative source for aggregate weights:

| Dimension | Weight | Implementation |
| --- | ---: | --- |
| Functional | 0.50 | Pooled Playwright cases |
| Visual | 0.50 | SSIM, LPIPS, layout IoU and coverage, or optional VLM |
| Structural | 0.00 | DOM and semantic similarity diagnostic |
| Quality | 0.00 | Accessibility, semantics and code-quality diagnostic |

The adapter preserves Web's native `final_score` as `task_score`. For the shared
program/VLM view it removes the VLM-backed visual dimension from `program_score`, so
`prog_vlm_avg` does not count the judge twice. Structural and quality diagnostics
remain in the output even while their aggregate weights are zero.

## Screenshots and artifacts

Tool-use capture attaches to the same Chromium page used by the agent. It consumes
CDP screencast frames and writes the shared `tool_use_screenshots/manifest.jsonl`
layout. Capture failure is diagnostic and does not change the agent or evaluator
exit status. Final metrics are written before the adapter's artifact hook runs.

Run the offline runtime contract check from the repository root:

```bash
uv run python scripts/release/smoke_runtime.py
```
