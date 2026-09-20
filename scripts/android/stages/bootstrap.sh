#!/usr/bin/env bash
# Networked Android toolchain bootstrap followed by an offline Gradle hand-off.
#
# This file is sourced by pipeline.sh and intentionally does not change shell options: the
# deployment adapter owns them. Every pipeline stage calls bootstrap_android_build_env before it
# downloads stage inputs or demotes the recreation agent. The first call installs and warms the
# pinned toolchain while the pod still has network access; later calls are marker-based no-ops.

RB_ANDROID_TOOLCHAIN_ROOT="${RB_ANDROID_TOOLCHAIN_ROOT:-${RB_ANDROID_OFFLINE_ROOT:-/opt/rb-android-toolchain-v1}}"
# Kept as an exported compatibility name: the prompt and permission hand-off already use it.
RB_ANDROID_OFFLINE_ROOT="$RB_ANDROID_TOOLCHAIN_ROOT"
RB_ANDROID_GRADLE_VERSION="7.6.4"
RB_ANDROID_AGP_VERSION="7.4.2"
RB_ANDROID_KOTLIN_VERSION="1.8.22"
RB_ANDROID_COMPILE_SDK="33"
RB_ANDROID_BUILD_TOOLS_VERSION="33.0.0"
RB_ANDROID_MOBILE_MCP_VERSION="0.1.5"
RB_ANDROID_CODEX_VERSION="0.145.0"
RB_ANDROID_NODE_VERSION="22.22.0"
RB_ANDROID_COMMANDLINE_TOOLS_VERSION="14742923"

RB_ANDROID_GRADLE_ARCHIVE="gradle-${RB_ANDROID_GRADLE_VERSION}-bin.zip"
RB_ANDROID_COMMANDLINE_TOOLS_ARCHIVE="commandlinetools-linux-${RB_ANDROID_COMMANDLINE_TOOLS_VERSION}_latest.zip"
RB_ANDROID_NODE_ARCHIVE="node-v${RB_ANDROID_NODE_VERSION}-linux-x64.tar.xz"
RB_ANDROID_GRADLE_URL="${RB_ANDROID_GRADLE_URL:-https://services.gradle.org/distributions/$RB_ANDROID_GRADLE_ARCHIVE}"
RB_ANDROID_COMMANDLINE_TOOLS_URL="${RB_ANDROID_COMMANDLINE_TOOLS_URL:-https://dl.google.com/android/repository/$RB_ANDROID_COMMANDLINE_TOOLS_ARCHIVE}"
RB_ANDROID_NODE_URL="${RB_ANDROID_NODE_URL:-https://nodejs.org/dist/v$RB_ANDROID_NODE_VERSION/$RB_ANDROID_NODE_ARCHIVE}"
RB_ANDROID_BOOTSTRAP_MARKER=".rb-network-bootstrap-v1-ok"

_android_runtime_helper() {
    printf '%s' "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/runtime_helpers.py"
}

_android_runtime_asset() {
    printf '%s/%s' "$(cd "$(dirname "${BASH_SOURCE[0]}")/../runtime" && pwd)" "$1"
}

_android_bootstrap_download() {
    local name="$1" url="$2" out="$3" local_file=""

    if [ -n "${RB_ANDROID_DOWNLOAD_DIR:-}" ]; then
        local_file="${RB_ANDROID_DOWNLOAD_DIR%/}/$name"
    elif [ -n "${AAR_THIRD_PARTY_SRC:-}" ]; then
        local_file="${AAR_THIRD_PARTY_SRC%/}/third-party/$name"
    fi
    if [ -n "$local_file" ] && [ -s "$local_file" ]; then
        cp -f "$local_file" "$out"
        return 0
    fi

    # Prefer the configured artifact mirror, then use the public upstream URL.
    local base_prefix="${RB_ARTIFACT_PREFIX:-}"
    local prefix="${RB_ARTIFACT_THIRD_PARTY_PREFIX:-${base_prefix%/}/third-party}"
    if declare -F artifact_backend_available >/dev/null 2>&1 &&
       artifact_backend_available; then
        echo "==> [android-bootstrap] fetching $name from artifact mirror"
        if artifact_xfer dl_file "${prefix%/}/$name" "$out"; then
            return 0
        fi
        echo "WARN: Android artifact fetch failed for $name; falling back to upstream" >&2
    fi

    command -v curl >/dev/null 2>&1 || {
        echo "ERROR: curl is required to download $name during Android setup" >&2
        return 1
    }
    curl --fail --location --retry 5 --retry-delay 5 "$url" -o "$out"
}

