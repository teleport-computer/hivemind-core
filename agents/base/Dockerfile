FROM python:3.12-slim

# Install Node.js 20 (required by Claude Code CLI)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Pin to known-compatible versions. Post-2.1.109 claude-code + 0.1.66
# claude-agent-sdk crashed at session start (observed 2026-04-25);
# 2.1.109 is the npm `stable` dist-tag.
RUN npm install -g @anthropic-ai/claude-code@2.1.109

RUN pip install --no-cache-dir "claude-agent-sdk==0.1.61" aiohttp

RUN useradd -m -s /bin/bash agent

WORKDIR /app
RUN chown agent:agent /app
ENV PYTHONPATH=/app

USER agent
