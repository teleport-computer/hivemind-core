from ..tools import Tool


class ClaudeSDKBackend:
    """Claude Code SDK backend. Skeleton — swap in real implementation when ready."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def run(self, prompt: str, system: str, tools: list[Tool], on_tool_call) -> str:
        raise NotImplementedError(
            "Claude SDK backend not yet implemented. "
            "Set HIVEMIND_AGENT_BACKEND=openrouter to use the OpenRouter backend."
        )
