"""M5 — mutuals, detail pages, settings, CSV export."""

import csv
import io

import httpx
import pytest
from fastapi.testclient import TestClient

from sonde.api.client import BlueskyClient
from sonde.db import store
from sonde.sync import mutuals
from sonde.web.app import create_app
from tests.fakes import actor, routed, verified


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "depth.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


async def add_follower(profile: dict, rank: int = 0) -> None:
    await store.upsert_actor(profile)
    await store.mark_seen(profile["did"], rank)


def follows_client(pages: list[list[dict]]) -> BlueskyClient:
    def handler(request):
        cursor = request.url.params.get("cursor")
        i = int(cursor) if cursor else 0
        body = {"follows": pages[i]}
        if i + 1 < len(pages):
            body["cursor"] = str(i + 1)
        return httpx.Response(200, json=body)

    return BlueskyClient(
        "https://fake", transport=routed({"app.bsky.graph.getFollows": handler}),
        per_second=1000,
    )


# ------------------------------------------------------------- mutuals

async def test_mutual_is_the_intersection():
    await add_follower(actor("did:plc:both"), 0)
    await add_follower(actor("did:plc:onlyfollower"), 1)

    c = follows_client([[actor("did:plc:both"), actor("did:plc:onlyfollowed")]])
    result = await mutuals.sync_follows(c)
    await c.aclose()

    assert result["follows"] == 2
    assert result["mutuals"] == 1
    counts = await store.counts()
    assert counts["mutuals"] == 1


async def test_unfollowing_removes_the_mutual():
    await add_follower(actor("did:plc:x"), 0)
    c = follows_client([[actor("did:plc:x")]])
    await mutuals.sync_follows(c)
    await c.aclose()
    assert await store.mutual_count() == 1

    c = follows_client([[actor("did:plc:someone-else")]])
    await mutuals.sync_follows(c)
    await c.aclose()

    assert await store.mutual_count() == 0, "stale rows mean I unfollowed them"


async def test_follows_sync_stores_profiles_as_affinity_candidates():
    """The affinity index draws its sources from here, so it needs the profiles."""
    c = follows_client([[actor("did:plc:src1"), actor("did:plc:src2")]])
    await mutuals.sync_follows(c)
    await c.aclose()

    assert await store.get_handle("did:plc:src1") is not None


async def test_mutual_filter_on_the_followers_table():
    await add_follower(actor("did:plc:m"), 0)
    await add_follower(actor("did:plc:not"), 1)
    c = follows_client([[actor("did:plc:m")]])
    await mutuals.sync_follows(c)
    await c.aclose()

    rows = await store.ranked_followers(mutual_only=True)
    assert [r["did"] for r in rows] == ["did:plc:m"]
    assert rows[0]["is_mutual"] == 1


# -------------------------------------------------------- detail page

async def test_detail_gathers_everything_about_one_person():
    await add_follower(verified("did:plc:d", issuer="nytimes.com"), 3)
    await store.add_event("did:plc:d", "followed")
    await store.add_event("did:plc:d", "handle_changed", detail="old → new")

    person = await store.follower_detail("did:plc:d")

    assert person["handle"] == "d.bsky.social"
    assert person["list_rank"] == 3
    assert person["verification_records"][0]["issuerHandle"] == "nytimes.com"
    assert [e["event"] for e in person["events"]] == ["handle_changed", "followed"]


async def test_detail_marks_a_private_follower():
    await add_follower(actor("did:plc:p", labels=[{"val": "!no-unauthenticated"}]), 0)
    person = await store.follower_detail("did:plc:p")
    assert person["is_private"] is True


async def test_detail_returns_none_for_a_stranger():
    assert await store.follower_detail("did:plc:nobody") is None


async def test_detail_route_renders(client):
    await add_follower(verified("did:plc:route"), 0)
    r = client.get("/followers/did:plc:route")
    assert r.status_code == 200
    assert "route.bsky.social" in r.text
    assert "Score breakdown" in r.text or "History" in r.text


async def test_detail_route_404s_for_an_unknown_did(client):
    assert client.get("/followers/did:plc:ghost").status_code == 404


async def test_followers_list_route_still_resolves(client):
    """/followers must not be swallowed by /followers/{did:path}."""
    await add_follower(actor("did:plc:listed"), 0)
    r = client.get("/followers")
    assert r.status_code == 200
    assert "Handle" in r.text


# --------------------------------------------------------------- CSV

async def test_csv_export_has_a_row_per_follower(client):
    await add_follower(verified("did:plc:c1"), 0)
    await add_follower(actor("did:plc:c2"), 1)

    r = client.get("/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]

    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 2
    assert {row["handle"] for row in rows} == {"c1.bsky.social", "c2.bsky.social"}
    assert rows[0]["did"].startswith("did:plc:")


async def test_csv_export_excludes_departed(client):
    await add_follower(actor("did:plc:gone"), 0)
    await store.mark_departed(["did:plc:gone"], reason="unfollow")

    rows = list(csv.DictReader(io.StringIO(client.get("/export.csv").text)))
    assert rows == []


# ---------------------------------------------------------- settings

async def test_settings_route_renders(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Scoring weights" in r.text
    assert "Run a job" in r.text


async def test_settings_shows_the_permanent_gap(client):
    await add_follower(actor("did:plc:s"), 0)
    await store.set_meta("followers_reported", "11451")

    text = client.get("/settings").text
    assert "11,451" in text
    assert "permanent" in text


async def test_settings_shows_the_review_banner_with_an_override(client):
    await store.set_meta("needs_review_count", "213")
    text = client.get("/settings").text
    assert "213" in text
    assert "Accept this sweep" in text


async def test_unknown_job_is_rejected(client):
    assert client.post("/settings/sync/nonsense").status_code == 404


async def test_known_job_redirects_to_settings(client):
    r = client.post("/settings/sync/rescore", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"
