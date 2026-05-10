import subprocess
from pathlib import Path


def test_phala_deploy_script_shell_syntax_is_valid():
    subprocess.run(["bash", "-n", "deploy/phala/deploy.sh"], check=True)


def test_phala_compose_defaults_rooms_to_hermes_agents():
    compose = Path("deploy/phala/docker-compose.core.yaml").read_text()

    assert (
        "HIVEMIND_DEFAULT_INDEX_AGENT: "
        "${HIVEMIND_DEFAULT_INDEX_AGENT:-default-index-hermes}"
    ) in compose
    assert (
        "HIVEMIND_DEFAULT_SCOPE_AGENT: "
        "${HIVEMIND_DEFAULT_SCOPE_AGENT:-default-scope-hermes}"
    ) in compose
    assert (
        "HIVEMIND_DEFAULT_QUERY_AGENT: "
        "${HIVEMIND_DEFAULT_QUERY_AGENT:-default-query-hermes}"
    ) in compose
    assert (
        "HIVEMIND_DEFAULT_MEDIATOR_AGENT: "
        "${HIVEMIND_DEFAULT_MEDIATOR_AGENT:-default-mediator-hermes}"
    ) in compose
    assert "HIVEMIND_ENCLAVE_TLS: ${HIVEMIND_ENCLAVE_TLS:-0}" in compose
    assert 'TARGET_ENDPOINT: "http://hivemind:8100"' in compose
    assert (
        "HIVEMIND_DISABLED_LLM_PROVIDERS: "
        "${HIVEMIND_DISABLED_LLM_PROVIDERS-tinfoil}"
    ) in compose


def test_phala_deploy_syncs_default_room_agents_to_hermes():
    deploy_sh = Path("deploy/phala/deploy.sh").read_text()

    assert 'local image_tag="${IMAGE_SHA:-latest}"' in deploy_sh
    assert "HIVEMIND_DEFAULT_INDEX_AGENT \\\n        default-index-hermes" in deploy_sh
    assert "HIVEMIND_DEFAULT_SCOPE_AGENT \\\n        default-scope-hermes" in deploy_sh
    assert "HIVEMIND_DEFAULT_QUERY_AGENT \\\n        default-query-hermes" in deploy_sh
    assert (
        "HIVEMIND_DEFAULT_MEDIATOR_AGENT \\\n        default-mediator-hermes"
        in deploy_sh
    )
    assert "ghcr.io/teleport-computer" in deploy_sh
    assert 'hivemind-default-query-hermes:${image_tag}' in deploy_sh
    assert "env_file_has_key HIVEMIND_ENCLAVE_TLS" in deploy_sh
    assert "compose_tls_default" in deploy_sh
    assert "is_truthy" in deploy_sh


def test_phala_deploy_guards_update_vs_create_mode():
    deploy_sh = Path("deploy/phala/deploy.sh").read_text()

    assert "require_target_mode_is_safe" in deploy_sh
    assert "already exists but NODE_ID" in deploy_sh
    assert "not found in the active Phala workspace" in deploy_sh
    assert "Do not create a new postgres CVM" in deploy_sh
