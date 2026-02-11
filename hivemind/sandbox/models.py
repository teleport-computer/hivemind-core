from pydantic import BaseModel


class AgentConfig(BaseModel):
    """Registered agent definition — Docker image only."""

    agent_id: str
    name: str
    description: str = ""
    image: str  # Docker image reference (e.g. "myorg/my-agent:v1")
    entrypoint: str | None = None  # Override container CMD
    memory_mb: int = 256  # Container memory limit
    max_llm_calls: int = 20
    max_tokens: int = 100_000
    timeout_seconds: int = 120


class SandboxSettings(BaseModel):
    """Sandbox configuration for Docker-based agent execution."""

    bridge_host: str = "0.0.0.0"  # containers need to reach bridge
    docker_network_name: str = "hivemind-sandbox"
    container_memory_mb: int = 256
    container_cpu_quota: float = 1.0
    global_max_llm_calls: int = 50
    global_max_tokens: int = 200_000
    global_timeout_seconds: int = 300


# ── Bridge request/response models ──


class BridgeLLMRequest(BaseModel):
    """Request from agent container to bridge /llm/chat endpoint.

    The agent controls model selection and all LLM parameters.
    The bridge just forwards to OpenRouter and enforces budget.
    """

    messages: list[dict]
    model: str | None = None  # None = use server default
    max_tokens: int = 4096
    temperature: float | None = None
    top_p: float | None = None


class BridgeLLMResponse(BaseModel):
    """Response from bridge /llm/chat endpoint."""

    content: str
    usage: dict = {}


class BridgeToolRequest(BaseModel):
    """Request from agent container to bridge /tools/{name} endpoint."""

    arguments: dict = {}


class BridgeToolResponse(BaseModel):
    """Response from bridge /tools/{name} endpoint."""

    result: str
    error: str | None = None


class AgentCreateRequest(BaseModel):
    """Request body for POST /v1/agents — register a Docker agent."""

    name: str
    image: str  # Docker image reference (required)
    description: str = ""
    entrypoint: str | None = None
    memory_mb: int = 256
    max_llm_calls: int = 20
    max_tokens: int = 100_000
    timeout_seconds: int = 120
