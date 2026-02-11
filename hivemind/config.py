from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-sonnet-4.5"
    index_model: str = "anthropic/claude-haiku-4.5"
    mediator_model: str = "anthropic/claude-haiku-4.5"
    anthropic_api_key: str = ""
    agent_backend: str = "openrouter"  # "openrouter" | "claude_sdk"
    encryption_key: str = ""  # Fernet key for encrypting record text; empty = plaintext
    db_path: str = "./hivemind.db"
    api_key: str = ""  # shared secret for HTTP auth, empty = no auth
    hyde_enabled: bool = True  # run HyDE query expansion before agent search
    max_agent_turns: int = 10
    host: str = "0.0.0.0"
    port: int = 8100

    # Sandbox settings (Docker-based agent execution)
    sandbox_enabled: bool = False
    sandbox_bridge_host: str = "0.0.0.0"
    sandbox_docker_network: str = "hivemind-sandbox"
    sandbox_container_memory_mb: int = 256
    sandbox_container_cpu_quota: float = 1.0
    sandbox_max_llm_calls: int = 50
    sandbox_max_tokens: int = 200_000
    sandbox_timeout: int = 300

    model_config = {"env_prefix": "HIVEMIND_", "env_file": ".env"}
