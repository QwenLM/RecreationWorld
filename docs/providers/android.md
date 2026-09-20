# Running RecreationBench on Android

This guide runs RecreationBench on an x86_64 Linux host with KVM. The host builds an
Android Emulator image and a worker image, runs a fixed Android task, and optionally
serves a browser viewer for the recreated application.

The frozen evaluation dataset is distributed separately. The runtime does not select
or download a dataset source.

## Requirements

- x86_64 Ubuntu 22.04 host with `/dev/kvm`.
- Recommended capacity: at least 32 vCPUs, 128 GiB RAM, and a 200 GiB system disk.
- Docker and access to public container, Git, Ubuntu, Google Android, npm, PyPI,
  Gradle, Maven, and crates.io repositories.
- Reachable agent-model and visual-judge endpoints.
- The released Android evaluation dataset.

Verify KVM before continuing:

```bash
ls -l /dev/kvm
```

## Clone and prepare the host

```bash
cd /opt
git clone <public-repository-url> recreationbench
cd recreationbench
git checkout <release-tag-or-commit>
sudo providers/android/provision-host.sh
```

The provisioner installs Docker, QEMU/KVM, ADB, ffmpeg, and Python, then builds:

```text
recreationbench/android-emulator:api35-google-apis-r09
recreationbench/android-worker:public-v0.1.0
```

To rebuild only the images:

```bash
sudo providers/android/build-images.sh
```

The worker includes JDK 17 for the recreation environment, JDK 21 and Rust 1.90 for
compatible reference builds, Gradle 7.6.4, Android Gradle Plugin 7.4.2, Kotlin 1.8.22,
Android compileSdk/build-tools 33, Node.js 22.22.0, Claude Code 2.1.177, Codex CLI
0.145.0, Mobile MCP 0.1.5, and the Python evaluation dependencies. The build warms the
required recreation dependencies so the agent build can remain offline. Trusted
reference preparation may additionally install SDK platform, build-tools, NDK, or CMake
versions declared by the pinned upstream project.

## Configure model endpoints

Create a root-readable environment file:

```bash
sudo install -d -m 700 /etc/recreationbench
sudo cp providers/android/.env.example /etc/recreationbench/android.env
sudo chmod 600 /etc/recreationbench/android.env
sudo editor /etc/recreationbench/android.env
```

Claude Code requires an Anthropic Messages-compatible endpoint:

```dotenv
RB_AGENT_CLI=claude
RB_API_MODE=native
RB_MODEL=<model-name>
RB_CLAUDE_MODEL=<claude-code-model-alias>
RB_MODEL_BASE_URL=https://<model-endpoint>
RB_MODEL_API_KEY=<model-key>
```

Codex requires an OpenAI Responses-compatible endpoint:

```dotenv
RB_AGENT_CLI=codex
RB_API_MODE=openai
RB_MODEL=<model-name>
RB_MODEL_BASE_URL=https://<responses-compatible-endpoint>/v1
RB_MODEL_API_KEY=<model-key>
```

Configure the visual judge independently:

```dotenv
RB_VLM_MODEL=<judge-model>
RB_VLM_BASE_URL=https://<chat-completions-compatible-endpoint>/v1
RB_VLM_KEY=<judge-key>
```

## Install the released dataset

Keep the published hierarchy unchanged:

```text
<repo>/providers/android/dataset/
  android/
    <APP_ID>/
      instance.json
      vlm_assertions.json
      reference/
        launch.sh
        patches/              # present only when declared by instance.json
        screenshots/
      tests/
        test_manifest.json
        android_testgen_kit.py
        test_android_*.py
```

Validate one task or the full 50-task release:

```bash
python3 providers/android/validate-fixed-suite.py \
  --app-id dozingcat-vector-pinball
python3 providers/android/validate-fixed-suite.py
```

The wrapsource dataset identifies the Android reference by its public repository and
pinned commit in `instance.json`; it does not ship a reference APK. During trusted
preparation, the worker clones that revision, verifies and applies any declared patch,
runs the project's Gradle `assembleDebug` task, and selects the APK whose application ID
matches `package`. If no exact match exists, `package` plus `.debug` is also accepted;
installation and launch use the selected APK's actual application ID. Other package
names are rejected. Public Git/Gradle/Maven access is required on the first run.

The source checkout and on-disk APK are removed before the recreation agent starts. The
agent can observe the installed reference application in the emulator but cannot read
its source, APK, tests, visual assertions, or reference screenshots.

## Start the emulator

```bash
sudo providers/android/start-emulator.sh
adb devices
```

A healthy instance appears as `emulator-5554 device`. To recreate it:

```bash
sudo docker rm -f recreationbench-emulator
sudo providers/android/start-emulator.sh
```

## Run a task

Only task IDs in `tasks/android.jsonl` are accepted.

```bash
sudo APP_ID=dozingcat-vector-pinball \
  RUN_NAME=vector-pinball-run-1 \
  providers/android/run-bench.sh
```

The worker starts detached, so disconnecting SSH or stopping `docker logs -f` does
not terminate it. Follow progress with:

```bash
docker logs -f recreationbench-vector-pinball-run-1
```

Run recreation only:

```bash
sudo STAGE=recreation APP_ID=dozingcat-vector-pinball \
  RUN_NAME=vector-pinball-recreation-1 \
  providers/android/run-bench.sh
```

Evaluate an existing candidate APK:

```bash
sudo STAGE=eval EVAL_TARGET=recreation \
  APP_ID=dozingcat-vector-pinball \
  CANDIDATE_APK=/absolute/path/recreated.apk \
  RUN_NAME=vector-pinball-eval-1 \
  providers/android/run-bench.sh
```

Results are stored under `/opt/recreationbench/runs/<RUN_NAME>/`. Read
`metrics.json` for automation; the directory also contains the recreated APK,
trajectory, screenshots, evaluation details, and logs.

## Open the viewer

```bash
RB_RUN_DIR=/opt/recreationbench/runs/vector-pinball-run-1 \
ANDROID_SERIAL=emulator-5554 \
python3 providers/android/viewer/server.py --host 127.0.0.1 --port 8080
```

Forward the local-only viewer from your workstation:

```bash
ssh -L 8080:127.0.0.1:8080 root@ECS_PUBLIC_IP
```

Open `http://127.0.0.1:8080`. The viewer displays run status and scores and supports
live screen viewing, taps, swipes, text input, navigation buttons, and application
launching.

## Troubleshooting

- If the emulator is very slow or fails to start, verify that `/dev/kvm` exists and
  is accessible. Do not use pure software emulation for benchmark runs.
- For model failures, verify the endpoint protocol, model name, credentials, quota, and
  network reachability.
- For missing input, verify
  `providers/android/dataset/android/<APP_ID>/instance.json` and run the dataset
  validator.
- For an incomplete run, inspect the worker exit code, logs, and `metrics.json`.
