#!/usr/bin/env python3
"""Agent runner server — wraps agent entrypoint as a long-running HTTP service.

Deployed inside persistent agent CVMs (scope, index, mediator). Accepts job
requests from hivemind-core and runs the agent entrypoint as a subprocess.

Zero external dependencies — uses only Python stdlib so it can be dropped into
any agent image without additional installs.

Usage:
    python -m hivemind.sandbox.agent_runner

Environment:
    AGENT_COMMAND  — shell command to run the agent (default: "python _bridge.py")
    RUNNER_PORT    — port to listen on (default: 8080)

Protocol:
    GET  /health           → {"status": "ok"}
    POST /run {"env": {}, "timeout": 300}
         → {"stdout": "...", "stderr": "...", "exit_code": 0, "timed_out": false}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

AGENT_COMMAND = os.environ.get("AGENT_COMMAND", "python _bridge.py")

# Limit concurrent agent runs to prevent resource exhaustion
_run_semaphore = threading.Semaphore(1)


class RunnerHandler(BaseHTTPRequestHandler):
    """HTTP handler for agent run requests."""

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._json_response(404, {"error": "not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response(400, {"error": "empty body"})
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, ValueError) as e:
            self._json_response(400, {"error": f"invalid JSON: {e}"})
            return

        run_env = body.get("env", {})
        timeout = body.get("timeout", 300)
        command = body.get("command", AGENT_COMMAND)

        if not _run_semaphore.acquire(blocking=False):
            self._json_response(
                429, {"error": "agent is already running, try again later"}
            )
            return

        try:
            merged_env = {**os.environ, **run_env}
            proc = subprocess.run(
                command,
                shell=isinstance(command, str),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=merged_env,
            )
            self._json_response(200, {
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "exit_code": proc.returncode,
                "timed_out": False,
            })
        except subprocess.TimeoutExpired:
            self._json_response(200, {
                "stdout": "",
                "stderr": f"Agent timed out after {timeout}s",
                "exit_code": -1,
                "timed_out": True,
            })
        except Exception as e:
            self._json_response(500, {
                "stdout": "",
                "stderr": f"Runner error: {e}",
                "exit_code": -1,
                "timed_out": False,
            })
        finally:
            _run_semaphore.release()

    def _json_response(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # Use compact log format
        sys.stderr.write(f"[runner] {args[0]} {args[1]} {args[2]}\n")


def main() -> None:
    port = int(os.environ.get("RUNNER_PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), RunnerHandler)
    print(f"[runner] Agent runner listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[runner] Shutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
