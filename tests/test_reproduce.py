from hivemind import reproduce


def test_blob_to_raw_ignores_render_query_string():
    raw = reproduce.blob_to_raw(
        "https://github.com/teleport-computer/hivemind-core/blob/"
        "abc123/deploy/phala/docker-compose.core.yaml?image_sha=abc1234"
    )

    assert raw == (
        "https://raw.githubusercontent.com/teleport-computer/hivemind-core/"
        "abc123/deploy/phala/docker-compose.core.yaml"
    )


def test_render_registered_compose_applies_image_sha_hint():
    yaml_text = """
services:
  core:
    image: ghcr.io/teleport-computer/hivemind-core:oldtag
  ingress:
    image: dstacktee/dstack-ingress:20250929
""".lstrip()

    rendered, notes = reproduce.render_registered_compose(
        "https://github.com/teleport-computer/hivemind-core/blob/"
        "abc123/deploy/phala/docker-compose.core.yaml?image_sha=abc1234",
        yaml_text,
    )

    assert "ghcr.io/teleport-computer/hivemind-core:abc1234" in rendered
    assert "ghcr.io/teleport-computer/hivemind-core:oldtag" not in rendered
    assert notes == ["core image tag override: abc1234"]
    assert "dstacktee/dstack-ingress:20250929" in rendered


def test_render_registered_compose_leaves_plain_uri_unchanged():
    yaml_text = "services:\n  core:\n    image: ghcr.io/teleport-computer/hivemind-core:oldtag\n"

    rendered, notes = reproduce.render_registered_compose(
        "https://github.com/teleport-computer/hivemind-core/blob/"
        "abc123/deploy/phala/docker-compose.core.yaml",
        yaml_text,
    )

    assert rendered == yaml_text
    assert notes == []
