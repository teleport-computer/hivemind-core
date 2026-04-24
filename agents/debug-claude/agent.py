"""Diagnostic agent — runs claude-code CLI manually to capture real stderr.

Uploaded as role=scope. Its job is NOT to produce a valid scope_fn, but to
surface the actual failure mode of the claude-code subprocess inside the
Phala CVM container. All output goes to stderr so the host-side logger picks
it up.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback


def log(msg: str) -> None:
    print(f"[debug-claude] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], *, env=None, input_bytes: bytes | None = None, timeout: int = 30) -> None:
    log(f"RUN {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            input=input_bytes,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        log(f"  TIMEOUT after {timeout}s")
        out = (e.stdout or b"").decode("utf-8", errors="replace")
        err = (e.stderr or b"").decode("utf-8", errors="replace")
        for line in out.splitlines()[:60]:
            log(f"  stdout| {line}")
        for line in err.splitlines()[:60]:
            log(f"  stderr| {line}")
        return
    except Exception as e:
        log(f"  EXCEPTION {type(e).__name__}: {e}")
        return
    log(f"  exit={proc.returncode}")
    out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    log(f"  stdout_len={len(out)} stderr_len={len(err)}")
    for line in out.splitlines()[:80]:
        log(f"  stdout| {line}")
    for line in err.splitlines()[:80]:
        log(f"  stderr| {line}")


def main() -> None:
    log("=== environment ===")
    for k in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "BRIDGE_URL",
        "SESSION_TOKEN",
        "HOSTNAME",
        "PATH",
        "HOME",
        "USER",
    ):
        v = os.environ.get(k, "")
        if k in ("ANTHROPIC_API_KEY", "SESSION_TOKEN") and v:
            v = f"<{len(v)}chars>"
        log(f"  {k}={v!r}")

    log("=== versions ===")
    run(["node", "--version"])
    run(["npm", "--version"])
    run(["which", "claude"])
    run(["claude", "--version"])
    run(["python3", "--version"])
    run(["python3", "-c", "import claude_agent_sdk, sys; print(claude_agent_sdk.__version__ if hasattr(claude_agent_sdk, '__version__') else 'no __version__'); print('path=', claude_agent_sdk.__file__)"])

    log("=== reachability test: curl the bridge ===")
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if base:
        run(["curl", "-v", "-m", "5", "-s", "-o", "/dev/null", "-w", "http=%{http_code} time=%{time_total}s\n", f"{base}/v1/messages"])
        # Try a HEAD too
        run(["curl", "-v", "-m", "5", "-sI", f"{base}"])

    log("=== claude --help ===")
    run(["claude", "--help"], timeout=15)

    log("=== claude -p 'say hi' --model claude-sonnet-4-5 (NON-SDK direct) ===")
    env = os.environ.copy()
    # Claude CLI expects ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY, same as SDK.
    run(
        ["claude", "-p", "say hi in one word", "--model", "claude-sonnet-4-5", "--output-format", "json"],
        env=env,
        timeout=60,
    )

    log("=== minimal SDK invocation ===")
    try:
        import asyncio
        from claude_agent_sdk import query, ClaudeAgentOptions
        async def _once():
            msgs = []
            async for m in query(
                prompt="say hi in one word",
                options=ClaudeAgentOptions(
                    permission_mode="bypassPermissions",
                    cwd="/tmp",
                    max_turns=1,
                ),
            ):
                msgs.append(type(m).__name__)
                log(f"  sdk msg: {type(m).__name__}")
            return msgs
        result = asyncio.run(_once())
        log(f"  SDK ok: {len(result)} messages: {result}")
    except Exception as e:
        log(f"  SDK failed: {type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines():
            log(f"  trace| {line}")

    # Emit a stub scope_fn so the pipeline sees a parseable response and
    # doesn't waste more time.
    print(
        json.dumps(
            {"scope_fn": "def scope(sql, params, rows):\n    return {'allow': False, 'error': 'debug-claude diagnostic agent'}\n"}
        ),
        flush=True,
    )
    log("=== done ===")


if __name__ == "__main__":
    main()
