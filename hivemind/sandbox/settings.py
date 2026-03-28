from __future__ import annotations

from ..config import Settings
from .models import SandboxSettings


def build_sandbox_settings(settings: Settings) -> SandboxSettings:
    """Map app-level settings into sandbox execution settings."""
    return SandboxSettings(
        backend=settings.sandbox_backend,
        # Docker
        bridge_host=settings.bridge_host,
        docker_host=settings.docker_host,
        docker_network_name=settings.docker_network,
        docker_network_internal=settings.docker_network_internal,
        enforce_bridge_only_egress=settings.enforce_bridge_only_egress,
        enforce_bridge_only_egress_fail_closed=settings.enforce_bridge_only_egress_fail_closed,
        container_memory_mb=settings.container_memory_mb,
        container_cpu_quota=settings.container_cpu_quota,
        container_pids_limit=settings.container_pids_limit,
        container_read_only_fs=settings.container_read_only_fs,
        container_drop_all_caps=settings.container_drop_all_caps,
        container_no_new_privileges=settings.container_no_new_privileges,
        # Phala
        phala_api_key=settings.phala_api_key,
        phala_public_url=settings.phala_public_url,
        # Persistent agent CVM URLs
        phala_scope_url=settings.phala_scope_url,
        phala_index_url=settings.phala_index_url,
        phala_mediator_url=settings.phala_mediator_url,
        # Shared
        global_max_llm_calls=settings.max_llm_calls,
        global_max_tokens=settings.max_tokens,
        global_timeout_seconds=settings.agent_timeout,
    )