_android_bootstrap_export_env() {
    export RB_ANDROID_TOOLCHAIN_ROOT RB_ANDROID_OFFLINE_ROOT
    export ANDROID_HOME="$RB_ANDROID_TOOLCHAIN_ROOT/android-sdk"
    export ANDROID_SDK_ROOT="$ANDROID_HOME"
    export GRADLE_HOME="$RB_ANDROID_TOOLCHAIN_ROOT/gradle/gradle-$RB_ANDROID_GRADLE_VERSION"
    export RB_ANDROID_NODE_HOME="$RB_ANDROID_TOOLCHAIN_ROOT/node-v$RB_ANDROID_NODE_VERSION-linux-x64"
    export PATH="$RB_ANDROID_NODE_HOME/bin:$RB_ANDROID_TOOLCHAIN_ROOT/codex/node_modules/.bin:$GRADLE_HOME/bin:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/build-tools/$RB_ANDROID_BUILD_TOOLS_VERSION:$PATH"
}

_android_bootstrap_prepare_gradle_cache() {
    local requested="${GRADLE_USER_HOME:-/root/.gradle}"
    local agent_cache="/home/${RB_ANDROID_AGENT_USER:-rbagent}/.gradle"

    # permission_probe relocates root's cache into the unprivileged agent home. Reuse it when a
    # recreation_eval sequence enters the next stage in the same pod.
    if [ -s "$requested/.rb-offline-cache-ready" ]; then
        GRADLE_USER_HOME="$requested"
    elif [ -s "$agent_cache/.rb-offline-cache-ready" ]; then
        GRADLE_USER_HOME="$agent_cache"
    else
        GRADLE_USER_HOME="$requested"
        mkdir -p "$GRADLE_USER_HOME" || return 1
    fi
    export GRADLE_USER_HOME
}

_android_bootstrap_install_node() {
    local root="$RB_ANDROID_TOOLCHAIN_ROOT"
    local node_home="$root/node-v$RB_ANDROID_NODE_VERSION-linux-x64"
    local archive="$root/downloads/$RB_ANDROID_NODE_ARCHIVE"

    if [ -x "$node_home/bin/node" ] &&
       [ "$("$node_home/bin/node" --version 2>/dev/null)" = "v$RB_ANDROID_NODE_VERSION" ] &&
       [ -x "$node_home/bin/npm" ]; then
        return 0
    fi
    [ "$(uname -m)" = "x86_64" ] || {
        echo "ERROR: Android bootstrap currently pins the Node.js x64 distribution" >&2
        return 1
    }
    echo "==> Downloading Node.js $RB_ANDROID_NODE_VERSION"
    _android_bootstrap_download "$RB_ANDROID_NODE_ARCHIVE" "$RB_ANDROID_NODE_URL" "$archive" || return 1
    tar -xJf "$archive" -C "$root" || return 1
    [ -x "$node_home/bin/node" ] && [ -x "$node_home/bin/npm" ] || {
        echo "ERROR: Node.js archive did not contain the expected executables" >&2
        return 1
    }
}

