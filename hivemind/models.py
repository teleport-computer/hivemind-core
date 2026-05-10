from pydantic import BaseModel, Field, model_validator


# ── Store ──


class StoreRequest(BaseModel):
    sql: str = Field(..., min_length=1)
    params: list = Field(default_factory=list)


class StoreResponse(BaseModel):
    rows: list[dict] = Field(default_factory=list)
    rowcount: int = 0


# ── Query ──


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    room_id: str | None = None
    query_agent_id: str | None = None
    scope_agent_id: str | None = None
    mediator_agent_id: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_llm_calls: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=1)
    # Per-call LLM model override. Empty/None falls back to per-role
    # config (HIVEMIND_SCOPE_MODEL/QUERY_MODEL/MEDIATOR_MODEL) and then
    # to the global HIVEMIND_LLM_MODEL. Use to A/B different models per
    # query, e.g. "z-ai/glm-5" or "moonshotai/kimi-k2.5".
    model: str | None = None
    # Optional per-role model overrides. These let scope stay on a reliable
    # tool-loop model while query uses a stronger prose/research model.
    scope_model: str | None = None
    query_model: str | None = None
    mediator_model: str | None = None
    # Per-call LLM provider override. Accepts "openrouter" (default) or
    # "tinfoil" (requires HIVEMIND_TINFOIL_API_KEY on the server). Lets a
    # recipient flip provider per-question without re-deploying.
    provider: str | None = None
    # Optional privacy/utility policy the scope agent should enforce.
    # Example: "Only allow conversations from the last 30 days; block
    # content from before that window." The scope agent reads this as
    # context and designs its scope_fn to honor it. If None, scope uses
    # generic row-transformation defaults.
    policy: str | None = None

    @model_validator(mode="after")
    def _validate_query(self):
        if not self.query.strip():
            raise ValueError("'query' is required")
        return self


class QueryResponse(BaseModel):
    output: str
    mediated: bool
    usage: dict | None = None


# ── Health ──


class HealthResponse(BaseModel):
    status: str = "ok"
    table_count: int
    version: str
    artifact_retention_seconds: int = 86400
