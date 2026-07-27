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
    """An exact allowlist: /healthz is the one surface Authelia does not guard.

    Job kinds, counts and ages are fine. Handles and DIDs are not. This asserts
    the key set exactly so a new field cannot leak in unnoticed.
    """
    body = client.get("/healthz").json()
    assert set(body) <= {
        "status", "uptime_seconds", "build", "last_sync",
        "last_sync_age_seconds", "jobs_running", "scheduler",
    }
    blob = repr(body)
    assert "did:" not in blob
    assert "bsky.social" not in blob
    assert "handle" not in blob


def test_healthz_reports_job_and_scheduler_state(client):
    """So the watchdog can see a stalled scheduler, not just a live process."""
    body = client.get("/healthz").json()
    assert body["jobs_running"] == []
    assert body["scheduler"] is False, "no scheduler attached in web-only mode"
    assert "last_sync_age_seconds" in body


def test_dashboard_renders_empty_state(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "No data yet" in r.text
    assert "Followers tracked" in r.text


def test_changes_route_renders_empty_state(client):
    r = client.get("/changes")
    assert r.status_code == 200
    assert "Nothing recorded yet" in r.text


def test_changes_route_accepts_an_event_filter(client):
    r = client.get("/changes?event=departed")
    assert r.status_code == 200


def test_influential_route_renders_empty_state(client):
    r = client.get("/influential")
    assert r.status_code == 200
    assert "Nothing ranked yet" in r.text


def test_followers_route_renders_empty_state(client):
    r = client.get("/followers")
    assert r.status_code == 200
    assert "run a sweep" in r.text


def test_followers_route_accepts_filters(client):
    r = client.get("/followers?q=test&verified=true&min_followers=100&order=followers")
    assert r.status_code == 200


def test_every_route_survives_an_empty_database(client):
    for path in ("/", "/followers", "/influential", "/verified", "/changes", "/healthz"):
        assert client.get(path).status_code == 200, path
