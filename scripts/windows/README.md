# Windows runtime

Windows evaluation runs on a prepared Windows SSH target. The deployment controller owns target
provisioning and teardown; RecreationBench owns deployment of the pinned runtime,
native execution, result normalization and artifact collection.

## Entry and ownership

```text
core/pipeline.py
  -> platforms/windows/pipeline.py      validate target and normalize results
  -> windows/vm_runtime.py              pod-side SSH/SFTP deployment coordinator
  -> windows/worker.py                  worker executed on the Windows VM
  -> windows/stages/                    recreation and UIA evaluation
  -> windows/runtime/                   transported PowerShell/Python/config files
```

Runtime files under `runtime/` are validated before upload over SFTP. Remote
failures retain stage-start and task diagnostics in the final artifact.

## Result and artifacts

`vm_runtime.py` writes `pipeline_summary.json`; the adapter validates its task,
stage and exit evidence before converting it to the shared state. The common
pipeline writes final `metrics.json` and then performs the final artifact upload.

Tool-use screenshots use the Windows desktop capture backend and share the same
`tool_use_screenshots/manifest.jsonl` contract as the other platforms.

Run the offline runtime contract check from the repository root:

```bash
uv run python scripts/release/smoke_runtime.py
```
