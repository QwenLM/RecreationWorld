#!/bin/bash
set -eo pipefail

if [ -f /tmp/.rb_bootstrap_done ]; then
    export DISPLAY=:99
    if xdpyinfo -display :99 >/dev/null 2>&1 && pgrep -x openbox >/dev/null 2>&1; then
        echo "Bootstrap already complete, skipping"
        exit 0
    fi
    echo "Bootstrap marker is stale; display session will be rebuilt"
    rm -f /tmp/.rb_bootstrap_done
fi

echo "=== RecreationBench VM Bootstrap ==="
export DEBIAN_FRONTEND=noninteractive

# Elevate to root if needed
if [ "$(id -u)" -ne 0 ]; then
    if sudo -n true 2>/dev/null; then
        exec sudo -E bash "$0" "$@"
    elif [ -n "${RB_SUDO_PASSWORD:-}" ]; then
        echo "$RB_SUDO_PASSWORD" | sudo -S -E bash "$0" "$@"
        exit $?
    else
        echo "ERROR: need root but no passwordless sudo and no RB_SUDO_PASSWORD"
        exit 1
    fi
fi

ensure_ubuntu_updates_source() {
    # Some ECS images have jammy-updates runtime packages installed while the
    # matching binary apt source is missing. Then apt cannot install matching
    # -dev packages such as libadwaita-1-dev or libpango1.0-dev.
    [ -r /etc/os-release ] && . /etc/os-release
    if [ "${ID:-}" != "ubuntu" ] || [ -z "${VERSION_CODENAME:-}" ]; then
        return
    fi
    if grep -RE "^[[:space:]]*deb .* ${VERSION_CODENAME}-updates([[:space:]]|$)" \
        /etc/apt/sources.list /etc/apt/sources.list.d/*.list >/dev/null 2>&1; then
        return
    fi
    mirror="$(awk -v codename="$VERSION_CODENAME" '$1 == "deb" && $2 ~ /^https?:/ && $3 == codename {print $2; exit}' \
        /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null || true)"
    mirror="${mirror:-http://archive.ubuntu.com/ubuntu/}"
    echo "Adding ${VERSION_CODENAME}-updates apt source: $mirror"
    cat > "/etc/apt/sources.list.d/rb-${VERSION_CODENAME}-updates.list" <<EOF
deb $mirror ${VERSION_CODENAME}-updates main restricted universe multiverse
EOF
}

disable_stale_external_apt_sources() {
    # Reused ECS images often carry third-party apt sources (google-chrome, zotero,
    # ...) whose signing keys are missing or whose repos are dead/unsigned, which
    # makes apt-get update fail during bootstrap/build. The VM build stage needs only
    # the Ubuntu archive plus the rb-managed updates source, so disable any other
    # *.list entry (Ubuntu mirrors and deb822 *.sources files are left untouched).
    for file in /etc/apt/sources.list.d/*.list; do
        [ -f "$file" ] || continue
        case "$(basename "$file")" in
            rb-*-updates.list) continue ;;
        esac
        if grep -qiE '^[[:space:]]*deb .*ubuntu' "$file" 2>/dev/null; then
            continue
        fi
        echo "Disabling stale external apt source: $file"
        sed -i 's/^[[:space:]]*deb /# disabled by rb pipeline: deb /' "$file"
    done
}

install_host_cua_driver_if_possible() {
    if command -v cua-driver >/dev/null 2>&1; then
        return
    fi
    # Ensure Docker daemon is running so we can extract from the builder image
    if command -v docker >/dev/null 2>&1; then
        if ! docker info >/dev/null 2>&1; then
            echo "Starting Docker daemon for cua-driver extraction..."
            systemctl start docker 2>/dev/null || service docker start 2>/dev/null || true
            for i in $(seq 1 15); do
                docker info >/dev/null 2>&1 && break
                sleep 1
            done
        fi
    fi
    src=""
    if command -v docker >/dev/null 2>&1 && docker image inspect rb-gui-builder:latest >/dev/null 2>&1; then
        cid="$(docker create rb-gui-builder:latest)"
        docker cp "$cid:/usr/local/bin/cua-driver" /tmp/rb-cua-driver || true
        docker rm "$cid" >/dev/null 2>&1 || true
        if [ -s /tmp/rb-cua-driver ]; then
            src=/tmp/rb-cua-driver
        fi
    fi
    if [ -z "$src" ]; then
        src="$(find /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots \
            -path '*/usr/local/bin/cua-driver' -type f -perm -111 2>/dev/null | head -1 || true)"
    fi
    if [ -n "$src" ] && [ -f "$src" ]; then
        install -m 0755 "$src" /usr/local/bin/cua-driver
        echo "cua-driver extracted from: $src"
    fi
}