_android_bootstrap_install_sdk() {
    local root="$RB_ANDROID_TOOLCHAIN_ROOT" sdk="$ANDROID_HOME"
    local sdkmanager="$sdk/cmdline-tools/latest/bin/sdkmanager"
    local archive="$root/downloads/$RB_ANDROID_COMMANDLINE_TOOLS_ARCHIVE"
    local unpacked="$root/commandline-tools-unpacked"

    if [ ! -x "$sdkmanager" ]; then
        echo "==> Downloading Android command-line tools $RB_ANDROID_COMMANDLINE_TOOLS_VERSION"
        _android_bootstrap_download \
            "$RB_ANDROID_COMMANDLINE_TOOLS_ARCHIVE" \
            "$RB_ANDROID_COMMANDLINE_TOOLS_URL" "$archive" || return 1
        rm -rf "$unpacked" "$sdk/cmdline-tools/latest"
        mkdir -p "$unpacked" "$sdk/cmdline-tools/latest"
        unzip -q "$archive" -d "$unpacked" || return 1
        cp -a "$unpacked/cmdline-tools/." "$sdk/cmdline-tools/latest/" || return 1
        sdkmanager="$sdk/cmdline-tools/latest/bin/sdkmanager"
    fi
    [ -x "$sdkmanager" ] || {
        echo "ERROR: sdkmanager is missing after command-line tools installation" >&2
        return 1
    }

    _android_bootstrap_sdk_ready() {
        [ -s "$sdk/platforms/android-$RB_ANDROID_COMPILE_SDK/android.jar" ] &&
        [ -x "$sdk/build-tools/$RB_ANDROID_BUILD_TOOLS_VERSION/aapt2" ] &&
        [ -x "$sdk/build-tools/$RB_ANDROID_BUILD_TOOLS_VERSION/d8" ] &&
        [ -x "$sdk/build-tools/$RB_ANDROID_BUILD_TOOLS_VERSION/apksigner" ] &&
        [ -x "$sdk/platform-tools/adb" ]
    }

    _android_bootstrap_clean_partial_sdk() {
        # sdkmanager leaves both its PackageOperation scratch tree and the
        # destination directory behind when a streamed zip is truncated.  A
        # second invocation otherwise reopens the same corrupt partial and
        # fails with the same SeekableByteChannel error.
        rm -rf "$sdk/.temp"
        rm -rf \
            "$sdk/platforms/android-$RB_ANDROID_COMPILE_SDK" \
            "$sdk/build-tools/$RB_ANDROID_BUILD_TOOLS_VERSION" \
            "$sdk/platform-tools"
    }

    if _android_bootstrap_sdk_ready; then
        return 0
    fi

    echo "==> Installing Android API $RB_ANDROID_COMPILE_SDK and build-tools $RB_ANDROID_BUILD_TOOLS_VERSION"
    if ! "$sdkmanager" --sdk_root="$sdk" --licenses >/dev/null \
        < "$(_android_runtime_asset sdk_licenses.txt)"; then
        echo "ERROR: Android SDK licenses could not be accepted" >&2
        return 1
    fi
    local attempts="${RB_ANDROID_SDK_INSTALL_ATTEMPTS:-3}"
    local retry_wait="${RB_ANDROID_SDK_RETRY_WAIT_SEC:-10}"
    case "$attempts" in
        ''|*[!0-9]*|0) attempts=3 ;;
    esac
    case "$retry_wait" in
        ''|*[!0-9]*) retry_wait=10 ;;
    esac
    local attempt
    for attempt in $(seq 1 "$attempts"); do
        if "$sdkmanager" --sdk_root="$sdk" \
            "platforms;android-$RB_ANDROID_COMPILE_SDK" \
            "build-tools;$RB_ANDROID_BUILD_TOOLS_VERSION" \
            "platform-tools" && _android_bootstrap_sdk_ready; then
            return 0
        fi
        echo "WARN: Android SDK install incomplete (attempt $attempt/$attempts); cleaning partial packages" >&2
        _android_bootstrap_clean_partial_sdk
        if [ "$attempt" -lt "$attempts" ]; then
            sleep "$((retry_wait * attempt))"
        fi
    done
    echo "ERROR: Android SDK install failed after $attempts attempt(s)" >&2
    return 1
}

