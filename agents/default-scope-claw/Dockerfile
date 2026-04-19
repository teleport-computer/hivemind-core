FROM hivemind-agent-sdk-base:latest

# claw binary (built on host, copied via build context).
# Base image runs non-root so we cannot chmod inside the build.
# The binary is already +x on the host from `cargo build --release`,
# and COPY preserves source file mode.
COPY claw /usr/local/bin/claw

# hivemind mcp stdio proxy (copied via build context from hivemind/ subdir)
RUN mkdir -p /app/hivemind
COPY mcp_stdio_proxy.py /app/hivemind/mcp_stdio_proxy.py
RUN touch /app/hivemind/__init__.py

# Vendored default-scope SYSTEM_PROMPT source for prompt extraction.
RUN mkdir -p /workspace/default-scope
COPY default-scope-agent.py /workspace/default-scope/agent.py

# scope-claw agent entrypoint
COPY agent.py /app/agent.py

WORKDIR /app
ENV PYTHONPATH=/app:$PYTHONPATH

CMD ["python", "-u", "/app/agent.py"]
