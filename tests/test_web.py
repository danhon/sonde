"""Route smoke tests. Every route must survive an empty database."""

import pytest
from fastapi.testclient import TestClient

from sonde.db import store
from sonde.web.app import create_app


@pytest.fixture
def client(tmp_path):
    store.set_db_path(str(tmp_path / "web.db"))
    with TestClient(create_app()) as c:
        yield c
    store.set_db_path(None)


def test_healthz_needs_no_database(client):
    """/healthz is the one unauthenticated surface and must never 500.

    The watchdog reads it, so an empty or missing DB has to look alive-but-empty
    rather than dead.
    """
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "uptime_seconds" in body


def test_healthz_leaks_no_follower_data(client):
    body = client.get("/healthz").json()
    assert set(body) <= {"status", "uptime_seconds", "build", "last_sync"}
    blob = repr(body)
    assert "did:" not in blob
    assert "bsky.social" not in blob


def test_dashboard_renders_empty_state(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "No data yet" in r.text
    assert "Followers tracked" in r.text
