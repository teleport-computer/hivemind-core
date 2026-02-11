from pydantic import BaseModel, Field
from datetime import datetime


# ── Scope: pre-resolved ID whitelists from caller ──


class Scope(BaseModel):
    user_ids: list[str] | None = None  # None = all users visible
    record_ids: list[str] | None = None  # None = all records visible
    # Both set = AND (record must match both)


# ── Soft Constraints: prompt-level guidance ──


class SoftConstraints(BaseModel):
    detail_level: str = "synthesis"  # "full", "synthesis", "aggregate_only", or any custom string
    custom_instructions: str = ""


# ── Index ──


class IndexEntry(BaseModel):
    title: str
    summary: str
    tags: list[str]
    key_claims: list[str] = []
    extra: dict = {}


# ── Store ──


class StoreRequest(BaseModel):
    text: str = Field(..., min_length=1)
    space_id: str = "public"
    user_id: str | None = None
    metadata: dict | None = None
    index: IndexEntry | None = None  # pre-computed index (skip LLM indexing)
    index_agent_id: str | None = None  # custom indexing agent (sandbox)


class StoreResponse(BaseModel):
    record_id: str
    timestamp: datetime
    index: IndexEntry


# ── Query ──


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    context: str = ""  # the agent's "uploaded brain"
    scope: Scope = Scope()
    soft: SoftConstraints = SoftConstraints()
    querier_id: str | None = None
    query_agent_id: str | None = None  # if set, use sandbox backend with this agent
    scope_agent_id: str | None = None  # if set, run scoping agent to resolve scope
    context_bag: dict = {}  # opaque, for future use


class QueryResponse(BaseModel):
    answer: str
    sources_used: int
    source_ids: list[str]
    audited: bool  # True if mediator successfully audited the output


# ── Health ──


class HealthResponse(BaseModel):
    status: str = "ok"
    record_count: int
    version: str
