# Stock grok-build + this RLE harness. Isolated GROK_HOME at runtime.
# Does not bake secrets. Does not copy host ~/.grok or ~/.claude.json.
#
# Linux binaries are published by xAI (x.ai/cli/install.sh):
#   https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-<ver>-linux-x86_64
#   https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-<ver>-linux-aarch64
#
# Build: docker build -f docker/Dockerfile -t rle-grok-build:local .
# Run:   docker run --rm -e XAI_API_KEY rle-grok-build:local smoke

FROM python:3.14-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git zstd \
    && rm -rf /var/lib/apt/lists/*

# Install the official Linux grok binary into a disposable HOME so the
# installer-written ~/.grok/config.toml is discarded. Runtime GROK_HOME is empty.
ARG GROK_VERSION=
ENV GROK_BIN_DIR=/opt/grok/bin
RUN mkdir -p /tmp/grok-install-home /opt/grok/bin \
    && HOME=/tmp/grok-install-home \
       bash -c 'curl -fsSL https://x.ai/cli/install.sh | bash -s -- ${GROK_VERSION}' \
    && install -m 0755 "$(readlink -f /opt/grok/bin/grok)" /usr/local/bin/grok \
    && rm -rf /tmp/grok-install-home /opt/grok /root/.grok \
    && grok --version \
    && test ! -e /root/.grok/config.toml

WORKDIR /opt/harness
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        "rimworld-learning-environment[mcp] @ git+https://github.com/AppSprout-dev/RLE" \
        .

COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh \
    && useradd --create-home --uid 1000 --shell /bin/bash rle \
    && mkdir -p /home/rle/.grok /auth /work \
    && chown -R rle:rle /home/rle /auth /work

# Isolated runtime home. Auth arrives via XAI_API_KEY or /auth/auth.json.
ENV GROK_HOME=/home/rle/.grok \
    HOME=/home/rle \
    PYTHONUNBUFFERED=1 \
    RIMAPI_URL=http://host.docker.internal:8765 \
    PATH=/usr/local/bin:/usr/bin:/bin

USER rle
WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
CMD ["smoke"]
