# Linux runtime

Linux evaluation runs on an Ubuntu VM prepared by the deployment controller. RecreationBench
receives that VM as an SSH target; it does not create, select or destroy the VM.

## Entry and ownership

```text
core/pipeline.py
  -> platforms/linux/pipeline.py       validate target, normalize and publish
  -> linux/worker.py                    native lifecycle coordinator
  -> linux/stages/                      setup, recreation and AT-SPI evaluation
  -> linux/runtime/                     files transported to the Ubuntu VM
```

## Runtime stages

- `setup` checks the VM, CUA/MCP wiring and permission boundary without invoking a
  model.
- `recreation` starts the reference environment, runs the isolated agent and saves
  the reconstructed application and trajectory.
- `eval` runs the trusted programmatic and VLM evaluators against frozen inputs.
- `recreation_eval` runs recreation followed by eval and is the release default.

The agent loses access to frozen evaluation inputs during recreation. After the
agent exits, the controller restores only the read and directory-traversal access
required by trusted evaluators; frozen inputs remain non-writable. Linux uses the
standard CUA driver path directly.

## Artifacts

The adapter surfaces `trajectory.jsonl`, `sessions/`, logs, pipeline state and final
`metrics.json`. With tool-use capture enabled, every identified tool result is
recorded under `tool_use_screenshots/` with a `manifest.jsonl` entry. Screenshot
failure is diagnostic and does not replace the native evaluation result.

Run the offline runtime contract check from the repository root:

```bash
uv run python scripts/release/smoke_runtime.py
```
