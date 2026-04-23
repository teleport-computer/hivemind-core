from pydantic_settings import BaseSettings
from pydantic import model_validator


_LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class Settings(BaseSettings):
    # Storage — Postgres required
    # Direct: postgres://...  |  Via HTTP proxy: https://<cvm>-8080.app.phala.network
    database_url: str = ""
    sql_proxy_key: str = ""  # shared secret for SQL proxy (Phala split deploy)
    api_key: str = ""  # shared secret for HTTP auth; empty = no auth
    host: str = "127.0.0.1"
    port: int = 8100
    cors_allow_origins: str = ""  # comma-separated list; empty = disable CORS

    # LLM (for bridge proxy)
    llm_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "anthropic/claude-sonnet-4.5"
    llm_timeout_seconds: int = 45

    # Docker sandbox
    bridge_host: str = "0.0.0.0"
    docker_host: str = ""
    docker_network: str = "hivemind-sandbox"
    docker_network_internal: bool = True
    enforce_bridge_only_egress: bool = True
    enforce_bridge_only_egress_fail_closed: bool = True
    container_memory_mb: int = 256
    container_cpu_quota: float = 1.0
    container_pids_limit: int = 256
    container_read_only_fs: bool = True
    container_drop_all_caps: bool = True
    container_no_new_privileges: bool = True
    max_llm_calls: int = 50
    max_tokens: int = 300_000
    agent_timeout: int = 300

    # Artifact retention — how long query-agent artifact uploads and run
    # output/error text are kept before the periodic sweeper purges them.
    # Artifacts and run output live in Postgres inside the TEE; there is
    # no external object store. 24h default keeps disk bounded.
    artifact_retention_seconds: int = 86400
    artifact_sweep_interval_seconds: int = 3600

    # Default agents (Docker images) — empty = not available
    autoload_default_agents: bool = True
    default_index_agent: str = ""
    default_query_agent: str = ""
    default_scope_agent: str = ""
    default_mediator_agent: str = ""
    default_index_image: str = ""
    default_query_image: str = ""
    default_scope_image: str = ""
    default_mediator_image: str = ""

    @model_validator(mode="after")
    def _validate_security(self):
        host = (self.host or "").strip().lower()
        if not self.api_key and host not in _LOCAL_BIND_HOSTS:
            raise ValueError(
                "HIVEMIND_API_KEY must be set when HIVEMIND_HOST binds "
                "non-local interfaces"
            )
        return self

    model_config = {"env_prefix": "HIVEMIND_", "env_file": ".env", "extra": "ignore"}
