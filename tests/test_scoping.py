"""Tests for scope resolution (Stage 0 of query pipeline)."""
import pytest

from hivemind.core import Hivemind
from hivemind.config import Settings
from hivemind.models import QueryRequest, Scope


@pytest.fixture
def hivemind(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        openrouter_api_key="test",
        sandbox_enabled=True,
    )
    hm = Hivemind(settings)
    yield hm
    hm.storage.close()


class TestResolveScope:
    """Test _resolve_scope() — the dispatch between static and agent scope."""

    @pytest.mark.asyncio
    async def test_static_scope_passthrough(self, hivemind):
        """When no scope_agent_id, return the static scope from the request."""
        req = QueryRequest(
            question="test",
            scope=Scope(user_ids=["alice"]),
        )
        scope = await hivemind._resolve_scope(req)
        assert scope.user_ids == ["alice"]
        assert scope.record_ids is None

    @pytest.mark.asyncio
    async def test_static_scope_empty(self, hivemind):
        """Empty scope (no restrictions) passes through."""
        req = QueryRequest(question="test")
        scope = await hivemind._resolve_scope(req)
        assert scope.user_ids is None
        assert scope.record_ids is None

    @pytest.mark.asyncio
    async def test_scope_agent_not_found(self, hivemind):
        """Scoping agent that doesn't exist raises ValueError."""
        req = QueryRequest(
            question="test",
            scope_agent_id="nonexistent",
        )
        with pytest.raises(ValueError, match="not found"):
            await hivemind._resolve_scope(req)

    @pytest.mark.asyncio
    async def test_scope_agent_requires_sandbox(self, tmp_path):
        """Scoping agent without sandbox enabled raises ValueError."""
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            openrouter_api_key="test",
            sandbox_enabled=False,
        )
        hm = Hivemind(settings)
        req = QueryRequest(
            question="test",
            scope_agent_id="some-agent",
        )
        with pytest.raises(ValueError, match="sandbox"):
            await hm._resolve_scope(req)
        hm.storage.close()


class TestScopeModel:
    """Test that scope_agent_id field works on QueryRequest."""

    def test_query_request_accepts_scope_agent_id(self):
        req = QueryRequest(
            question="test",
            scope_agent_id="agent123",
        )
        assert req.scope_agent_id == "agent123"

    def test_query_request_default_no_scope_agent(self):
        req = QueryRequest(question="test")
        assert req.scope_agent_id is None

    def test_scope_agent_id_with_static_scope(self):
        """scope_agent_id takes precedence — static scope is ignored."""
        req = QueryRequest(
            question="test",
            scope=Scope(user_ids=["alice"]),
            scope_agent_id="agent123",
        )
        assert req.scope_agent_id == "agent123"
        assert req.scope.user_ids == ["alice"]  # still present on model, but won't be used


