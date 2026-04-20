"""Scope agent, claw-code runtime variant.

Runtime comparison target: same system prompt, same MCP tools, same pipeline
wiring as agents/default-scope/agent.py — but the agent loop runs inside
claw-code (Rust Claude Code clone) instead of claude-agent-sdk (Python).

The stdio MCP proxy (hivemind.mcp_stdio_proxy) bridges claw's MCP client
to our existing bridge HTTP tool endpoints, so execute_sql, get_schema,
verify_scope_fn, and simulate_query work identically to the Python SDK
path.

Reuses:
- SYSTEM_PROMPT from the sibling default-scope agent (same prompt)
- _extract_scope_json for parsing scope_fn emits
- Same POLICY_CONTEXT user_prompt scaffolding
- Same safe-fallback emit on failure

Differs:
- Runs `claw prompt --model $MODEL --mcp-config /tmp/claw_mcp.json
    --system-prompt <system> <user_prompt>` as subprocess
- Parses claw stdout for the scope_fn JSON
- No verify_call_count instrumentation — claw doesn't expose per-tool
  streaming to Python the way claude-agent-sdk does. Retry loop still
  works because it operates on final output only.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

# Reuse extraction logic + SYSTEM_PROMPT from the sibling scope agent.
_SCOPE_DIR = Path("/app/workspace/default-scope")
if str(_SCOPE_DIR) not in sys.path:
    sys.path.insert(0, str(_SCOPE_DIR))

# Scope sibling imports claude_agent_sdk at module load; we only need
# the prompt + extract helpers, so inline minimal copies here rather than
# paying the SDK import cost inside a claw-runtime container.


BRIDGE_URL = os.environ["BRIDGE_URL"]
SESSION_TOKEN = os.environ["SESSION_TOKEN"]
QUERY_AGENT_ID = os.environ.get("QUERY_AGENT_ID", "")
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
POLICY_CONTEXT = os.environ.get("POLICY_CONTEXT", "").strip()
LLM_MODEL = os.environ.get("HIVEMIND_LLM_MODEL", "anthropic/claude-haiku-4.5")
LLM_BASE_URL = os.environ.get("HIVEMIND_LLM_BASE_URL", "https://openrouter.ai/api/v1")
CLAW_BIN = os.environ.get("CLAW_BIN", "/usr/local/bin/claw")


def _load_system_prompt() -> str:
    """Load the scope system prompt verbatim from the sibling agent."""
    prompt_path = _SCOPE_DIR / "agent.py"
    if not prompt_path.exists():
        # Fallback: the default-scope/agent.py file may have been
        # mounted at a different path; emit a minimal placeholder.
        return (
            "You are the ROW TRANSFORMER. Emit a JSON object "
            '{"scope_fn": "def scope(sql, params, rows): ..."} '
            "that transforms raw rows into privacy-safe rows."
        )
    text = prompt_path.read_text("utf-8")
    # Pull the SYSTEM_PROMPT = """...""" block.
    marker = 'SYSTEM_PROMPT = """\\\n'
    # More tolerant search: find the triple-quoted SYSTEM_PROMPT.
    start_idx = text.find('SYSTEM_PROMPT = """')
    if start_idx < 0:
        return ""
    body_start = text.find('"""', start_idx) + 3
    # Skip the optional backslash-newline right after the opening triple quote.
    if text[body_start:body_start + 2] == "\\\n":
        body_start += 2
    body_end = text.find('"""', body_start)
    return text[body_start:body_end]


SYSTEM_PROMPT = _load_system_prompt()


def _looks_like_scope_source(src: str) -> bool:
    if not isinstance(src, str):
        return False
    for line in src.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") or s.startswith("@"):
            continue
        return s.startswith("def scope(") or s.startswith("def scope (")
    return False


def _scrape_def_scope(text: str) -> str | None:
    import re
    m = re.search(r"(?m)^[ \t]*(def\s+scope\s*\([^)]*\)\s*(?:->\s*[^:]+)?\s*:)", text)
    if not m:
        return None
    start = m.start()
    lines = text[start:].splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        stripped = line.lstrip()
        if not stripped:
            out.append(line)
            continue
        if line[:1] not in (" ", "\t"):
            if stripped.startswith("```"):
                break
            break
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) if out else None


def _extract_scope_json(text: str) -> dict | None:
    if not isinstance(text, str):
        return None
    text = text.strip()

    def validate_or_rescue(parsed):
        if not (isinstance(parsed, dict) and "scope_fn" in parsed):
            return None
        if _looks_like_scope_source(parsed["scope_fn"]):
            return parsed
        for candidate in (parsed["scope_fn"], text):
            if isinstance(candidate, str):
                r = _scrape_def_scope(candidate)
                if r and _looks_like_scope_source(r):
                    parsed["scope_fn"] = r
                    return parsed
        return None

    try:
        parsed = json.loads(text)
        result = validate_or_rescue(parsed) if isinstance(parsed, dict) else None
        if result:
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Scan for first { ... } block containing scope_fn.
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[i : j + 1])
                    result = validate_or_rescue(parsed) if isinstance(parsed, dict) else None
                    if result:
                        return result
                except (json.JSONDecodeError, ValueError):
                    pass
                break
    rescued = _scrape_def_scope(text)
    if rescued and _looks_like_scope_source(rescued):
        return {"scope_fn": rescued}
    return None


