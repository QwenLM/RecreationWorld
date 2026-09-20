# Android runtime

Android evaluation runs in a runtime pod connected to an emulator prepared by the
deployment environment.

## Entry and ownership

```text
core/pipeline.py
  -> platforms/android/pipeline.py      launch worker and normalize native metrics
  -> platforms/android/worker.sh        runtime-pod worker boundary
  -> android/stages/                    bootstrap and release lifecycle
  -> android/evaluation/                frozen-test and VLM evaluation
  -> android/runtime/                   offline Gradle seed and SDK inputs
```

`stages/bootstrap.sh` prepares the workspace, offline Android build inputs and MCP
preflight. `stages/pipeline.sh` runs recreation and evaluation.

Frozen tests and VLM assertions are not left readable by the agent. The native
metrics writer preserves Android's authoritative task score and dimension counts;
the adapter maps those fields into the common result without recomputing them.

Tool-use screenshots capture the emulator display and use the shared manifest and
artifact path. Run the offline runtime contract check from the repository root:

```bash
uv run python scripts/release/smoke_runtime.py
```
