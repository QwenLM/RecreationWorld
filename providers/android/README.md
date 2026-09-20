# RecreationBench Android provider

This directory contains the public Android execution layer for an x86_64 Linux host
with KVM. It builds the Emulator and worker images, runs a fixed Android task, and can
serve a browser viewer for the recreated application.

Android uses the same repository-root runtime as the other four platforms.

| Path | Purpose |
| --- | --- |
| `docker/emulator/` | Android 35 Google APIs x86_64 Emulator image |
| `docker/worker/` | Worker image with the Android toolchain, agent CLI, Mobile MCP, and evaluator |
| `../../scripts/`, `../../src/` | Shared benchmark runtime |
| `dataset/` | Local frozen evaluation data, excluded from Git |
| `provision-host.sh` | Install host dependencies and build images |
| `start-emulator.sh` | Start the KVM-accelerated emulator |
| `run-bench.sh` | Run recreation and evaluation for an allowed task |
| `viewer/` | Display run state, scores, and the live emulator |
| `systemd/` | Optional Emulator and Viewer services |

See the [Android run guide](../../docs/providers/android.md) for host requirements,
configuration, commands, artifacts, and troubleshooting.
