from __future__ import annotations

from ..config import Settings
from .models import SandboxSettings


def build_sandbox_settings(settings: Settings) -> SandboxSettings:
    """Map app-level settings into sandbox execution settings."""
    return SandboxSettings(
        bridge_host=settings.bridge_host,
        docker_host=settings.docker_host,
        docker_network_name=settings.docker_network,
        docker_network_internal=settings.docker_network_internal,
        docker_build_network=settings.docker_build_network,
        docker_build_timeout_seconds=settings.docker_build_timeout_seconds,
        docker_build_memory_mb=settings.docker_build_memory_mb,
        docker_build_cpu_shares=settings.docker_build_cpu_shares,
        enforce_bridge_only_egress=settings.enforce_bridge_only_egress,
        enforce_bridge_only_egress_fail_closed=settings.enforce_bridge_only_egress_fail_closed,
        container_memory_mb=settings.container_memory_mb,
        container_cpu_quota=settings.container_cpu_quota,
        container_pids_limit=settings.container_pids_limit,
        container_user=settings.container_user,
        container_read_only_fs=settings.container_read_only_fs,
        container_drop_all_caps=settings.container_drop_all_caps,
        container_no_new_privileges=settings.container_no_new_privileges,
        global_max_llm_calls=settings.max_llm_calls,
        global_max_tokens=settings.max_tokens,
        global_timeout_seconds=settings.agent_timeout,
        debug_trace_enabled=settings.debug_trace_enabled,
        debug_trace_max_entries=settings.debug_trace_max_entries,
        debug_trace_max_chars_per_entry=settings.debug_trace_max_chars_per_entry,
    )
