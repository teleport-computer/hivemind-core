from pydantic import BaseModel, Field


VALID_AGENT_TYPES = {"index", "scope", "query", "mediator"}


class AgentConfig(BaseModel):
    """Registered agent definition — Docker image only."""

    agent_id: str
    name: str
    description: str = ""
    agent_type: str = "query"  # index | scope | query | mediator
    image: str  # Docker image reference (e.g. "myorg/my-agent:v1")
    entrypoint: str | None = None  # Override container CMD
    memory_mb: int = Field(default=256, ge=16)  # Container memory limit
    max_llm_calls: int = Field(default=20, ge=1)
    max_tokens: int = Field(default=100_000, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)


class SandboxSettings(BaseModel):
    """Sandbox configuration for Docker agent execution."""

    bridge_host: str = "0.0.0.0"  # containers need to reach bridge
    docker_host: str = ""  # optional explicit Docker daemon socket/URL
    docker_network_name: str = "hivemind-sandbox"
    docker_network_internal: bool = True
    enforce_bridge_only_egress: bool = True
    enforce_bridge_only_egress_fail_closed: bool = True
    container_memory_mb: int = Field(default=256, ge=16)
    container_cpu_quota: float = Field(default=1.0, gt=0.0)
    container_pids_limit: int = Field(default=256, ge=16)
    container_read_only_fs: bool = True
    container_drop_all_caps: bool = True
    container_no_new_privileges: bool = True

    # Shared limits
    global_max_llm_calls: int = Field(default=50, ge=1)
    global_max_tokens: int = Field(default=300_000, ge=1)
    global_timeout_seconds: int = Field(default=300, ge=1)


# ── Bridge request/response models ──


class BridgeLLMRequest(BaseModel):
    """Request from agent container to bridge /llm/chat endpoint."""

    messages: list[dict]
    model: str | None = None  # None = use server default
    max_tokens: int = Field(default=4096, ge=1, le=16384)
    temperature: float | None = None
    top_p: float | None = None


class BridgeLLMResponse(BaseModel):
    """Response from bridge /llm/chat endpoint."""

    content: str
    usage: dict = Field(default_factory=dict)


class BridgeToolRequest(BaseModel):
    """Request from agent container to bridge /tools/{name} endpoint."""

    arguments: dict = Field(default_factory=dict)


class BridgeToolResponse(BaseModel):
    """Response from bridge /tools/{name} endpoint."""

    result: str
    error: str | None = None


class AgentCreateRequest(BaseModel):
    """Request body for POST /v1/agents — register a Docker agent."""

    name: str
    image: str  # Docker image reference (required)
    description: str = ""
    agent_type: str = "query"  # index | scope | query | mediator
    entrypoint: str | None = None
    memory_mb: int = Field(default=256, ge=16)
    max_llm_calls: int = Field(default=20, ge=1)
    max_tokens: int = Field(default=100_000, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)


# ── Simulation models (scope agents only) ──


class OpenAIChatRequest(BaseModel):
    """OpenAI-compatible request for POST /v1/chat/completions on the bridge."""

    model: str | None = None
    messages: list[dict]
    max_tokens: int | None = Field(default=4096, ge=1)
    temperature: float | None = None
    top_p: float | None = None
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None


class AnthropicMessagesRequest(BaseModel):
    """Anthropic-compatible request for POST /v1/messages on the bridge."""

    model: str
    max_tokens: int
    system: str | list[dict] | None = None
    messages: list[dict]
    tools: list[dict] | None = None
    tool_choice: dict | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: list[str] | None = None
    stream: bool = False


class SimulateRequest(BaseModel):
    """Request to POST /sandbox/simulate — run a nested query agent."""

    query_agent_id: str
    prompt: str
    scope_fn_source: str  # Python source for def scope(sql, params, rows): ...
    replay_tape: list[dict] | None = None  # serialized tape entries for replay


class SimulateResponse(BaseModel):
    """Response from POST /sandbox/simulate."""

    output: str
    tape: list[dict] | None = None  # recorded tape from this run


class SimulateBatchRequest(BaseModel):
    """Request to POST /sandbox/simulate_batch — run multiple scope_fn candidates concurrently."""

    query_agent_id: str
    prompt: str
    candidates: list[str] = Field(..., min_length=1, max_length=3)
    replay_tape: list[dict] | None = None


class SimulateBatchItem(BaseModel):
    """One candidate's result from a batch simulate."""

    idx: int
    output: str = ""
    error: str | None = None


class SimulateBatchResponse(BaseModel):
    """Response from POST /sandbox/simulate_batch."""

    results: list[SimulateBatchItem] = Field(default_factory=list)


# ── Verify scope_fn models (scope agents only) ──


class ScopeTestCase(BaseModel):
    """One synthetic test for a candidate scope_fn."""

    sql: str
    params: list = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    expect_allow: bool | None = None  # None = don't assert, just record outcome
    label: str = ""  # human-readable name


class VerifyScopeRequest(BaseModel):
    """Request to POST /sandbox/verify_scope_fn — compile and test a scope_fn."""

    source: str
    tests: list[ScopeTestCase] = Field(default_factory=list)


class ScopeTestResult(BaseModel):
    """Outcome of running a single test case against a compiled scope_fn."""

    label: str = ""
    sql: str
    allow: bool | None = None
    error: str | None = None
    rows_returned: int = 0
    expected_allow: bool | None = None
    passed: bool | None = None  # None if no expectation provided


class VerifyScopeResponse(BaseModel):
    """Response from POST /sandbox/verify_scope_fn."""

    compiles: bool
    compile_error: str | None = None
    all_tests_passed: bool = True  # True if every test with an expectation met it
    results: list[ScopeTestResult] = Field(default_factory=list)


# ── Artifact upload models (query agents with run tracking) ──


class BridgeArtifactUploadRequest(BaseModel):
    """Request from agent container to bridge POST /sandbox/artifact-upload."""

    filename: str
    content_base64: str  # Base64-encoded file content
    content_type: str = "application/octet-stream"


class BridgeArtifactUploadResponse(BaseModel):
    """Response from bridge POST /sandbox/artifact-upload.

    Artifacts are stored in the server's Postgres (encrypted at rest under
    the enclave's KMS key) and fetched via GET path. Retention is the
    server-wide artifact TTL (default 24h).
    """

    path: str  # e.g. /v1/query/runs/{run_id}/artifacts/{filename}
    size_bytes: int
    retention_seconds: int
    error: str | None = None
