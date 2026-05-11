import asyncio
import logging
from inspect import isawaitable
from pathlib import Path

from .config import Settings
from .db import connect
from .pipeline import Pipeline
from .room_vault import RoomVault
from .rooms import RoomStore
from .sandbox.agents import AgentStore
from .sandbox.artifact_store import ArtifactStore
from .sandbox.backend import _create_runner
from .sandbox.models import AgentConfig, SandboxSettings
from .sandbox.run_store import RunStore
from .sandbox.settings import build_sandbox_settings
from .version import APP_VERSION

logger = logging.getLogger(__name__)


DEFAULT_AGENT_SPECS = (
    (
        "scope",
        "claude_code",
        "default_scope_agent",
        "default_scope_image",
        "default-scope",
    ),
    (
        "query",
        "claude_code",
        "default_query_agent",
        "default_query_image",
        "default-query",
    ),
    (
        "mediator",
        "claude_code",
        "default_mediator_agent",
        "default_mediator_image",
        "default-mediator",
    ),
    (
        "scope",
        "hermes",
        "default_scope_hermes_agent",
        "default_scope_hermes_image",
        "default-scope-hermes",
    ),
    (
        "query",
        "hermes",
        "default_query_hermes_agent",
        "default_query_hermes_image",
        "default-query-hermes",
    ),
    (
        "mediator",
        "hermes",
        "default_mediator_hermes_agent",
        "default_mediator_hermes_image",
        "default-mediator-hermes",
    ),
)


