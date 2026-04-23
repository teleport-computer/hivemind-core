FROM python:3.12-slim

# Install Node.js 20 (required by Claude Code CLI)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (latest; diagnostic bump from 2.1.109 → 2.1.112+)
RUN npm install -g @anthropic-ai/claude-code@latest

# Install Python Agent SDK + aiohttp for bridge tool calls
# (diagnostic bump to pick up 0.1.61+ message-reader fixes)
RUN pip install --no-cache-dir --upgrade "claude-agent-sdk>=0.1.61" aiohttp

RUN useradd -m -s /bin/bash agent

WORKDIR /app
RUN chown agent:agent /app
ENV PYTHONPATH=/app

USER agent
