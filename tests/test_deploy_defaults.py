from pathlib import Path


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


def test_phala_deploy_syncs_default_room_agents_to_hermes():
    deploy_sh = Path("deploy/phala/deploy.sh").read_text()

    assert "HIVEMIND_DEFAULT_INDEX_AGENT \\\n        default-index-hermes" in deploy_sh
    assert "HIVEMIND_DEFAULT_SCOPE_AGENT \\\n        default-scope-hermes" in deploy_sh
    assert "HIVEMIND_DEFAULT_QUERY_AGENT \\\n        default-query-hermes" in deploy_sh
    assert (
        "HIVEMIND_DEFAULT_MEDIATOR_AGENT \\\n        default-mediator-hermes"
        in deploy_sh
    )