artifact_backend_configured() {
    artifact_cli configured >/dev/null 2>&1
}

artifact_cli() {
    PYTHONPATH="/opt/recreationbench/scripts${PYTHONPATH:+:${PYTHONPATH}}" \
        python3 -m infrastructure.artifacts.cli "$@"
}

ensure_artifact_backend() {
    PYTHONPATH="/opt/recreationbench/scripts${PYTHONPATH:+:${PYTHONPATH}}" \
        python3 -m infrastructure.artifacts.provision
}

artifact_cache_fetch() {
    ensure_artifact_backend || return 1
    artifact_cli xfer dl_file "$1" "$2"
}

artifact_cache_prime() {
    ensure_artifact_backend || return 1
    artifact_cli put-if-absent "$1" "$2"
}

select_cua_driver_version() {
    # Optional setup-stage override of the cua-driver binary.
    #   RB_CUA_DRIVER_REF=cua-driver-rs-v<X.Y.Z>  -> build trycua/cua from source (pixel).
    #   RB_CUA_DRIVER_REF=qwen:<X.Y.Z>            -> install the qwen-cua-driver fork
    #       (has the 0-1000 window-normalized-coordinate feature; trycua does not).
    #   RB_CUA_COORDINATE_SPACE=1                 -> wrap the binary so
    #       CUA_DRIVER_RS_COORDINATE_SPACE=1 is exported to EVERY cua-driver invocation
    #       (incl. the recreation agent's long-lived MCP server, where normalization
    #       actually takes effect — a single-shot `call` cannot normalize, per SIZE_CACHE).
    # Unset ref keeps the image-baked binary (default 0.4.2 -> zero change).
    local ref="${RB_CUA_DRIVER_REF:-}"
    local coord="${RB_CUA_COORDINATE_SPACE:-}"
    [ -z "$ref" ] && return 0

    # coord mode may be encoded as a ref suffix (qwen:0.6.8:norm) so it can ride the
    # already-declared rb_cua_driver_ref param without needing a second deployment platform param.
    case "$ref" in
      *:norm) coord=1; ref="${ref%:norm}" ;;
      *+norm) coord=1; ref="${ref%+norm}" ;;
    esac

    # _finalize_cua_driver <binary>: install it as /usr/local/bin/cua-driver. When
    # coord=1, install a tiny wrapper that exports CUA_DRIVER_RS_COORDINATE_SPACE=1 so
    # EVERY invocation normalizes — critically the recreation agent's long-lived MCP
    # server, which inherits the wrapper's env (a single-shot `call` cannot normalize,
    # per-process SIZE_CACHE). Then make /dev/uinput writable so cua-driver >=0.7.0's
    # real-pixel path (uinput/evdev) lands on GTK/Qt/Electron+canvas instead of
    # silently falling back to XSendEvent (0666 is isolation-safe: input, not files).
    _finalize_cua_driver() {
        local bin="$1"
        if [ "$coord" = "1" ]; then
            install -m 0755 "$bin" /opt/cua-driver-real
            install -m 0755 \
                /opt/recreationbench/scripts/linux/runtime/cua_driver_wrapper.sh \
                /usr/local/bin/cua-driver
        else
            install -m 0755 "$bin" /usr/local/bin/cua-driver
        fi
        hash -r
        if [ -e /dev/uinput ]; then
            chmod 0666 /dev/uinput && echo "/dev/uinput -> 0666 (real-pixel uinput enabled for $ref)"
        else
            echo "WARN: /dev/uinput absent; $ref will fall back to XSendEvent (pixel may void on GTK/Qt)"
        fi
    }

    # ── qwen-cua-driver fork ── prebuilt download (fast; ships the 0-1000 window-
    # normalized-coordinate feature that trycua cua-driver-rs lacks). No cargo build
    # and a fork-specific binary, so it uses neither the rust toolchain nor the trycua
    # artifact cache (whose key is trycua-tag-specific).
    case "$ref" in
      qwen:*|qwen-*)
        local qv="${ref#qwen:}"; qv="${qv#qwen-}"
        echo "Installing qwen-cua-driver $qv (fork; setup-stage override)..."
        # Shared self-priming cache: at high concurrency, all jobs downloading the fork
        # from raw.githubusercontent.com rate-limit GitHub -> bootstrap fails. Fetch a
        # prebuilt binary through the artifact adapter; the first job to miss downloads
        # from GitHub and uploads it. Binary is coord-agnostic (norm vs
        # pixel is the wrapper), so the cache key is version-only.
        local cache_prefix="${RB_CUA_DRIVER_CACHE_PREFIX:-RecreationBench/bin}"
        local qkey="${cache_prefix%/}/qwen-cua-driver-${qv}-x86_64"
        if artifact_backend_configured; then
            echo "Trying prebuilt qwen-cua-driver from artifact key $qkey ..."
            if artifact_cache_fetch "$qkey" /tmp/qwen-cua-prebuilt 2>/tmp/qwen_fetch.err; then
                chmod 0755 /tmp/qwen-cua-prebuilt 2>/dev/null || true
                if /tmp/qwen-cua-prebuilt --version 2>/dev/null | grep -qw "$qv"; then
                    _finalize_cua_driver /tmp/qwen-cua-prebuilt; rm -f /tmp/qwen-cua-prebuilt
                    echo "qwen-cua-driver $qv from artifact cache ($([ "$coord" = "1" ] && echo NORMALIZED-0-1000 || echo pixel); ref=$ref, coord=${coord:-0})"
                    return 0
                fi
                echo "  prebuilt version mismatch; downloading from GitHub"; rm -f /tmp/qwen-cua-prebuilt
            else
                echo "  prebuilt fetch miss ($(tail -2 /tmp/qwen_fetch.err 2>/dev/null | tr '\n' ' ')); downloading from GitHub"
            fi
        fi
        curl -fsSL --retry 5 --retry-delay 10 --retry-all-errors \
            "https://raw.githubusercontent.com/QwenLM/qwen-code/cua-driver-rs-v${qv}/packages/cua-driver/scripts/install.sh" \
            -o /tmp/qwen-cua-install.sh \
            || { echo "ERROR: cannot download qwen-cua-driver installer"; exit 1; }
        CUA_DRIVER_RS_VERSION="$qv" HOME=/root bash /tmp/qwen-cua-install.sh >/tmp/qwen-cua-install.log 2>&1 \
            || { echo "ERROR: qwen-cua-driver $qv install failed"; tail -20 /tmp/qwen-cua-install.log; exit 1; }
        local qbin; qbin="$(find /root/.cua-driver -type f -name '*cua-driver*' -perm -111 2>/dev/null | head -1)"
        [ -x "$qbin" ] || { echo "ERROR: qwen-cua-driver binary not found after install"; exit 1; }
        # Self-prime the artifact cache (best-effort, before wrapping) so peers skip GitHub.
        if artifact_backend_configured; then
            artifact_cache_prime "$qbin" "$qkey" \
              2>/tmp/qwen_put.err && echo "  self-primed artifact cache: $qkey" || echo "  artifact cache prime failed ($(tail -2 /tmp/qwen_put.err 2>/dev/null | tr '\n' ' ')); non-fatal"
        fi
        _finalize_cua_driver "$qbin"
        echo "qwen-cua-driver override installed ($([ "$coord" = "1" ] && echo NORMALIZED-0-1000 || echo pixel)): $(cua-driver --version 2>&1) (ref=$ref, coord=${coord:-0})"
        return 0
        ;;
    esac

    # ── trycua cua-driver-rs (pixel) ── artifact cache → source build → self-prime.
    local want="${ref#cua-driver-rs-v}"
    if [ -z "$coord" ] && command -v cua-driver >/dev/null 2>&1 && cua-driver --version 2>/dev/null | grep -qw "$want"; then
        echo "cua-driver $want already installed (ref=$ref); skipping source build"
        return 0
    fi

    local cache_prefix="${RB_CUA_DRIVER_CACHE_PREFIX:-RecreationBench/bin}"
    local cache_key="${cache_prefix%/}/cua-driver-${ref}-x86_64"

    # ── Prebuilt-binary fast path ──────────────────────────────────────────────
    # At high concurrency (e.g. 500 jobs) simultaneous git-clone(github)+cargo-build
    # (crates.io) hit rate limits and fail bootstrap. Fetch a prebuilt binary from
    # the configured artifact store instead. The first job to miss builds
    # from source and uploads it (self-priming cache; see below). Verified via
    # `--version` before use, so a bad/mismatched blob safely falls back to source.
    if artifact_backend_configured; then
        echo "Trying prebuilt cua-driver from artifact key $cache_key ..."
        if artifact_cache_fetch "$cache_key" /tmp/cua-driver-prebuilt 2>/tmp/cua_fetch.err; then
            chmod 0755 /tmp/cua-driver-prebuilt 2>/dev/null || true
            if /tmp/cua-driver-prebuilt --version 2>/dev/null | grep -qw "$want"; then
                _finalize_cua_driver /tmp/cua-driver-prebuilt
                rm -f /tmp/cua-driver-prebuilt
                echo "cua-driver $want installed from artifact cache (ref=$ref, coord=${coord:-0})"
                return 0
            fi
            echo "  prebuilt version mismatch ($(/tmp/cua-driver-prebuilt --version 2>&1 | head -1)); building from source"
            rm -f /tmp/cua-driver-prebuilt
        else
            echo "  prebuilt fetch miss ($(head -1 /tmp/cua_fetch.err 2>/dev/null)); building from source"
        fi
    fi

    echo "Building cua-driver from source at ref=$ref (setup-stage override)..."
    export RUSTUP_HOME=/opt/rb-rust/rustup CARGO_HOME=/opt/rb-rust/cargo
    export PATH="$CARGO_HOME/bin:$PATH"
    if ! command -v cargo >/dev/null 2>&1; then
        echo "  installing rust toolchain (cached at /opt/rb-rust)..."
        if curl -sSf https://rsproxy.cn/rustup/dist/x86_64-unknown-linux-gnu/rustup-init -o /tmp/rustup-init && chmod +x /tmp/rustup-init; then
            RUSTUP_DIST_SERVER=https://rsproxy.cn RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup /tmp/rustup-init -y --no-modify-path --profile minimal || true
        fi
        if ! command -v cargo >/dev/null 2>&1; then
            echo "  rsproxy rustup unavailable; trying official rustup..."
            if curl -sSf https://sh.rustup.rs -o /tmp/rustup-init2 && chmod +x /tmp/rustup-init2; then
                /tmp/rustup-init2 -y --no-modify-path --profile minimal || true
            fi
        fi
        hash -r
    fi
    command -v cargo >/dev/null 2>&1 || { echo "ERROR: cargo unavailable; cannot build cua-driver $ref"; exit 1; }

    local src=/tmp/rb-cua-src
    rm -rf "$src"
    git clone --depth 1 --branch "$ref" https://github.com/trycua/cua.git "$src" \
        || { echo "ERROR: git clone $ref failed"; exit 1; }
    ( cd "$src/libs/cua-driver/rust" && cargo build --release -p cua-driver ) \
        || { echo "ERROR: cargo build of $ref failed"; exit 1; }
    local built_bin="$src/libs/cua-driver/rust/target/release/cua-driver"
    _finalize_cua_driver "$built_bin"
    # Self-prime the artifact cache so concurrent jobs fetch instead of rebuilding (best-
    # effort, idempotent). Upload the RAW binary ($built_bin) — NOT /usr/local/bin/
    # cua-driver, which is the coord wrapper script when coord=1.
    if artifact_backend_configured; then
        artifact_cache_prime "$built_bin" "$cache_key" \
          2>/dev/null && echo "  cua-driver artifact cache primed: $cache_key" || echo "  artifact cache prime skipped"
    fi
    rm -rf "$src"
    echo "cua-driver override installed: $(cua-driver --version 2>&1) (ref=$ref, coord=${coord:-0})"
}