def _write_claw_mcp_config() -> str:
    """Write ~/.claw/settings.json + return its path.

    Registers our stdio MCP proxy as the 'hivemind' server so claw
    launches it on tool-use. Env vars pass the bridge URL + token.
    """
    home = Path(os.environ.get("HOME", "/tmp"))
    claw_dir = home / ".claw"
    claw_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "mcpServers": {
            "hivemind": {
                "command": "python",
                "args": ["-m", "hivemind.mcp_stdio_proxy"],
                "env": {
                    "BRIDGE_URL": BRIDGE_URL,
                    "SESSION_TOKEN": SESSION_TOKEN,
                    "QUERY_AGENT_ID": QUERY_AGENT_ID,
                    "QUERY_PROMPT": QUERY_PROMPT,
                },
            }
        },
        # Point claw at OpenRouter for OpenAI-compat chat completions.
        "model": LLM_MODEL,
    }
    path = claw_dir / "settings.json"
    path.write_text(json.dumps(config, indent=2))
    return str(path)


def _build_user_prompt() -> str:
    prompt = (
        "Design a scope_fn for the query agent that will answer the user's "
        "question below.\n\n"
        f"User question: {QUERY_PROMPT!r}\n"
        f"Query agent ID: {QUERY_AGENT_ID}\n"
    )
    if POLICY_CONTEXT:
        prompt += (
            "\n---\n"
            "POLICY (authoritative — your scope_fn must enforce this):\n"
            f"{POLICY_CONTEXT}\n"
            "---\n"
        )
    return prompt


def _invoke_claw(user_prompt: str, system_prompt: str, attempt: str) -> tuple[str, int]:
    """Run claw prompt, return (stdout, returncode)."""
    env = os.environ.copy()
    # Route Anthropic-compat traffic through OpenRouter / OpenAI-compat.
    # claw reads OPENAI_BASE_URL + OPENAI_API_KEY for openai_compat provider.
    if LLM_BASE_URL:
        env["OPENAI_BASE_URL"] = LLM_BASE_URL
    env["OPENAI_API_KEY"] = os.environ.get("HIVEMIND_LLM_API_KEY", "")
    # Feed system prompt via CLAUDE.md / claw-system-prompt mechanism:
    # write a temporary file, point claw at it.
    sys_path = Path("/tmp/claw_system_prompt.txt")
    sys_path.write_text(system_prompt)
    # claw doesn't yet document a --system-prompt flag; try settings.json
    # "systemPrompt" or fallback to inlining into the user prompt.
    combined_prompt = (
        "[SYSTEM PROMPT — AUTHORITATIVE]\n"
        f"{system_prompt}\n\n"
        "[USER TASK]\n"
        f"{user_prompt}"
    )
    cmd = [
        CLAW_BIN,
        "--model", LLM_MODEL,
        "--output-format", "text",
        "--dangerously-skip-permissions",
        "prompt", combined_prompt,
    ]
    print(
        f"[scope-claw] INVOKE attempt={attempt} cmd_len={len(' '.join(cmd))}",
        file=sys.stderr,
        flush=True,
    )
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=480,
        )
    except subprocess.TimeoutExpired:
        return "", -1
    stderr = (result.stderr or "")[:2000]
    if stderr.strip():
        print(f"[scope-claw] claw_stderr: {stderr}", file=sys.stderr, flush=True)
    return result.stdout or "", result.returncode


async def _run_single() -> tuple[dict | None, str, str]:
    user_prompt = _build_user_prompt()
    _write_claw_mcp_config()
    stdout, rc = _invoke_claw(user_prompt, SYSTEM_PROMPT, attempt="1/2")
    print(
        f"[scope-claw] returncode={rc} stdout_len={len(stdout)}",
        file=sys.stderr,
        flush=True,
    )
    parsed = _extract_scope_json(stdout) if stdout else None
    if parsed is not None:
        return parsed, "success", parsed.get("scope_fn", "")

    # Retry once with a remediation hint.
    retry_prompt = user_prompt + (
        "\n\n---\nREMEDIATION: Your previous emit was rejected. "
        "You MUST emit exactly `{\"scope_fn\": \"def scope(sql, params, rows): ...\"}` "
        "with the function body inline. Nothing else.\n"
    )
    stdout, rc = _invoke_claw(retry_prompt, SYSTEM_PROMPT, attempt="2/2")
    parsed = _extract_scope_json(stdout) if stdout else None
    if parsed is not None:
        return parsed, "success-retry", parsed.get("scope_fn", "")
    return None, f"no-parse rc={rc}", ""


async def main() -> None:
    print(
        f"[scope-claw] PATH=starting prompt_len={len(QUERY_PROMPT)} "
        f"model={LLM_MODEL}",
        file=sys.stderr,
        flush=True,
    )
    parsed, outcome, full_src = await _run_single()
    if parsed is not None:
        flat = full_src.replace("\n", "\\n")
        print(
            f"[scope-claw] PATH=success scope_fn_full len={len(full_src)} src={flat}",
            file=sys.stderr,
            flush=True,
        )
        print(json.dumps(parsed))
        return
    print(
        f"[scope-claw] PATH=emit-failure REASON={outcome[:200]}",
        file=sys.stderr,
        flush=True,
    )
    # Safe fallback identical to default-scope/agent.py.
    print(json.dumps({
        "scope_fn": (
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, "
            "'rows': [{'policy_note': 'scope agent failed — content redacted by safe fallback', "
            "'match_count': len(rows)}]}"
        )
    }))


if __name__ == "__main__":
    asyncio.run(main())
