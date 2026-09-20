FROM eclipse-temurin:21-jdk-jammy AS jdk21
FROM rust:1.90.0-slim-bookworm AS rust-toolchain
FROM ubuntu:22.04

ARG CLAUDE_CODE_VERSION=2.1.177

ENV DEBIAN_FRONTEND=noninteractive \
    RB_ANDROID_TOOLCHAIN_ROOT=/opt/rb-android-toolchain-v1 \
    RB_ANDROID_OFFLINE_ROOT=/opt/rb-android-toolchain-v1 \
    RB_ANDROID_JAVA_21_HOME=/opt/java/openjdk \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PYTHONPATH=/opt/recreationbench/src:/opt/recreationbench/scripts \
    RB_SCRIPTS_DIR=/opt/recreationbench/scripts \
    ANDROID_HOME=/opt/rb-android-toolchain-v1/android-sdk \
    ANDROID_SDK_ROOT=/opt/rb-android-toolchain-v1/android-sdk \
    GRADLE_HOME=/opt/rb-android-toolchain-v1/gradle/gradle-7.6.4 \
    PATH=/usr/local/cargo/bin:/opt/rb-android-toolchain-v1/node-v22.22.0-linux-x64/bin:/opt/rb-android-toolchain-v1/codex/node_modules/.bin:/opt/rb-android-toolchain-v1/gradle/gradle-7.6.4/bin:/opt/rb-android-toolchain-v1/android-sdk/cmdline-tools/latest/bin:/opt/rb-android-toolchain-v1/android-sdk/platform-tools:/opt/rb-android-toolchain-v1/android-sdk/build-tools/33.0.0:${PATH}

# The fixed recreation scaffold stays on JDK 17. Some pinned reference projects
# require JDK 21 or Rust, so those compilers are copied in under separate paths and
# selected only by the trusted reference builder.
COPY --from=jdk21 /opt/java/openjdk /opt/java/openjdk
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      bash ca-certificates curl git jq file unzip zip tar xz-utils \
      openjdk-17-jdk-headless python3 python3-pip python3-venv \
      build-essential cmake ninja-build pkg-config procps passwd util-linux && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --no-cache-dir \
      'pydantic>=2,<3' 'pyyaml>=6,<7' 'paramiko>=3,<5'

# Reuse the benchmark's own version pins and public download URLs. This warms
# Gradle and Android dependencies while the image is built; scored recreation
# runs are then forced offline for deterministic builds.
COPY scripts/android/stages/bootstrap.sh /tmp/rb-bootstrap.sh
RUN bash /tmp/rb-bootstrap.sh && \
    /opt/rb-android-toolchain-v1/node-v22.22.0-linux-x64/bin/npm install \
      --prefix /opt/rb-android-toolchain-v1/claude-code \
      --no-save --package-lock=false --no-audit --no-fund \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && \
    ln -s /opt/rb-android-toolchain-v1/claude-code/node_modules/.bin/claude \
      /usr/local/bin/claude && \
    test "$(claude --version | awk '{print $1}')" = "${CLAUDE_CODE_VERSION}" && \
    rm -f /tmp/rb-bootstrap.sh

COPY . /opt/recreationbench
WORKDIR /opt/recreationbench

LABEL org.opencontainers.image.title="RecreationBench Android public worker" \
      org.opencontainers.image.description="Self-contained public RecreationBench Android worker"

CMD ["bash"]
