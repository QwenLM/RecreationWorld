# RecreationBench providers

This directory contains the external execution layer for running RecreationBench with
compute, model endpoints, and frozen evaluation data supplied by the user. The
provider-neutral runtime lives at the repository root.

## Layout

```text
<repo>/
  scripts/                # provider-neutral five-platform runtime
  src/                    # CLI and public data models
  providers/
    common/               # shared controller utilities
    linux/                # Linux Sandbox/ECS controller
    web/                  # Web Sandbox/ECS controller
    windows/              # Windows SSH controller
    macos/                # macOS SSH controller and runtime assets
    android/              # Android ECS/KVM controller
  docs/providers/         # end-user setup and run guides
```

| Platform | Provider | User guide |
| --- | --- | --- |
| Linux | [providers/linux](linux/README.md) | [Linux guide](../docs/providers/linux.md) |
| Web | [providers/web](web/README.md) | [Web guide](../docs/providers/web.md) |
| Windows | [providers/windows](windows/README.md) | [Windows guide](../docs/providers/windows.md) |
| macOS | [providers/macos](macos/README.md) | [macOS guide](../docs/providers/macos.md) |
| Android | [providers/android](android/README.md) | [Android guide](../docs/providers/android.md) |

See [Sandbox access](../docs/providers/sandbox.md) before using the Linux or Web
Sandbox paths.

Linux and Web support FC Agent Sandbox and user-managed ECS/Docker environments.
Windows and macOS connect to prepared SSH targets. Android runs on an x86_64
host with KVM.

## Inputs and results

- Supply evaluation data as a local directory or archive before starting a run.
- Model and visual-judge credentials come from the user's environment.
- Results are saved in the user-selected directory.

## Development

Providers use the runtime in this checkout and share its scoring, screenshot, and
artifact contracts. Validate changes from the repository root:

```bash
uv run python scripts/release/smoke_providers.py
uv run python scripts/release/smoke_runtime.py
```