class Hivemind:
    """Thin wrapper: database + pipeline + health.

    `tenant_db` (optional) narrows the DB connection to a specific tenant
    database — the sql_proxy routes by X-Tenant-DB header, or a direct
    psycopg DSN has its dbname swapped. `tenant_id` stamps docker image
    tags so per-tenant agent images don't collide on a shared daemon.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        tenant_db: str | None = None,
        tenant_id: str | None = None,
        sealer=None,
        billing_meter=None,
    ):
        self.settings = settings
        self.tenant_id = tenant_id
        self.tenant_db = tenant_db
        self.sealer = sealer
        self.billing_meter = billing_meter
        self._default_agent_image_warm_inflight = False
        self._default_agent_image_warm_completed = False
        self.db = connect(
            settings.database_url,
            proxy_key=settings.sql_proxy_key,
            tenant_db=tenant_db,
        )
        self.run_store = RunStore(self.db)
        self.artifact_store = ArtifactStore(self.db)
        self.room_store = RoomStore(self.db)
        self.room_vault = RoomVault(self.db, tenant_id=tenant_id)
        self.agent_store = AgentStore(
            self.db,
            sealer=sealer,
            tenant_id=tenant_id,
            room_vault=self.room_vault,
        )
        self.pipeline: Pipeline | None = None
        self._retention_task: asyncio.Task | None = None
        try:
            self._bootstrap_default_agents()
            self.pipeline = Pipeline(
                settings,
                self.db,
                self.agent_store,
                billing_meter=billing_meter,
            )

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

    def start_retention_sweeper(self) -> None:
        """Kick off the periodic TTL sweep for artifacts + run output.

        Called from the FastAPI lifespan startup hook so the task lives on
        the server's event loop. Safe to call multiple times (idempotent).
        """
        if self._retention_task is not None and not self._retention_task.done():
            return
        self._retention_task = asyncio.create_task(self._retention_loop())

    async def stop_retention_sweeper(self) -> None:
        if self._retention_task is None:
            return
        self._retention_task.cancel()
        try:
            await self._retention_task
        except (asyncio.CancelledError, Exception):
            pass
        self._retention_task = None

    async def _retention_loop(self) -> None:
        ttl = self.settings.artifact_retention_seconds
        interval = self.settings.artifact_sweep_interval_seconds
        # Run once immediately so a restart after downtime catches up.
        while True:
            try:
                deleted = await asyncio.to_thread(
                    self.artifact_store.delete_expired, ttl
                )
                scrubbed = await asyncio.to_thread(
                    self.run_store.scrub_expired, ttl
                )
                if deleted or scrubbed:
                    logger.info(
                        "Retention sweep: deleted %d artifacts, "
                        "scrubbed %d runs (ttl=%ds)",
                        deleted, scrubbed, ttl,
                    )
            except Exception as e:
                logger.warning("Retention sweep failed: %s", e)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

    def _build_sandbox_settings(self) -> SandboxSettings:
        return build_sandbox_settings(self.settings)

    def _bundled_agents_root(self) -> Path | None:
        configured = (getattr(self.settings, "bundled_agents_dir", "") or "").strip()
        if configured:
            root = Path(configured)
            if root.is_dir():
                return root

        for root in (
            Path("/app/agents"),
            Path(__file__).resolve().parents[1] / "agents",
        ):
            if root.is_dir():
                return root
        return None

    @staticmethod
    def _image_leaf_name(image: str) -> str:
        leaf = image.rsplit("/", 1)[-1]
        leaf = leaf.split("@", 1)[0]
        return leaf.split(":", 1)[0]

    def _is_trusted_bundled_default_image(
        self,
        *,
        image: str,
        source_name: str,
    ) -> bool:
        leaf = self._image_leaf_name(image)
        if leaf not in {source_name, f"hivemind-{source_name}"}:
            return False

        image_ref = image.split("@", 1)[0]
        image_name = image_ref.rsplit(":", 1)[0]
        if "/" not in image_name:
            return True

        return image_name.lower() in {
            f"ghcr.io/teleport-computer/{source_name}",
            f"ghcr.io/teleport-computer/hivemind-{source_name}",
        }

    def _build_bundled_default_agent_image(
        self,
        runner,
        *,
        image: str,
        source_name: str,
    ) -> bool:
        """Build a trusted built-in default-agent image from bundled source.

        Uploaded agents still use the normal no-network build path. This helper
        is only for repo-owned defaults that are copied into the production core
        image so prod does not depend on public GHCR agent packages.
        """
        if not self._is_trusted_bundled_default_image(
            image=image, source_name=source_name
        ):
            return False

        root = self._bundled_agents_root()
        if root is None:
            return False

        source_dir = root / source_name
        if not (source_dir / "Dockerfile").is_file():
            return False

        try:
            runner.build_image(str(source_dir), image)
            logger.info(
                "Built bundled default agent image %s from %s",
                image, source_dir,
            )
            return True
        except Exception as e:
            raise RuntimeError(
                f"Bundled default agent build failed for {image} "
                f"from {source_dir}: {e}"
            ) from e

    def _bundled_default_agent_files(
        self,
        *,
        image: str,
        source_name: str,
        max_file_size: int = 512_000,
        max_total_size: int = 5_000_000,
    ) -> dict[str, str] | None:
        """Return bundled source context for trusted built-in default-agent refs.

        Tenant construction must not block on Docker builds. For repo-owned
        local tags and GHCR refs, store the bundled Docker context directly in
        Postgres; the normal run path can build the image from that context on
        first use after a CVM redeploy.
        """
        if not self._is_trusted_bundled_default_image(
            image=image, source_name=source_name
        ):
            return None

        root = self._bundled_agents_root()
        if root is None:
            return None
        source_dir = root / source_name
        if not (source_dir / "Dockerfile").is_file():
            return None

        files: dict[str, str] = {}
        total = 0
        skip_dirs = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache"}
        for item in sorted(source_dir.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(source_dir).as_posix()
            if any(part in skip_dirs for part in item.relative_to(source_dir).parts):
                continue
            data = item.read_bytes()
            if len(data) > max_file_size:
                continue
            total += len(data)
            if total > max_total_size:
                raise RuntimeError(
                    f"Bundled default agent context too large for {source_name}"
                )
            try:
                files[rel] = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
        return files or None

    def _bootstrap_default_agents(self) -> None:
        """Auto-register built-in default agents using stable IDs.

        Two harness flavors run side-by-side: the original Claude-Code-SDK
        agents (harness="claude_code") and the NousResearch/hermes-agent
        agents (harness="hermes"). Each flavor is independently gated by
        its own image setting — leaving the image blank skips that flavor.
        """
        if not self.settings.autoload_default_agents:
            return

        runner = None
        for role, harness, agent_key, image_key, fallback_agent_id in DEFAULT_AGENT_SPECS:
            image = (getattr(self.settings, image_key, "") or "").strip()
            if not image:
                continue

            agent_id = (getattr(self.settings, agent_key, "") or "").strip()
            if not agent_id:
                agent_id = fallback_agent_id
                setattr(self.settings, agent_key, agent_id)

            existing = self.agent_store.get(agent_id)
            existing_files = self.agent_store.list_file_paths(agent_id)
            image_changed = existing is not None and existing.image != image
            config = AgentConfig(
                agent_id=agent_id,
                name=fallback_agent_id,
                description=f"Autoloaded default {role} agent ({harness} harness)",
                agent_type=role,
                image=image,
                memory_mb=self.settings.container_memory_mb,
                max_llm_calls=self.settings.max_llm_calls,
                max_tokens=self.settings.max_tokens,
                timeout_seconds=self.settings.agent_timeout,
                harness=harness,
            )

            bundled_files = self._bundled_default_agent_files(
                image=image,
                source_name=fallback_agent_id,
            )
            if bundled_files is not None:
                self.agent_store.upsert(config)
                # Local bundled defaults commonly keep stable :latest tags, so
                # refresh the stored source context even when the image string
                # is unchanged.
                self.agent_store.replace_files(agent_id, bundled_files)
                continue

            if runner is None:
                runner = _create_runner(self._build_sandbox_settings())
            if not runner.image_exists(image):
                built = self._build_bundled_default_agent_image(
                    runner,
                    image=image,
                    source_name=fallback_agent_id,
                )
                if built and runner.image_exists(image):
                    logger.info(
                        "Built default %s/%s image for autoload: %s",
                        role, harness, image,
                    )
                elif built:
                    raise RuntimeError(
                        f"Bundled default {role}/{harness} build reported "
                        f"success but image is still missing: {image}"
                    )
            if not runner.image_exists(image):
                pulled = False
                pull_image = getattr(runner, "pull_image", None)
                if callable(pull_image):
                    pulled = bool(pull_image(image))
                if pulled and runner.image_exists(image):
                    logger.info(
                        "Pulled default %s/%s image for autoload: %s",
                        role, harness, image,
                    )
                else:
                    logger.warning(
                        "Default %s/%s image not found: %s — skipping autoload. "
                        "Build/pull the image or unset HIVEMIND_%s.",
                        role, harness, image, image_key.upper(),
                    )
                    continue
            self.agent_store.upsert(config)

            try:
                if existing_files and not image_changed:
                    continue
                files = runner.extract_image_files(image)
                self.agent_store.replace_files(agent_id, files)
            except Exception as e:
                raise RuntimeError(
                    f"Default {role} agent bootstrap failed for image '{image}': {e}"
                )

    def needs_default_agent_image_warmup(self) -> bool:
        if not self.settings.autoload_default_agents:
            return False
        return (
            not self._default_agent_image_warm_inflight
            and not self._default_agent_image_warm_completed
        )

    def start_default_agent_image_warmup(self) -> bool:
        if not self.needs_default_agent_image_warmup():
            return False
        self._default_agent_image_warm_inflight = True
        return True

    async def warm_default_agent_images(self) -> None:
        """Rebuild missing autoloaded default images without blocking auth.

        Autoload stores bundled source for repo-owned defaults so tenant
        construction stays fast after a CVM redeploy. This background pass
        pays the Docker rebuild cost before the first real room run needs
        scope/query/mediator containers.
        """
        if (
            not self._default_agent_image_warm_inflight
            and not self.start_default_agent_image_warmup()
        ):
            return
        failures = 0
        try:
            runner = _create_runner(self._build_sandbox_settings())
            for spec in DEFAULT_AGENT_SPECS:
                _role, _harness, agent_key, image_key, fallback_agent_id = spec
                image = (getattr(self.settings, image_key, "") or "").strip()
                if not image:
                    continue
                agent_id = (getattr(self.settings, agent_key, "") or "").strip()
                if not agent_id:
                    agent_id = fallback_agent_id
                agent = await asyncio.to_thread(self.agent_store.get, agent_id)
                if agent is None:
                    continue
                try:
                    files = await asyncio.to_thread(
                        lambda: self.agent_store.get_files(
                            agent.agent_id,
                            allow_sealed=True,
                        )
                    )
                    rebuilt = await runner.ensure_image_async(agent.image, files)
                    if rebuilt:
                        logger.info(
                            "Warmed default agent image %s for %s",
                            agent.image,
                            agent.agent_id,
                        )
                except Exception as e:
                    failures += 1
                    logger.warning(
                        "Default agent image warmup failed for %s (%s): %s",
                        agent_id,
                        image,
                        e,
                    )
        except Exception as e:
            failures += 1
            logger.warning("Default agent image warmup setup failed: %s", e)
        finally:
            self._default_agent_image_warm_completed = failures == 0
            self._default_agent_image_warm_inflight = False

    def health(self) -> dict:
        rows = self.db.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        table_count = rows[0]["cnt"] if rows else 0
        return {
            "status": "ok",
            "table_count": table_count,
            "artifact_retention_seconds": self.settings.artifact_retention_seconds,
            "disabled_llm_providers": self.settings.disabled_llm_providers,
            "disabled_llm_routes": self.settings.disabled_llm_routes,
            "version": APP_VERSION,
        }

    async def close(self) -> None:
        """Release network/database resources owned by this instance."""
        await self.stop_retention_sweeper()
        try:
            if self.pipeline is not None:
                for client in self.pipeline.llm_clients.values():
                    llm_close = getattr(client, "close", None)
                    if callable(llm_close):
                        result = llm_close()
                        if isawaitable(result):
                            await result
        finally:
            self.db.close()
