FROM hivemind-agent-base:latest

# Use the absolute path. The runtime invokes containers from /tmp,
# not the WORKDIR, so a bare `python agent.py` fails to find the file.
# All built-in agents (default-scope, default-mediator) follow this
# pattern.
COPY agent.py /app/agent.py
CMD ["python", "/app/agent.py"]
