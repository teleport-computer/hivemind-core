from pydantic_settings import BaseSettings
from pydantic import model_validator


_LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class Settings(BaseSettings):
    # Storage — Postgres required
    # Direct: postgres://...  |  Via HTTP proxy: https://<cvm>-8080.app.phala.network
    database_url: str = ""
    sql_proxy_key: str = ""  # shared secret for SQL proxy (Phala split deploy)
    host: str = "127.0.0.1"

    # Multi-tenant control plane. Bearer tokens resolve to per-tenant
    # Hivemind instances via the `_tenants` table in `control_database`.
    # Admin endpoints (POST/DELETE /v1/admin/tenants) are gated by
    # `admin_key` and let the operator mint tenant API keys.
    # `sql_proxy_admin_key` lets this process call sql_proxy's CREATE/DROP
    # DATABASE routes — never exposed outside the core CVM.
    admin_key: str = ""
    sql_proxy_admin_key: str = ""
    control_database: str = "hivemind_control"
    tenant_cache_size: int = 32
    port: int = 8100
    cors_allow_origins: str = ""  # comma-separated list; empty = disable CORS

    # LLM (for bridge proxy)
    llm_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "z-ai/glm-5"
    llm_timeout_seconds: int = 45

    # Optional secondary provider. When ``tinfoil_api_key`` is set, callers
    # can flip the upstream LLM provider per-call by sending
    # ``provider="tinfoil"`` on a QueryRequest (or passing
    # ``--provider tinfoil`` on the CLI). Tinfoil is OpenAI-compatible, so
    # the same SDK is reused with a different ``base_url`` + ``api_key``.
    tinfoil_api_key: str = ""
    tinfoil_base_url: str = "https://inference.tinfoil.sh/v1"

    # Per-role model overrides. Empty falls back to ``llm_model``. Set via
    # HIVEMIND_SCOPE_MODEL / HIVEMIND_QUERY_MODEL / HIVEMIND_MEDIATOR_MODEL.
    # Empty means every stage uses llm_model. Operators can still pin stronger
    # or more conservative models per role when a room needs them.
    scope_model: str = ""
    query_model: str = ""
    mediator_model: str = ""

    # Docker sandbox
    bridge_host: str = "0.0.0.0"
    docker_host: str = ""
    docker_network: str = "hivemind-sandbox"
    docker_network_internal: bool = True
    # Docker build-time isolation for uploaded agent images. Runtime
    # containers are separately hardened below; builds run on the host Docker
    # daemon, so default to no build network, resource caps, and a hard
    # wall-clock cap. Async builds use a separate worker process so a timeout
    # closes the client connection to the daemon instead of leaving an
    # unkillable Python thread blocked in the SDK.
    docker_build_network: str = "none"
    docker_build_timeout_seconds: int = 600
    docker_build_memory_mb: int = 1024
    docker_build_cpu_shares: int = 512
    enforce_bridge_only_egress: bool = True
    enforce_bridge_only_egress_fail_closed: bool = True
    container_memory_mb: int = 256
    container_cpu_quota: float = 1.0
    container_pids_limit: int = 256
    container_user: str = "1000:1000"
    container_read_only_fs: bool = True
    container_drop_all_caps: bool = True
    container_no_new_privileges: bool = True
    # Global ceilings — apply across all agent runs (scope/query/mediator/
    # index). Per-call ``QueryRequest.max_tokens`` / ``--max-llm-calls`` /
    # ``--timeout`` and per-agent ``AgentConfig`` values are clamped to
    # these ceilings, so set them at the highest your operator wants to
    # tolerate. Override via HIVEMIND_MAX_LLM_CALLS / HIVEMIND_MAX_TOKENS /
    # HIVEMIND_AGENT_TIMEOUT.
    max_llm_calls: int = 100
    max_tokens: int = 1_000_000
    agent_timeout: int = 900

    # Billing/metering. Usage is always recorded on run rows when available.
    # Ledger charging happens when a payer tenant is known. Credit enforcement
    # is opt-in so existing deployments can turn metering on before requiring
    # tenant balances.
    billing_enforce_credits: bool = False
    # Public tenant signup. Disabled by default so an operator has to
    # explicitly opt in before unauthenticated callers can mint hmk_ keys.
    # When enabled, POST /v1/signup provisions a tenant with $0 balance.
    # Credit codes are separate and can be redeemed after signup.
    self_serve_signup_enabled: bool = False

    # Credit code that gets auto-redeemed by the server immediately after a
    # /v1/signup mints a tenant. Empty (default) = no auto-credit; the
    # tenant lands at $0 and must redeem manually via
    # POST /v1/billing/credit-codes/redeem. When set to a valid hmcc_*
    # code, the server mints the tenant, then redeems the code against
    # the new tenant before responding so CLI users (`hmctl signup`) get
    # the same first-run-covered experience as the website's /signup. The
    # code's max_redemptions cap is the abuse fence — set it sized to
    # your expected signup volume. Failures (revoked, exhausted, expired)
    # are non-fatal and logged; signup still succeeds.
    signup_starter_credit_code: str = ""

    # Artifact retention — how long query-agent artifact uploads and run
    # output/error text are kept before the periodic sweeper purges them.
    # Artifacts and run output live in Postgres inside the TEE; there is
    # no external object store. 24h default keeps disk bounded.
    artifact_retention_seconds: int = 86400
    artifact_sweep_interval_seconds: int = 3600

    # On-chain governance (feedling's third attestation binding).
    # `app_auth_contract` is the deployed HivemindAppAuth address; when
    # set, the CLI queries `isAppAllowed(compose_hash)` on every
    # `_require_trust` and auto-accepts approved hashes / hard-rejects
    # revoked ones. Empty string disables on-chain gating (TOFU only).
    app_auth_contract: str = ""
    app_auth_chain_id: int = 11155111  # Ethereum Sepolia
    app_auth_rpc_url: str = "https://ethereum-sepolia-rpc.publicnode.com"
    app_auth_explorer_base_url: str = "https://sepolia.etherscan.io"

    # Default agents (Docker images) — empty = not available.
    # The claude_code-harness defaults extend hivemind-agent-base; the
    # hermes-harness defaults extend hivemind-agent-base-hermes and use the
    # NousResearch/hermes-agent loop with native plugin tools.
    autoload_default_agents: bool = True
    default_index_agent: str = ""
    default_query_agent: str = ""
    default_scope_agent: str = ""
    default_mediator_agent: str = ""
    default_index_image: str = ""
    default_query_image: str = ""
    default_scope_image: str = ""
    default_mediator_image: str = ""
    default_index_hermes_agent: str = ""
    default_query_hermes_agent: str = ""
    default_scope_hermes_agent: str = ""
    default_mediator_hermes_agent: str = ""
    default_index_hermes_image: str = ""
    default_query_hermes_image: str = ""
    default_scope_hermes_image: str = ""
    default_mediator_hermes_image: str = ""
    # Directory containing built-in agent Docker contexts. The production
    # core image sets this to /app/agents so default agents can be built
    # locally when GHCR agent packages are private or unreachable.
    bundled_agents_dir: str = ""

    @model_validator(mode="after")
    def _validate_security(self):
        host = (self.host or "").strip().lower()
        if not self.admin_key and host not in _LOCAL_BIND_HOSTS:
            raise ValueError(
                "HIVEMIND_ADMIN_KEY must be set when HIVEMIND_HOST binds "
                "non-local interfaces"
            )
        return self

    model_config = {"env_prefix": "HIVEMIND_", "env_file": ".env", "extra": "ignore"}