_android_bootstrap_install_gradle() {
    local root="$RB_ANDROID_TOOLCHAIN_ROOT"
    local archive="$root/$RB_ANDROID_GRADLE_ARCHIVE"
    local gradle="$root/gradle/gradle-$RB_ANDROID_GRADLE_VERSION/bin/gradle"

    if [ ! -x "$gradle" ]; then
        echo "==> Downloading Gradle $RB_ANDROID_GRADLE_VERSION"
        _android_bootstrap_download "$RB_ANDROID_GRADLE_ARCHIVE" "$RB_ANDROID_GRADLE_URL" "$archive" || return 1
        mkdir -p "$root/gradle"
        unzip -qo "$archive" -d "$root/gradle" || return 1
    fi
    [ -x "$gradle" ] && [ -s "$archive" ] || {
        echo "ERROR: Gradle $RB_ANDROID_GRADLE_VERSION installation is incomplete" >&2
        return 1
    }
}

_android_bootstrap_write_seed_project() {
    local smoke="$RB_ANDROID_TOOLCHAIN_ROOT/smoke"
    mkdir -p "$smoke"
    cp -a "$(_android_runtime_asset offline_seed)/." "$smoke/" || return 1
    sed -i \
        -e "s|__AGP_VERSION__|$RB_ANDROID_AGP_VERSION|g" \
        -e "s|__KOTLIN_VERSION__|$RB_ANDROID_KOTLIN_VERSION|g" \
        -e "s|__COMPILE_SDK__|$RB_ANDROID_COMPILE_SDK|g" \
        -e "s|__BUILD_TOOLS_VERSION__|$RB_ANDROID_BUILD_TOOLS_VERSION|g" \
        "$smoke/build.gradle" "$smoke/app/build.gradle"
}

_android_bootstrap_seed_gradle() {
    local root="$RB_ANDROID_TOOLCHAIN_ROOT" smoke="$RB_ANDROID_TOOLCHAIN_ROOT/smoke"
    local wrapper_tmp="$RB_ANDROID_TOOLCHAIN_ROOT/wrapper-project"
    local offline_init="$GRADLE_USER_HOME/init.d/recreationbench-offline.gradle"

    _android_bootstrap_write_seed_project || return 1
    # A retry after an interrupted bootstrap must be allowed online long enough to finish seeding.
    rm -f "$offline_init" "$GRADLE_USER_HOME/.rb-offline-cache-ready"
    echo "==> Prewarming AGP, Kotlin and AndroidX dependencies"
    local attempts="${RB_ANDROID_GRADLE_SEED_ATTEMPTS:-3}"
    local retry_wait="${RB_ANDROID_GRADLE_RETRY_WAIT_SEC:-15}"
    case "$attempts" in
        ''|*[!0-9]*|0) attempts=3 ;;
    esac
    case "$retry_wait" in
        ''|*[!0-9]*) retry_wait=15 ;;
    esac
    local attempt seeded=0
    for attempt in $(seq 1 "$attempts"); do
        if "$GRADLE_HOME/bin/gradle" --no-daemon --refresh-dependencies \
            --gradle-user-home "$GRADLE_USER_HOME" -p "$smoke" \
            :app:seedOfflineDependencies :app:assembleDebug; then
            seeded=1
            break
        fi
        echo "WARN: Gradle dependency seed failed (attempt $attempt/$attempts)" >&2
        if [ "$attempt" -lt "$attempts" ]; then
            sleep "$((retry_wait * attempt))"
        fi
    done
    [ "$seeded" = 1 ] || {
        echo "ERROR: Gradle dependency seed failed after $attempts attempt(s)" >&2
        return 1
    }

    echo "==> Proving the warmed Gradle cache works offline"
    "$GRADLE_HOME/bin/gradle" --offline --no-daemon \
        --gradle-user-home "$GRADLE_USER_HOME" -p "$smoke" \
        clean :app:seedOfflineDependencies :app:assembleDebug || return 1

    mkdir -p "$wrapper_tmp"
    printf 'rootProject.name = "wrapper"\n' > "$wrapper_tmp/settings.gradle"
    "$GRADLE_HOME/bin/gradle" --offline --no-daemon \
        --gradle-user-home "$GRADLE_USER_HOME" -p "$wrapper_tmp" \
        wrapper --gradle-version "$RB_ANDROID_GRADLE_VERSION" --distribution-type bin || return 1
    mkdir -p "$root/wrapper/gradle/wrapper"
    cp -f "$wrapper_tmp/gradlew" "$wrapper_tmp/gradlew.bat" "$root/wrapper/" || return 1
    cp -f "$wrapper_tmp/gradle/wrapper/gradle-wrapper.jar" \
        "$root/wrapper/gradle/wrapper/" || return 1

    mkdir -p "$GRADLE_USER_HOME/init.d"
    install -m 0644 "$(_android_runtime_asset offline.gradle)" "$offline_init"
    printf 'ok\n' > "$GRADLE_USER_HOME/.rb-offline-cache-ready"
}