# ── apt lock hardening ──
# Ubuntu 24.04 starts apt-daily/unattended-upgrades shortly after boot.  They can
# grab /var/lib/dpkg/lock-frontend while a later build.sh is installing an app's
# own dependencies.  Persist a bounded wait for every later apt invocation and
# neutralize the background services before this bootstrap's first apt command.
echo "Hardening apt locking (disable unattended-upgrades + dpkg lock timeout)..."
mkdir -p /etc/apt/apt.conf.d
printf 'DPkg::Lock::Timeout "300";\n' > /etc/apt/apt.conf.d/99rb-lock-timeout
if command -v systemctl >/dev/null 2>&1; then
    systemctl stop unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer \
        apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
    systemctl disable unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer \
        apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
    systemctl mask unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer \
        apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
fi
pkill -9 -f unattended-upgr 2>/dev/null || true

# ── apt sources ──
# Released Ubuntu environments already include the complete build, GUI, and
# display toolchain (including Noble's libwxgtk3.2-dev). Keep apt metadata usable
# for small fallback installs without reinstalling that toolchain on every run.
disable_stale_external_apt_sources
ensure_ubuntu_updates_source
# A single unreachable mirror must not consume the complete 2h eval-stage
# budget.  The sandbox image already carries apt metadata and the normal
# runtime dependencies, so a bounded refresh may safely fall back to that
# metadata; any genuinely missing package will still fail at its install.
if ! timeout 300 apt-get update -qq \
    -o Acquire::Retries=3 \
    -o Acquire::http::Timeout=30 \
    -o Acquire::https::Timeout=30; then
    echo "WARNING: apt-get update timed out or failed; continuing with cached package metadata"
