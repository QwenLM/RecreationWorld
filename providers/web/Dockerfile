FROM ubuntu:24.04

ARG NODE_VERSION=22.18.0
ARG PLAYWRIGHT_VERSION=1.60.0
ARG PLAYWRIGHT_MCP_VERSION=0.0.79
ARG CLAUDE_CODE_VERSION=2.1.177
ARG CODEX_VERSION=0.145.0
ARG CLAUDE_CODE_PROXY_REF=acabbf6bb6ec33bb31ce17ca90ba83dbd16239aa
ARG PLAYWRIGHT_DOWNLOAD_HOST=

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST} \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    RB_PIPELINE_DIR=/workspace/RecreationBench \
    RB_OUTPUT_DIR=/tmp/recreationbench-output

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv python3-dev \
        curl ca-certificates git unzip xz-utils rsync jq util-linux sudo \
        build-essential pkg-config \
        xvfb x11-utils openbox xfce4 x11vnc novnc websockify netcat-openbsd \
        libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libpango-1.0-0 libcairo2 libasound2t64 fonts-liberation \
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

RUN arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) node_arch="x64" ;; \
        arm64) node_arch="arm64" ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
        | tar -xJ -C /usr/local --strip-components=1

RUN npm install -g --no-audit --no-fund \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "@playwright/test@${PLAYWRIGHT_VERSION}" \
        "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"

RUN mkdir -p /opt/mockweb-bench/batch_run \
    && cd /opt/mockweb-bench/batch_run \
    && npm init -y --silent \
    && npm install --no-audit --no-fund --silent "@playwright/test@${PLAYWRIGHT_VERSION}"

RUN pip install --no-cache-dir --break-system-packages --ignore-installed \
        "playwright==${PLAYWRIGHT_VERSION}" \
        pydantic pyyaml requests numpy Pillow opencv-python-headless scikit-image \
        beautifulsoup4 lxml cssutils openai \
        "git+https://github.com/seanbabalala/claude-code-proxy.git@${CLAUDE_CODE_PROXY_REF}" \
    && python3 -m playwright install chromium

RUN pip install --no-cache-dir --break-system-packages \
        --index-url https://download.pytorch.org/whl/cpu \
        torch torchvision \
    && pip install --no-cache-dir --break-system-packages scipy lpips

RUN useradd -m -s /bin/bash user \
    && useradd -m -s /bin/bash agent \
    && usermod --lock user \
    && printf 'user ALL=(ALL) NOPASSWD:ALL\n' >/etc/sudoers.d/recreationbench-user \
    && chmod 0440 /etc/sudoers.d/recreationbench-user \
    && mkdir -p /workspace /tmp/recreationbench-output /ms-playwright \
    && chmod 0755 /ms-playwright \
    && chown -R user:user /workspace /tmp/recreationbench-output \
    && chown -R agent:agent /home/agent

COPY providers/web/scripts/rb-web-verify /usr/local/bin/rb-web-verify
COPY providers/web/scripts/rb-start-visual /usr/local/bin/rb-start-visual
COPY providers/web/bin/rb-web-sandbox-run /usr/local/bin/rb-web-sandbox-run
COPY providers/web/scripts/rb-claude-code-proxy /usr/local/bin/rb-claude-code-proxy
COPY providers/common/controller_helper.py /usr/local/bin/rb-controller-helper
# Install only the provider-neutral runtime from the unified repository.
COPY scripts /workspace/RecreationBench/scripts
COPY src /workspace/RecreationBench/src
COPY tasks /workspace/RecreationBench/tasks
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md /workspace/RecreationBench/
RUN chmod +x /usr/local/bin/rb-web-verify /usr/local/bin/rb-start-visual /usr/local/bin/rb-web-sandbox-run /usr/local/bin/rb-claude-code-proxy /usr/local/bin/rb-controller-helper

WORKDIR /workspace
EXPOSE 6080
CMD ["sleep", "infinity"]
