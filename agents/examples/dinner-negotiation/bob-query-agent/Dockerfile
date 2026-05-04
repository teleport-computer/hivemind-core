# Base off hivemind-agent-base so the sandbox builder doesn't need to
# resolve pip at build time. agent.py uses stdlib only (urllib),
# so no extra layers are required.
#
# IMPORTANT: use the absolute path in CMD. The runtime invokes
# containers from /tmp, not the WORKDIR, so a bare `python agent.py`
# fails with No-such-file. All built-in agents (default-scope,
# default-query, default-mediator) follow this pattern.
FROM hivemind-agent-base:latest

COPY agent.py /app/agent.py
COPY b-private/ /app/b-private/
CMD ["python", "/app/agent.py"]
