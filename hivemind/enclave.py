import logging

from openai import AsyncOpenAI

from .models import (
    QueryResponse,
    Scope,
    SoftConstraints,
)
from .prompts import build_mediator_prompt
from .storage import Storage
from .tools import build_tools

logger = logging.getLogger(__name__)


async def run_query(
    question: str,
    context: str,
    scope: Scope,
    system: str,
    soft: SoftConstraints,
    querier_id: str | None,
    storage: Storage,
    backend,
    mediator_client: AsyncOpenAI,
    mediator_model: str,
) -> QueryResponse:
    """Execute the query pipeline: tools → agent → mediator.

    Scope and system prompt must already be resolved before calling this.
    The caller decides whether to include HyDE, soft-constraint instructions,
    or an empty system prompt (for sandbox agents).
    """
    # Stage 1: Build scoped tools + source tracking
    tools, on_tool_call, get_source_ids = _build_tracked_tools(storage, scope)

    # Stage 2: Agent execution
    prompt = f"{context}\n\nQuestion: {question}" if context else question
    raw_output = await backend.run(prompt, system, tools, on_tool_call)

    # Stage 3: Mediator audit (soft, LLM-enforced)
    mediated, mediator_ok = await run_mediator(
        mediator_client, mediator_model, raw_output, soft
    )

    source_ids = get_source_ids()
    return QueryResponse(
        answer=mediated,
        sources_used=len(source_ids),
        source_ids=list(source_ids),
        audited=mediator_ok,
    )


def _build_tracked_tools(storage: Storage, scope: Scope):
    """Build scoped tools with source tracking.

    Returns (tools, on_tool_call, get_source_ids) where get_source_ids()
    returns the set of record IDs actually read by the agent.
    """
    tools = build_tools(storage, scope)
    tool_handlers = {t.name: t.handler for t in tools}
    source_ids: set[str] = set()

    async def on_tool_call(name: str, args: dict) -> str:
        if name not in tool_handlers:
            return f"Error: unknown tool '{name}'. Available tools: {', '.join(tool_handlers)}"
        result = tool_handlers[name](**args)
        if name == "read_record" and "record_id" in args:
            if result != "Record not found":
                source_ids.add(args["record_id"])
        return result

    return tools, on_tool_call, lambda: source_ids


async def run_mediator(
    client: AsyncOpenAI, model: str, text: str, soft: SoftConstraints
) -> tuple[str, bool]:
    """Run mediator audit. Returns (text, success).

    On failure, returns unaudited text with success=False so the caller
    can surface this in the response — the caller should know when soft
    constraints were not enforced.
    """
    prompt = build_mediator_prompt(text, soft)
    if prompt is None:
        return text, True  # no audit needed, not a failure

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
        )
        return resp.choices[0].message.content or text, True
    except Exception as e:
        logger.warning("Mediator failed, returning unaudited output: %s", e)
        return text, False
