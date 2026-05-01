import re

from pydantic import BaseModel, Field, field_validator


VALID_AGENT_TYPES = {"index", "scope", "query", "mediator"}
MAX_ARTIFACT_FILENAME_LENGTH = 128
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
_ARTIFACT_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Bounds on /sandbox/simulate replay tapes. Tapes come from a scope agent
# (untrusted) and get fully deserialized into memory before replay starts,
# so unbounded list lengths or per-entry payloads are a memory-DoS vector
# against the bridge process. Limits are intentionally generous — a real
# scope-driven replay sequence is on the order of 10–50 entries.
MAX_REPLAY_TAPE_ENTRIES = 2_000
MAX_REPLAY_TAPE_ENTRY_BYTES = 256 * 1024  # 256 KiB serialized per entry


def _validate_replay_tape(tape: list[dict] | None) -> list[dict] | None:
    """Cap entry count + per-entry serialized size on a replay tape."""
    if tape is None:
        return None
    if len(tape) > MAX_REPLAY_TAPE_ENTRIES:
        raise ValueError(
            f"replay_tape too long: {len(tape)} entries "
            f"(limit {MAX_REPLAY_TAPE_ENTRIES})"
        )
    import json as _json
    for i, entry in enumerate(tape):
        try:
            size = len(_json.dumps(entry, default=str).encode("utf-8"))
        except Exception as exc:
            raise ValueError(f"replay_tape entry {i} not JSON-serializable: {exc}")
        if size > MAX_REPLAY_TAPE_ENTRY_BYTES:
            raise ValueError(
                f"replay_tape entry {i} too large: {size} bytes "
                f"(limit {MAX_REPLAY_TAPE_ENTRY_BYTES})"
            )
    return tape


def validate_artifact_filename(filename: str) -> str:
    """Return a safe artifact filename or raise ValueError.

    Artifact names are used in API paths, response headers, and local CLI
    fetch paths. Keep them basename-only so an untrusted query agent cannot
    write outside the fetch directory or inject headers.
    """
    name = str(filename or "").strip()
    if not name:
        raise ValueError("filename is required")
    if len(name) > MAX_ARTIFACT_FILENAME_LENGTH:
        raise ValueError(
            f"filename too long ({len(name)} > {MAX_ARTIFACT_FILENAME_LENGTH})"
        )
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("filename must be a basename without path traversal")
    if not _ARTIFACT_FILENAME_RE.fullmatch(name):
        raise ValueError(
            "filename may contain only letters, digits, '.', '_' and '-' "
            "and must start with a letter or digit"
        )
    return name


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
    # Per-agent inspection contract. Picked once at upload; query
    # agents inherit from their bound scope agent.
    #   "full"   — owner token can read source via files endpoint.
    #   "sealed" — bytes encrypted for runtime-only use; room uploads use
    #              the room key, reusable room agents use the tenant key.
    #              Files endpoint refuses plaintext to anyone. Image digest
    #              + attested file list still bind the workload.
    inspection_mode: str = "full"


class SandboxSettings(BaseModel):
    """Sandbox configuration for Docker agent execution."""

    bridge_host: str = "0.0.0.0"  # containers need to reach bridge
    docker_host: str = ""  # optional explicit Docker daemon socket/URL
    docker_network_name: str = "hivemind-sandbox"
    docker_network_internal: bool = True
    docker_build_network: str = "none"
    docker_build_timeout_seconds: int = Field(default=600, ge=1)
    docker_build_memory_mb: int = Field(default=1024, ge=128)
    docker_build_cpu_shares: int = Field(default=512, ge=2)
    enforce_bridge_only_egress: bool = True
    enforce_bridge_only_egress_fail_closed: bool = True
    container_memory_mb: int = Field(default=256, ge=16)
    container_cpu_quota: float = Field(default=1.0, gt=0.0)
    container_pids_limit: int = Field(default=256, ge=16)
    container_user: str = "1000:1000"
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
    """Internal request body for registering an already-built Docker image."""

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

    @field_validator("replay_tape")
    @classmethod
    def _bound_replay_tape(cls, v: list[dict] | None) -> list[dict] | None:
        return _validate_replay_tape(v)


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

    @field_validator("replay_tape")
    @classmethod
    def _bound_replay_tape(cls, v: list[dict] | None) -> list[dict] | None:
        return _validate_replay_tape(v)


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

    @field_validator("filename")
    @classmethod
    def _safe_filename(cls, v: str) -> str:
        return validate_artifact_filename(v)

    @field_validator("content_type")
    @classmethod
    def _safe_content_type(cls, v: str) -> str:
        value = (v or "application/octet-stream").strip()
        if not value:
            return "application/octet-stream"
        if len(value) > 100 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("content_type contains invalid characters")
        return value


class BridgeArtifactUploadResponse(BaseModel):
    """Response from bridge POST /sandbox/artifact-upload.

    Artifacts are stored in the server's Postgres and fetched via GET path.
    Retention is the server-wide artifact TTL (default 24h).
    """

    path: str  # e.g. /v1/query/runs/{run_id}/artifacts/{filename}
    size_bytes: int
    retention_seconds: int
    error: str | None = None
