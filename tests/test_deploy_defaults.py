import subprocess
from pathlib import Path


def test_phala_deploy_script_shell_syntax_is_valid():
    subprocess.run(["bash", "-n", "deploy/phala/deploy.sh"], check=True)


def test_phala_compose_defaults_rooms_to_hermes_agents():
    compose = Path("deploy/phala/docker-compose.core.yaml").read_text()

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
    assert "HIVEMIND_DEFAULT_INDEX_AGENT" not in compose
    assert "HIVEMIND_DEFAULT_INDEX_HERMES_IMAGE" not in compose
    assert "HIVEMIND_ENCLAVE_TLS: ${HIVEMIND_ENCLAVE_TLS:-0}" in compose
    assert 'TARGET_ENDPOINT: "http://hivemind:8100"' in compose
    assert (
        "HIVEMIND_DISABLED_LLM_PROVIDERS: "
        "${HIVEMIND_DISABLED_LLM_PROVIDERS-tinfoil}"
    ) in compose


def test_phala_deploy_syncs_default_room_agents_to_hermes():
    deploy_sh = Path("deploy/phala/deploy.sh").read_text()

    assert 'local image_tag="${IMAGE_SHA:-latest}"' in deploy_sh
    assert "HIVEMIND_DEFAULT_SCOPE_AGENT \\\n        default-scope-hermes" in deploy_sh
    assert "HIVEMIND_DEFAULT_QUERY_AGENT \\\n        default-query-hermes" in deploy_sh
    assert (
        "HIVEMIND_DEFAULT_MEDIATOR_AGENT \\\n        default-mediator-hermes"
        in deploy_sh
    )
    assert "ghcr.io/teleport-computer" in deploy_sh
    assert "hivemind-default-index-hermes" not in deploy_sh
    assert "drop_retired_default_agent_envs" in deploy_sh
    assert "delete_env_key \"${env_file}\" HIVEMIND_DEFAULT_INDEX_AGENT" in deploy_sh
    assert "delete_env_key \"${env_file}\" HIVEMIND_DEFAULT_INDEX_IMAGE" in deploy_sh
    assert (
        "delete_env_key \"${env_file}\" HIVEMIND_DEFAULT_INDEX_HERMES_IMAGE"
        in deploy_sh
    )
    assert 'hivemind-default-query-hermes:${image_tag}' in deploy_sh
    assert "env_file_has_key HIVEMIND_ENCLAVE_TLS" in deploy_sh
    assert "compose_tls_default" in deploy_sh
    assert "is_truthy" in deploy_sh


def test_phala_env_example_excludes_retired_index_defaults():
    env_example = Path("deploy/phala/.env.example").read_text()

    assert "HIVEMIND_DEFAULT_INDEX_AGENT" not in env_example
    assert "HIVEMIND_DEFAULT_INDEX_IMAGE" not in env_example
    assert "hivemind-default-index-hermes" not in env_example


def test_default_hermes_build_matrix_excludes_retired_index_agent():
    workflow = Path(".github/workflows/build-images.yml").read_text()

    assert "role: [query, scope, mediator]" in workflow
    assert "default-index-hermes" not in workflow


def test_post_deploy_hermes_eval_keeps_fast_canary_small():
    workflow = Path(".github/workflows/deploy.yml").read_text()

    assert "hermes_eval:" in workflow
    assert "max_tokens=250000" in workflow
    assert "max_llm_calls=20" in workflow
    assert "timeout_seconds=300" in workflow
    assert "max_tokens=1000000" in workflow
    assert "watch_history_report_artifact" in workflow
    assert "Deep report/artifact canary skipped" in workflow
    assert "Fast canary failed; retrying to filter stochastic" not in workflow


def test_phala_deploy_guards_update_vs_create_mode():
    deploy_sh = Path("deploy/phala/deploy.sh").read_text()

    assert "require_target_mode_is_safe" in deploy_sh
    assert "already exists but NODE_ID" in deploy_sh
    assert "not found in the active Phala workspace" in deploy_sh
    assert "Do not create a new postgres CVM" in deploy_sh
