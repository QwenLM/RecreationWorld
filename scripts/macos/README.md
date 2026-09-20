# macOS runtime

macOS evaluation runs on a prepared macOS SSH target. The user owns the target lifecycle;
RecreationBench deploys and runs the pinned benchmark runtime on that host.

## Entry and ownership

```text
core/pipeline.py
  -> platforms/macos/pipeline.py        validate native summary and normalize
  -> platforms/macos/vm_runtime.py      SSH deployment and result collection
  -> macos/runtime.py                   native runtime coordinator
  -> macos/run_full.sh + macos/stages/  recreation and AX evaluation
  -> macos/runtime_assets/              scripts transported to the target
```

The runtime uses a dedicated agent principal and protects frozen reference/tests
from that principal. Model and judge credentials live in the protected runtime
environment. CUA driver setup, network guards and sandbox preparation scripts live
under `runtime_assets/`.

## Result and artifacts

The target summary must match the requested task, stage and process exit code. A claimed
successful eval without readable programmatic/VLM results is converted to an
infrastructure failure. The platform keeps `results.tar.gz` for the complete macOS
bundle and also exposes trajectory/session files individually. Tool-use screenshots
are included in the archive and final upload when enabled.

Run the offline runtime contract check from the repository root:

```bash
uv run python scripts/release/smoke_runtime.py
```
