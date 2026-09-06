# Stock Linux grok-build only. Isolated GROK_HOME at runtime.
# Does not bake secrets. Does not copy host ~/.grok or ~/.claude.json.
# Does not run RimWorld — Windows Steam RimWorld + RIMAPI stay on the host.
#
# Pinned artifact (xAI public CLI):
#   https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-<ver>-linux-x86_64
#   https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-<ver>-linux-aarch64
#
# Build: docker build -f docker/Dockerfile -t rle-grok-build:local .
# Smoke: docker run --rm rle-grok-build:local mcp list

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl zstd \
    && rm -rf /var/lib/apt/lists/*

# Official Linux grok into a disposable HOME so installer config.toml is discarded.
ARG GROK_VERSION=1.0.13
ENV GROK_BIN_DIR=/opt/grok/bin
RUN mkdir -p /tmp/grok-install-home /opt/grok/bin \
    && HOME=/tmp/grok-install-home \
       bash -c 'curl -fsSL https://x.ai/cli/install.sh | bash -s -- ${GROK_VERSION}' \
    && install -m 0755 "$(readlink -f /opt/grok/bin/grok)" /usr/local/bin/grok \
    && rm -rf /tmp/grok-install-home /opt/grok /root/.grok \
    && grok --version \
    && test ! -e /root/.grok/config.toml

COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh \
    && useradd --create-home --uid 1000 --shell /bin/bash grok \
    && mkdir -p /home/grok/.grok /auth /work \
    && chown -R grok:grok /home/grok /auth /work

# Empty writable home. Auth: XAI_API_KEY or /auth/auth.json.
# MCP_URL is the *advertised* RLE McpHost URL (sibling RLE PR: 0.0.0.0:8766).
ENV GROK_HOME=/home/grok/.grok \
    HOME=/home/grok \
    MCP_URL=http://host.docker.internal:8766/mcp \
    PATH=/usr/local/bin:/usr/bin:/bin

USER grok
WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
CMD ["mcp", "list"]