_android_bootstrap_install_npm_packages() {
    local npm="$RB_ANDROID_NODE_HOME/bin/npm"
    echo "==> Installing pinned mobile-mcp and Codex CLI"
    "$npm" install --prefix "$RB_ANDROID_TOOLCHAIN_ROOT/mobile-mcp" \
        --no-save --package-lock=false --no-audit --no-fund \
        "@qwen-code/mobile-mcp@$RB_ANDROID_MOBILE_MCP_VERSION" || return 1
    "$npm" install --prefix "$RB_ANDROID_TOOLCHAIN_ROOT/codex" \
        --no-save --package-lock=false --no-audit --no-fund \
        "@openai/codex@$RB_ANDROID_CODEX_VERSION" || return 1
}

_android_bootstrap_install_python() {
    command -v python3 >/dev/null 2>&1 || {
        echo "ERROR: Android bootstrap requires python3" >&2
        return 1
    }
    if python3 "$(_android_runtime_helper)" check-eval-dependencies >/dev/null 2>&1; then
        return 0
    fi
    echo "==> Installing evaluator Python dependencies"
    python3 -m pip install --disable-pip-version-check \
        'pytest>=8,<10' 'pillow>=11,<13' || return 1
    python3 "$(_android_runtime_helper)" check-eval-dependencies >/dev/null 2>&1 || {
        echo "ERROR: evaluator Python dependencies could not be imported after setup" >&2
        return 1
    }
}

_android_bootstrap_write_manifest() {
    local root="$RB_ANDROID_TOOLCHAIN_ROOT" smoke="$RB_ANDROID_TOOLCHAIN_ROOT/smoke"
    cat > "$root/DEPENDENCIES.txt" <<EOF
JDK 17
Gradle $RB_ANDROID_GRADLE_VERSION
Android Gradle Plugin $RB_ANDROID_AGP_VERSION
Kotlin Gradle plugin $RB_ANDROID_KOTLIN_VERSION
compileSdk/targetSdk $RB_ANDROID_COMPILE_SDK
build-tools $RB_ANDROID_BUILD_TOOLS_VERSION
Node.js $RB_ANDROID_NODE_VERSION
mobile-mcp $RB_ANDROID_MOBILE_MCP_VERSION
Codex CLI $RB_ANDROID_CODEX_VERSION

The complete transitive closure for every dependency declared below was downloaded during pipeline
startup and verified with a network-free Gradle build. Do not change versions during recreation.

Available Android libraries (exact versions):
EOF
    sed -n "s/^[[:space:]]*\\(implementation\\|kapt\\) '\\([^']*\\)'.*/  \\2/p" \
        "$smoke/app/build.gradle" >> "$root/DEPENDENCIES.txt"
    cat > "$root/manifest.env" <<EOF
BOOTSTRAP_VERSION=1
GRADLE_VERSION=$RB_ANDROID_GRADLE_VERSION
AGP_VERSION=$RB_ANDROID_AGP_VERSION
KOTLIN_VERSION=$RB_ANDROID_KOTLIN_VERSION
COMPILE_SDK=$RB_ANDROID_COMPILE_SDK
BUILD_TOOLS_VERSION=$RB_ANDROID_BUILD_TOOLS_VERSION
MOBILE_MCP_VERSION=$RB_ANDROID_MOBILE_MCP_VERSION
CODEX_VERSION=$RB_ANDROID_CODEX_VERSION
NODE_VERSION=$RB_ANDROID_NODE_VERSION
EOF
}