fi
command -v wget >/dev/null 2>&1 || apt-get install -y -qq wget 2>&1 | tail -1

# ── Node.js 20 ──
if ! command -v node >/dev/null 2>&1; then
    echo "Installing Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1
    apt-get install -y -qq nodejs 2>&1 | tail -1
    npm install -g yarn electron-builder 2>&1 | tail -1
fi

# ── Node.js 22 (required for Codex CLI) ──
NODE_MAJOR=$(node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1)
if [ "${NODE_MAJOR:-0}" -lt 22 ]; then
    echo "Upgrading Node.js to 22 (required for Codex CLI)..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
    apt-get install -y -qq nodejs 2>&1 | tail -1
    echo "Node.js upgraded: $(node --version)"
fi

# ── Python test dependencies ──
echo "Installing Python test deps..."
pip3 install --break-system-packages pytest pytest-check Pillow numpy pyscreenshot opencv-python-headless 2>/dev/null \
    || pip3 install pytest pytest-check Pillow numpy pyscreenshot opencv-python-headless

rm -rf /var/lib/apt/lists/*

# ── Claude Code ──
# A present binary is not evidence that it is the release binary. Reused sandbox images often
# carry an older CLI, so converge by version just like Codex below.
claude_pin="2.1.177"
claude_have="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
if [ "$claude_have" != "$claude_pin" ]; then
    echo "Installing Claude Code (pinned $claude_pin; have ${claude_have:-missing})..."
    claude_tmp="$(mktemp /usr/local/bin/.claude.rb-download.XXXXXX)"
    trap 'rm -f "$claude_tmp"' EXIT
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 \
        --connect-timeout 15 --max-time 600 \
        "https://downloads.claude.ai/claude-code-releases/2.1.177/linux-x64/claude" \
        -o "$claude_tmp"
    chmod +x "$claude_tmp"
    claude_downloaded="$($claude_tmp --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
    [ "$claude_downloaded" = "$claude_pin" ] || {
        echo "ERROR: downloaded Claude Code version ${claude_downloaded:-invalid}; expected $claude_pin" >&2
        exit 1
    }
    mv -f "$claude_tmp" /usr/local/bin/claude
    trap - EXIT
fi
claude_have="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
[ "$claude_have" = "$claude_pin" ] || {
    echo "ERROR: Claude Code version ${claude_have:-missing}; expected $claude_pin" >&2
    exit 1
}
echo "Claude Code installed: $(claude --version 2>/dev/null || echo unknown)"

# ── Codex CLI (OpenAI) — PINNED ──
# Pin the version: npm 'latest' drifts (was silently installing 0.145.0), breaking
# reproducibility across runs. 0.145.0 is deployment platform-validated — codex-mcp runs the
# recreation end-to-end with the stdin-prompt / --dangerously-bypass-sandbox /
# root-streaming-proxy fixes. Bump deliberately only after re-validating on a
# canary. (config.py CODEX_CLI_VERSION is informational — THIS is the real install.)
codex_have="$(codex --version 2>/dev/null | awk '{print $NF}' || true)"
if [ "$codex_have" != "0.145.0" ]; then
    echo "Installing Codex CLI (pinned 0.145.0)..."
    npm install -g @openai/codex@0.145.0 2>&1 | tail -3
fi
codex_have="$(codex --version 2>/dev/null | awk '{print $NF}' || true)"
[ "$codex_have" = "0.145.0" ] || {
    echo "ERROR: Codex CLI version ${codex_have:-missing}; expected 0.145.0" >&2
    exit 1
}
echo "Codex CLI installed: $(codex --version 2>/dev/null || echo unknown)"

# ── cua-driver ──
install_host_cua_driver_if_possible
select_cua_driver_version
if ! command -v cua-driver >/dev/null 2>&1; then
    echo "ERROR: cua-driver not found after bootstrap" >&2
    exit 1
fi

# Cross-user Qt/Electron accessibility discovers the AT-SPI bus through the
# session bus.  dbus-daemon reads authentication mechanisms only at startup;
# SIGHUP reloads policy but cannot add ANONYMOUS authentication.  Keep this
# before every session-bus discovery/start path below.
cat > /etc/dbus-1/session-local.conf << 'DBUSCONF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <auth>ANONYMOUS</auth>
  <allow_anonymous/>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
    <allow user="*"/>
  </policy>
</busconfig>
DBUSCONF
echo "D-Bus session: enabled cross-user + anonymous access before bus startup"

# ── Display: prefer sandbox native desktop, fallback to Xvfb ──
# Set XAUTHORITY so xdpyinfo can connect to native display sessions
if [ -z "${XAUTHORITY:-}" ]; then
    for auth in /home/user/.Xauthority /run/user/$(id -u user 2>/dev/null || echo 1000)/gdm/Xauthority /var/run/lightdm/user/xauthority; do
        if [ -f "$auth" ]; then
            export XAUTHORITY="$auth"
            echo "Set XAUTHORITY=$auth"
            break
        fi
    done
fi
NATIVE_DISPLAY=""
for d in :0 :1 :2; do
    if xdpyinfo -display "$d" >/dev/null 2>&1; then
        NATIVE_DISPLAY="$d"
        break
    fi
done

if [ -n "$NATIVE_DISPLAY" ]; then
    export DISPLAY="$NATIVE_DISPLAY"
    echo "Using sandbox native display: $DISPLAY"
else
    echo "No native display found; starting Xvfb :99"
    if ! xdpyinfo -display :99 >/dev/null 2>&1; then
        rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
        Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
        for i in $(seq 1 30); do
            xdpyinfo -display :99 >/dev/null 2>&1 && break
            sleep 0.2
        done
        if ! xdpyinfo -display :99 >/dev/null 2>&1; then
            echo "ERROR: Xvfb :99 failed to start"
            exit 1
        fi
    fi
    export DISPLAY=:99
fi

# ── D-Bus + AT-SPI: reuse existing or start new ──
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    for bus_file in /run/user/*/bus; do
        if [ -S "$bus_file" ]; then
            export DBUS_SESSION_BUS_ADDRESS="unix:path=$bus_file"
            echo "Reusing D-Bus session: $DBUS_SESSION_BUS_ADDRESS"
            break
        fi
    done
