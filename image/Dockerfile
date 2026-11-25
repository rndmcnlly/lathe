# Shared base image for Lathe sandboxes on Daytona v2. Built by build.py.
# Every user can read every file. Daytona's runtime supplies the desktop
# (Xvfb, window manager, x11vnc); this adds a browser and a noVNC page for it.
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      at-spi2-core build-essential ca-certificates chromium curl file fonts-liberation fonts-noto-color-emoji git jq less openssh-client \
      pkg-config procps ripgrep rsync sqlite3 sudo tmux unzip vim-tiny wget xz-utils zip \
 && rm -rf /var/lib/apt/lists/*

# Daytona builds accept one base image, so node and uv come from their releases.
RUN curl -LsSf https://astral.sh/uv/install.sh -o /tmp/install-uv.sh \
 && UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh /tmp/install-uv.sh \
 && rm /tmp/install-uv.sh \
 && cd /tmp \
 && curl -fsSLO https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt \
 && node=$(grep -o 'node-v[0-9.]*-linux-x64.tar.xz' SHASUMS256.txt) \
 && curl -fsSLO "https://nodejs.org/dist/latest-v22.x/$node" \
 && grep " $node$" SHASUMS256.txt | sha256sum -c - \
 && tar -xJf "$node" -C /usr/local --strip-components=1 --exclude='*.md' --exclude='*/LICENSE' \
 && rm -f "$node" SHASUMS256.txt

# websockify only needs numpy to speed up bulk client uploads, which VNC lacks.
RUN pip install --no-cache-dir websockets \
 && pip install --no-cache-dir --no-deps websockify \
 && mkdir -p /opt/novnc \
 && curl -fsSL https://github.com/novnc/noVNC/archive/refs/tags/v1.6.0.tar.gz \
    | tar -xz -C /opt/novnc --strip-components=1

# An ordinary user, so Chromium keeps its own sandbox.
RUN useradd --create-home --uid 1000 --shell /bin/bash daytona \
 && echo 'daytona ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/daytona \
 && chmod 0440 /etc/sudoers.d/daytona \
 && printf 'export PATH=/home/daytona/.local/bin:$PATH\n' > /etc/profile.d/lathe.sh
USER daytona
ENV HOME=/home/daytona \
    NPM_CONFIG_PREFIX=/home/daytona/.local \
    PATH=/home/daytona/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /home/daytona
