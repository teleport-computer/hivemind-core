import logging
from inspect import isawaitable

from .config import Settings
from .db import Database, connect
from .pipeline import Pipeline
from .sandbox.agents import AgentStore
from .sandbox.backend import _create_runner
from .sandbox.models import AgentConfig, SandboxSettings
from .sandbox.run_store import RunStore
from .sandbox.settings import build_sandbox_settings
from .version import APP_VERSION

logger = logging.getLogger(__name__)


class Hivemind:
    """Thin wrapper: database + pipeline + health."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = connect(settings.database_url, proxy_key=settings.sql_proxy_key)
        self.agent_store = AgentStore(self.db)
        self.run_store = RunStore(self.db)
        self.s3_uploader = None
        self.pipeline: Pipeline | None = None
        try:
            if settings.s3_bucket:
                from .s3 import S3Uploader
                self.s3_uploader = S3Uploader(settings)

            self._bootstrap_default_agents()
            self.pipeline = Pipeline(settings, self.db, self.agent_store)

            # Cleanup stale containers from previous crashes
            try:
                sandbox_settings = self._build_sandbox_settings()
                _create_runner(sandbox_settings).cleanup_stale_containers()
            except Exception as e:
                logger.debug("Container cleanup skipped: %s", e)
        except Exception:
            try:
                self.db.close()
            except Exception as close_error:
                logger.debug(
                    "Database close failed after init error: %s", close_error
                )
            raise

    def _build_sandbox_settings(self) -> SandboxSettings:
        return build_sandbox_settings(self.settings)

    def _bootstrap_default_agents(self) -> None:
        """Auto-register built-in default agents using stable IDs."""
        if not self.settings.autoload_default_agents:
            return

        specs = (
            (
                "index",
                "default_index_agent",
                "default_index_image",
                "default-index",
            ),
            (
                "scope",
                "default_scope_agent",
                "default_scope_image",
                "default-scope",
            ),
            (
                "query",
                "default_query_agent",
                "default_query_image",
                "default-query",
            ),
            (
                "mediator",
                "default_mediator_agent",
                "default_mediator_image",
                "default-mediator",
            ),
        )

        runner = None
        for role, agent_key, image_key, fallback_agent_id in specs:
            image = (getattr(self.settings, image_key, "") or "").strip()
            if not image:
                continue

            if runner is None:
                runner = _create_runner(self._build_sandbox_settings())
            if not runner.image_exists(image):
                raise RuntimeError(
                    f"Default {role} image not found locally: {image}. "
                    "Build/pull the image or disable autoload."
                )

            agent_id = (getattr(self.settings, agent_key, "") or "").strip()
            if not agent_id:
                agent_id = fallback_agent_id
                setattr(self.settings, agent_key, agent_id)

            existing = self.agent_store.get(agent_id)
            config = AgentConfig(
                agent_id=agent_id,
                name=f"default-{role}",
                description=f"Autoloaded default {role} agent",
                agent_type=role,
                image=image,
                memory_mb=self.settings.container_memory_mb,
                max_llm_calls=self.settings.max_llm_calls,
                max_tokens=self.settings.max_tokens,
                timeout_seconds=self.settings.agent_timeout,
            )
            self.agent_store.upsert(config)

            try:
                existing_files = self.agent_store.list_file_paths(agent_id)
                image_changed = existing is not None and existing.image != image
                if existing_files and not image_changed:
                    continue
                files = runner.extract_image_files(image)
                self.agent_store.replace_files(agent_id, files)
            except Exception as e:
                raise RuntimeError(
                    f"Default {role} agent bootstrap failed for image '{image}': {e}"
                )

    def health(self) -> dict:
        rows = self.db.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        table_count = rows[0]["cnt"] if rows else 0
        return {
            "status": "ok",
            "table_count": table_count,
            "version": APP_VERSION,
        }

    async def close(self) -> None:
        """Release network/database resources owned by this instance."""
        try:
            if self.pipeline is not None:
                llm_close = getattr(self.pipeline.llm_client, "close", None)
                if callable(llm_close):
                    result = llm_close()
                    if isawaitable(result):
                        await result
        finally:
            self.db.close()