fi
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    eval $(dbus-launch --sh-syntax)
    export DBUS_SESSION_BUS_ADDRESS
fi
if ! pgrep -u "$(id -u)" -f at-spi2-registryd >/dev/null 2>&1; then
    (/usr/libexec/at-spi2-registryd >/tmp/atspi_registryd.log 2>&1 || true) &
    sleep 2
fi
if pgrep -u "$(id -u)" -f at-spi2-registryd >/dev/null 2>&1; then
    echo "AT-SPI registry: running for uid $(id -u)"
else
    echo "WARNING: no at-spi2-registryd for uid $(id -u); app enumeration will fail" >&2
fi

# ── Window manager: only start if none running ──
if ! wmctrl -m >/dev/null 2>&1; then
    openbox >/tmp/openbox.log 2>&1 &
    sleep 1
fi
echo "Display ready: DISPLAY=$DISPLAY"

# ── Accessibility env vars ──
export GTK_MODULES=gail:atk-bridge
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
export QT_ACCESSIBILITY=1
export ELECTRON_ENABLE_ACCESSIBILITY=1
export GSK_RENDERER=cairo
export LIBGL_ALWAYS_SOFTWARE=1

# ── Persist env for later stages ──
# Prefer native GNOME session bus over bootstrap's dbus-launch bus
_PRE_SWITCH_BUS="${DBUS_SESSION_BUS_ADDRESS:-}"
_USER_UID=$(id -u user 2>/dev/null || echo 1000)
if [ -S "/run/user/$_USER_UID/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$_USER_UID/bus"
fi

# Chromium checks this property before exposing its renderer tree.  Set it on
# the bus later stages actually use, and also on the bootstrap bus if distinct.
_set_screenreader() {
    DBUS_SESSION_BUS_ADDRESS="$1" gdbus call --session \
        --dest org.a11y.Bus \
        --object-path /org/a11y/bus \
        --method org.freedesktop.DBus.Properties.Set \
        org.a11y.Status ScreenReaderEnabled '<boolean true>' >/dev/null 2>&1 || true
}
if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    _set_screenreader "$DBUS_SESSION_BUS_ADDRESS"
    if [ -n "${_PRE_SWITCH_BUS:-}" ] && [ "$_PRE_SWITCH_BUS" != "$DBUS_SESSION_BUS_ADDRESS" ]; then
        _set_screenreader "$_PRE_SWITCH_BUS"
    fi
    sleep 1
    pkill -f orca 2>/dev/null || true
    echo "AT-SPI ScreenReaderEnabled=true on ${DBUS_SESSION_BUS_ADDRESS%%,*}"
fi
{
    printf 'export DISPLAY=%s\n' "$DISPLAY"
    printf 'export DBUS_SESSION_BUS_ADDRESS=%q\n' "${DBUS_SESSION_BUS_ADDRESS:-}"
    [ -n "${XAUTHORITY:-}" ] && printf 'export XAUTHORITY=%q\n' "$XAUTHORITY"
    printf '%s\n' 'export GTK_MODULES=gail:atk-bridge'
    printf '%s\n' 'export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1'
    printf '%s\n' 'export QT_ACCESSIBILITY=1'
    printf '%s\n' 'export GSK_RENDERER=cairo'
    printf '%s\n' 'export LIBGL_ALWAYS_SOFTWARE=1'
    printf '%s\n' 'export ELECTRON_ENABLE_ACCESSIBILITY=1'
    printf '%s\n' 'export XDG_DATA_DIRS=/workspace/install/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}'
} > /tmp/.rb_bootstrap_env

# ── Create workspace base (writable by all for stage scripts running as user) ──
mkdir -p /workspace
chmod 777 /workspace

# ── Runtime users ──
# ACS images already have `user`; the in-pod GUI-builder image does not promise
# it. Both execution locations use the same stage scripts, so establish it here.
if ! id user >/dev/null 2>&1; then
    useradd -m -s /bin/bash user
    echo "Created user for recreation agent"
fi

# ── Configure NOPASSWD sudo for pipeline stages ──
# Bootstrap runs as root; subsequent stages run as the SSH user and need sudo.
for u in user $(who | awk '{print $1}' | sort -u); do
    if id "$u" >/dev/null 2>&1 && [ "$u" != "root" ]; then
        echo "$u ALL=(ALL) NOPASSWD: ALL" > "/etc/sudoers.d/rb-$u"
        chmod 440 "/etc/sudoers.d/rb-$u"
        echo "NOPASSWD sudo configured for: $u"
    fi
done

# ── Create ref_user for recreation dual-user isolation ──
if ! id ref_user >/dev/null 2>&1; then
    useradd -m -s /bin/bash ref_user
    echo "Created ref_user for recreation isolation"
fi

# ── Allow all local users on the AT-SPI accessibility bus ──
# Cross-user AT-SPI requires ANONYMOUS auth so ref_user can connect to user's
# AT-SPI bus without UID-based EXTERNAL auth rejecting the connection.
ATSPI_CONF="/usr/share/defaults/at-spi2/accessibility.conf"
if [ -f "$ATSPI_CONF" ]; then
    if ! grep -q 'allow user="\*"' "$ATSPI_CONF"; then
        sed -i 's|<allow user="root"/>|<allow user="root"/>\n    <allow user="*"/>|' "$ATSPI_CONF"
    fi
    if ! grep -q 'ANONYMOUS' "$ATSPI_CONF"; then
        sed -i 's|<auth>EXTERNAL</auth>|<auth>EXTERNAL</auth>\n  <auth>ANONYMOUS</auth>\n  <allow_anonymous/>|' "$ATSPI_CONF"
    fi
    echo "AT-SPI bus: enabled multi-user + anonymous access"
fi

# Suppress GNOME update notifications (noisy in screenshots)
pkill -9 -f gnome-software 2>/dev/null || true
pkill -9 -f packagekit 2>/dev/null || true
systemctl mask packagekit gnome-software 2>/dev/null || true

touch /tmp/.rb_bootstrap_done
echo "=== Bootstrap complete ==="