_android_bootstrap_validate() {
    local root="$RB_ANDROID_TOOLCHAIN_ROOT" missing="" file
    for file in \
        "gradle/gradle-$RB_ANDROID_GRADLE_VERSION/bin/gradle" \
        "$RB_ANDROID_GRADLE_ARCHIVE" \
        "android-sdk/cmdline-tools/latest/bin/sdkmanager" \
        "android-sdk/platforms/android-$RB_ANDROID_COMPILE_SDK/android.jar" \
        "android-sdk/build-tools/$RB_ANDROID_BUILD_TOOLS_VERSION/aapt2" \
        "android-sdk/build-tools/$RB_ANDROID_BUILD_TOOLS_VERSION/d8" \
        "android-sdk/build-tools/$RB_ANDROID_BUILD_TOOLS_VERSION/apksigner" \
        "android-sdk/platform-tools/adb" \
        "node-v$RB_ANDROID_NODE_VERSION-linux-x64/bin/node" \
        "node-v$RB_ANDROID_NODE_VERSION-linux-x64/bin/npm" \
        "wrapper/gradlew" \
        "wrapper/gradlew.bat" \
        "wrapper/gradle/wrapper/gradle-wrapper.jar" \
        "smoke/settings.gradle" \
        "mobile-mcp/node_modules/@qwen-code/mobile-mcp/package.json" \
        "mobile-mcp/node_modules/.bin/mcp-server-mobile" \
        "codex/node_modules/@openai/codex/package.json" \
        "codex/node_modules/.bin/codex" \
        "DEPENDENCIES.txt" \
        "manifest.env"; do
        [ -e "$root/$file" ] || missing="$missing $file"
    done
    [ -s "$GRADLE_USER_HOME/.rb-offline-cache-ready" ] || missing="$missing gradle-cache-marker"
    [ -s "$GRADLE_USER_HOME/init.d/recreationbench-offline.gradle" ] || missing="$missing gradle-offline-init"
    if [ -n "$missing" ]; then
        echo "ERROR: Android startup bootstrap is incomplete; missing:$missing" >&2
        return 1
    fi

    [ "$("$RB_ANDROID_NODE_HOME/bin/node" --version 2>/dev/null)" = "v$RB_ANDROID_NODE_VERSION" ] || {
        echo "ERROR: installed Node.js version does not match $RB_ANDROID_NODE_VERSION" >&2
        return 1
    }
    grep -qx "GRADLE_VERSION=$RB_ANDROID_GRADLE_VERSION" "$root/manifest.env" &&
        grep -qx "AGP_VERSION=$RB_ANDROID_AGP_VERSION" "$root/manifest.env" &&
        grep -qx "KOTLIN_VERSION=$RB_ANDROID_KOTLIN_VERSION" "$root/manifest.env" &&
        grep -qx "COMPILE_SDK=$RB_ANDROID_COMPILE_SDK" "$root/manifest.env" &&
        grep -qx "BUILD_TOOLS_VERSION=$RB_ANDROID_BUILD_TOOLS_VERSION" "$root/manifest.env" &&
        grep -qx "MOBILE_MCP_VERSION=$RB_ANDROID_MOBILE_MCP_VERSION" "$root/manifest.env" &&
        grep -qx "CODEX_VERSION=$RB_ANDROID_CODEX_VERSION" "$root/manifest.env" &&
        grep -qx "NODE_VERSION=$RB_ANDROID_NODE_VERSION" "$root/manifest.env" || {
            echo "ERROR: Android bootstrap manifest does not match the pipeline pins" >&2
            return 1
        }
}

