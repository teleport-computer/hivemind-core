from openai import AsyncOpenAI

from ..config import Settings
from .openrouter import OpenRouterBackend


def create_backend(settings: Settings, client: AsyncOpenAI):
    if settings.agent_backend == "claude_sdk":
        from .claude_sdk import ClaudeSDKBackend

        return ClaudeSDKBackend(settings.anthropic_api_key)
    return OpenRouterBackend(client, settings.openrouter_model, settings.max_agent_turns)
