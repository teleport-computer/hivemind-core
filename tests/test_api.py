import tempfile

import pytest
from fastapi.testclient import TestClient

from hivemind.config import Settings
from hivemind.models import IndexEntry
from hivemind.server import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        openrouter_api_key="test",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed_client(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="secret",
        openrouter_api_key="test",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["record_count"] == 0
    assert data["version"] == "0.1.0"


def test_store_with_precomputed_index(client):
    resp = client.post(
        "/v1/store",
        json={
            "text": "The team decided to migrate to Stripe for payments.",
            "space_id": "team-x",
            "user_id": "alice",
            "index": {
                "title": "Payment Migration Decision",
                "summary": "Team chose Stripe for payment processing.",
                "tags": ["payments", "stripe"],
                "key_claims": ["Migrating to Stripe"],
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "record_id" in data
    assert data["index"]["title"] == "Payment Migration Decision"


def test_store_and_health_count(client):
    client.post(
        "/v1/store",
        json={
            "text": "Some text",
            "index": {
                "title": "Title",
                "summary": "Summary",
                "tags": ["tag"],
            },
        },
    )
    resp = client.get("/v1/health")
    assert resp.json()["record_count"] == 1


def test_delete_record(client):
    resp = client.post(
        "/v1/store",
        json={
            "text": "To be deleted",
            "index": {
                "title": "Delete Me",
                "summary": "Will be deleted",
                "tags": ["temp"],
            },
        },
    )
    record_id = resp.json()["record_id"]

    resp = client.delete(f"/v1/records/{record_id}")
    assert resp.status_code == 200

    resp = client.delete(f"/v1/records/{record_id}")
    assert resp.status_code == 404


def test_update_index(client):
    resp = client.post(
        "/v1/store",
        json={
            "text": "Original text",
            "index": {
                "title": "Old Title",
                "summary": "Old",
                "tags": ["old"],
            },
        },
    )
    record_id = resp.json()["record_id"]

    resp = client.patch(
        f"/v1/records/{record_id}/index",
        json={
            "title": "New Title",
            "summary": "New summary",
            "tags": ["new"],
        },
    )
    assert resp.status_code == 200


def test_spaces(client):
    for i in range(3):
        client.post(
            "/v1/store",
            json={
                "text": f"Text {i}",
                "space_id": "space-a" if i < 2 else "space-b",
                "index": {
                    "title": f"Title {i}",
                    "summary": f"Summary {i}",
                    "tags": ["tag"],
                },
            },
        )

    resp = client.get("/v1/spaces")
    assert resp.status_code == 200
    spaces = resp.json()
    assert len(spaces) == 2


def test_auth_required(authed_client):
    resp = authed_client.post(
        "/v1/store",
        json={"text": "test", "index": {"title": "T", "summary": "S", "tags": []}},
    )
    assert resp.status_code == 401

    resp = authed_client.post(
        "/v1/store",
        json={"text": "test", "index": {"title": "T", "summary": "S", "tags": []}},
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200


def test_health_no_auth_required(authed_client):
    # Health endpoint should work without auth
    resp = authed_client.get("/v1/health")
    assert resp.status_code == 200