bootstrap_android_build_env() {
    local root="$RB_ANDROID_TOOLCHAIN_ROOT"
    command -v java >/dev/null 2>&1 || {
        echo "ERROR: Android bootstrap requires a preinstalled JDK 17" >&2
        return 1
    }
    java -version 2>&1 | head -1 | grep -q 'version "17\.' || {
        echo "ERROR: Android bootstrap requires JDK 17" >&2
        return 1
    }
    for tool in unzip tar; do
        command -v "$tool" >/dev/null 2>&1 || {
            echo "ERROR: Android bootstrap requires $tool in the base image" >&2
            return 1
        }
    done

    mkdir -p "$root/downloads" || return 1
    _android_bootstrap_export_env
    _android_bootstrap_prepare_gradle_cache || return 1
    if [ -s "$root/$RB_ANDROID_BOOTSTRAP_MARKER" ] && _android_bootstrap_validate; then
        _android_bootstrap_install_python || return 1
        echo "==> Android startup dependencies already ready at $root"
        return 0
    fi

    echo "==> Installing pinned Android startup dependencies (network enabled)"
    _android_bootstrap_install_node || return 1
    _android_bootstrap_export_env
    _android_bootstrap_install_sdk || return 1
    _android_bootstrap_install_gradle || return 1
    _android_bootstrap_seed_gradle || return 1
    _android_bootstrap_install_npm_packages || return 1
    _android_bootstrap_install_python || return 1
    _android_bootstrap_write_manifest || return 1
    chmod -R a+rX "$root" "$GRADLE_USER_HOME" || return 1
    _android_bootstrap_validate || return 1
    printf 'ok\n' > "$root/$RB_ANDROID_BOOTSTRAP_MARKER"
    echo "==> Android setup complete; Gradle is now forced offline"
}

prepare_android_gradle_wrapper() {
    local project_dir="$1" wrapper_dir="$RB_ANDROID_TOOLCHAIN_ROOT/wrapper"
    [ -d "$project_dir" ] || mkdir -p "$project_dir"
    [ -s "$wrapper_dir/gradlew" ] &&
        [ -s "$wrapper_dir/gradle/wrapper/gradle-wrapper.jar" ] || {
            echo "ERROR: canonical Gradle wrapper is missing; startup bootstrap did not finish" >&2
            return 1
        }

    cp -f "$wrapper_dir/gradlew" "$project_dir/gradlew" || return 1
    cp -f "$wrapper_dir/gradlew.bat" "$project_dir/gradlew.bat" || return 1
    mkdir -p "$project_dir/gradle/wrapper"
    cp -f "$wrapper_dir/gradle/wrapper/gradle-wrapper.jar" \
        "$project_dir/gradle/wrapper/gradle-wrapper.jar" || return 1
    sed \
        -e "s|__TOOLCHAIN_ROOT__|$RB_ANDROID_TOOLCHAIN_ROOT|g" \
        -e "s|__GRADLE_ARCHIVE__|$RB_ANDROID_GRADLE_ARCHIVE|g" \
        "$(_android_runtime_asset gradle-wrapper.properties)" \
        > "$project_dir/gradle/wrapper/gradle-wrapper.properties"
    chmod +x "$project_dir/gradlew"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    bootstrap_android_build_env
fi
