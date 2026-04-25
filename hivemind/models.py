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
    prompt: str | None = None  # deprecated alias for query
    query_agent_id: str | None = None
    scope_agent_id: str | None = None
    mediator_agent_id: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    # Optional privacy/utility policy the scope agent should enforce.
    # Example: "Only allow conversations from the last 30 days; block
    # content from before that window." The scope agent reads this as
    # context and designs its scope_fn to honor it. If None, scope uses
    # generic row-transformation defaults.
    policy: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_query(cls, data):
        if not isinstance(data, dict):
            return data

        query = data.get("query")
        prompt = data.get("prompt")
        query_text = query.strip() if isinstance(query, str) else ""
        prompt_text = prompt.strip() if isinstance(prompt, str) else ""

        if query_text:
            return data
        if prompt_text:
            payload = dict(data)
            payload["query"] = prompt_text
            return payload
        if query is None:
            raise ValueError("'query' (or 'prompt') is required")
        return data

    @model_validator(mode="after")
    def _validate_query(self):
        if not self.query.strip():
            raise ValueError("'query' (or 'prompt') is required")
        return self


class QueryResponse(BaseModel):
    output: str
    mediated: bool
    usage: dict | None = None


# ── Index ──


class IndexRequest(BaseModel):
    data: str = Field(..., min_length=1)
    metadata: dict = Field(default_factory=dict)
    index_agent_id: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)


class IndexResponse(BaseModel):
    index_text: str
    metadata: dict
    usage: dict | None = None


# ── Health ──


class HealthResponse(BaseModel):
    status: str = "ok"
    table_count: int
    version: str
    artifact_retention_seconds: int = 86400
