FROM ubuntu:22.04

ARG NODE_MAJOR=22
ARG CLAUDE_CODE_VERSION=2.1.177
ARG CODEX_VERSION=0.145.0
ARG QWEN_CUA_DRIVER_VERSION=0.7.3

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    DISPLAY=:99 \
    GTK_MODULES=gail:atk-bridge \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    QT_ACCESSIBILITY=1 \
    ELECTRON_ENABLE_ACCESSIBILITY=1 \
    GSK_RENDERER=cairo \
    LIBGL_ALWAYS_SOFTWARE=1 \
    CUA_DRIVER_RS_UPDATE_CHECK=0 \
    CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS=86400 \
    RB_PIPELINE_DIR=/workspace/RecreationBench \
    RB_OUTPUT_DIR=/tmp/recreationbench-output

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg git unzip xz-utils tar rsync jq \
        sudo openssh-server openssh-client sshpass iproute2 procps psmisc \
        python3 python3-pip python3-venv python3-dev \
        build-essential cmake meson ninja-build autoconf automake libtool \
        pkg-config elfutils xsltproc gettext desktop-file-utils \
        appstream libappstream-dev \
        qtbase5-dev qttools5-dev qtwebengine5-dev qtmultimedia5-dev \
        qtdeclarative5-dev libqt5svg5-dev libqt5x11extras5-dev \
        qml-module-qtquick2 qml-module-qtquick-controls2 \
        qt6-base-dev qt6-tools-dev qt6-l10n-tools qt6-declarative-dev \
        qt6-multimedia-dev libqt6svg6-dev \
        libgtk-3-dev libgtkmm-3.0-dev libgtk-4-dev gir1.2-gtk-4.0 libwxgtk3.0-gtk3-dev \
        libadwaita-1-dev libhandy-1-dev valac libgee-0.8-dev \
        flatpak flatpak-builder bubblewrap fuse3 xdg-desktop-portal \
        libsdl2-dev libglew-dev libglfw3-dev libfreeimage-dev \
        libavcodec-dev libavformat-dev libswscale-dev \
        libpoppler-qt5-dev libpoppler-glib-dev \
        libboost-all-dev libssl-dev libcurl4-openssl-dev \
        libsqlite3-dev libxml2-dev libjson-glib-dev \
        libx11-dev libxext-dev libxrender-dev libxrandr-dev \
        xvfb x11-utils dbus-x11 openbox xfce4 xfce4-terminal \
        xdotool wmctrl imagemagick scrot \
        x11vnc novnc websockify netcat-openbsd \
        at-spi2-core python3-gi gir1.2-atspi-2.0 python3-pyatspi \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/noVNC/utils \
    && ln -sfn /usr/share/novnc/app /opt/noVNC/app \
    && ln -sfn /usr/share/novnc/core /opt/noVNC/core \
    && ln -sfn /usr/share/novnc/include /opt/noVNC/include \
    && ln -sfn /usr/share/novnc/vendor /opt/noVNC/vendor \
    && ln -sfn /usr/share/novnc/vnc.html /opt/noVNC/vnc.html \
    && ln -sfn /usr/share/novnc/vnc_lite.html /opt/noVNC/vnc_lite.html \
    && printf '%s\n' \
        '#!/usr/bin/env sh' \
        'set -eu' \
        'vnc="localhost:5900"' \
        'listen="6080"' \
        'web="/opt/noVNC"' \
        'while [ "$#" -gt 0 ]; do' \
        '  case "$1" in' \
        '    --vnc) vnc="$2"; shift 2 ;;' \
        '    --listen) listen="$2"; shift 2 ;;' \
        '    --web) web="$2"; shift 2 ;;' \
        '    *) shift ;;' \
        '  esac' \
        'done' \
        'exec websockify --web "$web" "0.0.0.0:${listen}" "$vnc"' \
        >/opt/noVNC/utils/novnc_proxy \
    && chmod +x /opt/noVNC/utils/novnc_proxy

RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g --no-audit --no-fund yarn electron-builder \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir \
        pytest Pillow numpy pyscreenshot opencv-python-headless paramiko \
        pyyaml openai requests pytest-timeout pyautogui pynput six \
    || python3 -m pip install --no-cache-dir --break-system-packages \
        pytest Pillow numpy pyscreenshot opencv-python-headless paramiko \
        pyyaml openai requests pytest-timeout pyautogui pynput six

RUN curl -fsSL \
        "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/linux-x64/claude" \
        -o /usr/local/bin/claude \
    && chmod +x /usr/local/bin/claude \
    && npm install -g --no-audit --no-fund "@openai/codex@${CODEX_VERSION}"

RUN curl -fsSL \
        "https://raw.githubusercontent.com/QwenLM/qwen-code/cua-driver-rs-v${QWEN_CUA_DRIVER_VERSION}/packages/cua-driver/scripts/install.sh" \
        -o /tmp/qwen-cua-install.sh \
    && CUA_DRIVER_RS_VERSION="${QWEN_CUA_DRIVER_VERSION}" HOME=/root bash /tmp/qwen-cua-install.sh \
    && qbin="$(find /root/.cua-driver -type f -name '*cua-driver*' -perm -111 | head -1)" \
    && test -x "$qbin" \
    && install -m 0755 "$qbin" /usr/local/bin/cua-driver \
    && rm -rf /tmp/qwen-cua-install.sh /root/.cua-driver

RUN useradd -m -s /bin/bash user \
    && usermod --lock user \
    && printf 'user ALL=(ALL) NOPASSWD:ALL\n' >/etc/sudoers.d/recreationbench-user \
    && chmod 0440 /etc/sudoers.d/recreationbench-user \
    && mkdir -p /workspace /home/user/rb_pipeline /tmp/recreationbench-output /run/sshd \
    && chown -R user:user /workspace /home/user/rb_pipeline /tmp/recreationbench-output

ARG RB_SCRIPT_REV=dev
COPY providers/linux/scripts/rb-linux-start /usr/local/bin/rb-linux-start
COPY providers/linux/scripts/rb-linux-entrypoint /usr/local/bin/rb-linux-entrypoint
COPY providers/linux/scripts/rb-linux-verify /usr/local/bin/rb-linux-verify
COPY providers/linux/scripts/rb-start-visual /usr/local/bin/rb-start-visual
COPY providers/linux/bin/rb-linux-sandbox-run /usr/local/bin/rb-linux-sandbox-run
COPY providers/common/controller_helper.py /usr/local/bin/rb-controller-helper
# Install only the provider-neutral runtime from the unified repository.
COPY scripts /workspace/RecreationBench/scripts
COPY src /workspace/RecreationBench/src
COPY tasks /workspace/RecreationBench/tasks
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md /workspace/RecreationBench/
RUN chmod +x /usr/local/bin/rb-linux-entrypoint \
    /usr/local/bin/rb-linux-start \
    /usr/local/bin/rb-linux-verify \
    /usr/local/bin/rb-start-visual \
    /usr/local/bin/rb-linux-sandbox-run \
    /usr/local/bin/rb-controller-helper

WORKDIR /workspace
EXPOSE 22 6080
ENTRYPOINT ["/usr/local/bin/rb-linux-entrypoint"]
CMD ["sleep", "infinity"]
