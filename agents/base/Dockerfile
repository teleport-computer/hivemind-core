# python:3.12-slim, resolved 2026-04-29.
FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3

# Install Node.js 20 (required by Claude Code CLI)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x -o /tmp/nodesource_setup.sh && \
    bash /tmp/nodesource_setup.sh && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -f /tmp/nodesource_setup.sh && \
    rm -rf /var/lib/apt/lists/*

# Pin to known-compatible versions. Post-2.1.109 claude-code + 0.1.66
# claude-agent-sdk crashed at session start (observed 2026-04-25);
# 2.1.109 is the npm `stable` dist-tag.
RUN npm install -g @anthropic-ai/claude-code@2.1.109

RUN pip install --no-cache-dir "claude-agent-sdk==0.1.61" "aiohttp==3.13.5"

RUN useradd -m -s /bin/bash agent

WORKDIR /app
RUN chown agent:agent /app
ENV PYTHONPATH=/app

USER agent
