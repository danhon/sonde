"""One-click follow-back — the only thing sonde writes to Bluesky.

Every other test in this suite is about reading. This one creates a public
record on a real account under the operator's name, so most of what follows is
about the cases where it must refuse.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from sonde.api import auth as auth_module
from sonde.api.client import BlueskyClient
from sonde.api.follows import FollowError, create_follow, delete_follow
from sonde.db import store
from tests.fakes import actor

TEMPLATES = Path(__file__).resolve().parents[1] / "sonde" / "web" / "templates"


class FakeSession:
    did = "did:plc:me"
    handle = "danhon.com"


def routed(handler):
    return BlueskyClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(auth_module.authenticator, "session", FakeSession())

    async def token():
        return "fake-jwt"

    monkeypatch.setattr(auth_module.authenticator, "token", token)


async def follower(did: str, *, current: bool = True,
                   ignored: bool = False) -> None:
    await store.upsert_actor(actor(did))
    await store.mark_seen(did, 0)
    db = await store._db()
    if not current:
        await db.execute("UPDATE follower_state SET is_current = 0 WHERE did = ?",
                         (did,))
    if ignored:
        await db.execute(
            "UPDATE follower_state SET ignored_at = '2026-01-01' WHERE did = ?",
            (did,))
    await db.commit()


# ------------------------------------------------- the write itself

async def test_a_follow_posts_a_record_and_returns_its_uri(signed_in):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"uri": "at://did:plc:me/app.bsky.graph.follow/abc"})

    client = routed(handler)
    uri = await create_follow(client, "did:plc:them")
    await client.aclose()

    assert uri.endswith("/abc")
    assert "com.atproto.repo.createRecord" in seen["url"]
    assert "app.bsky.graph.follow" in seen["body"]
    assert "did:plc:them" in seen["body"]


async def test_an_unfollow_deletes_by_record_key(signed_in):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={})

    client = routed(handler)
    await delete_follow(client, "at://did:plc:me/app.bsky.graph.follow/xyz789")
    await client.aclose()

    assert "deleteRecord" in seen["url"]
    import json
    assert json.loads(seen["body"])["rkey"] == "xyz789"


async def test_a_write_without_a_session_refuses(monkeypatch):
    monkeypatch.setattr(auth_module.authenticator, "session", None)
    client = routed(lambda r: httpx.Response(200, json={}))
    with pytest.raises(FollowError, match="app password"):
        await create_follow(client, "did:plc:them")
    await client.aclose()


async def test_a_non_did_subject_refuses(signed_in):
    """A handle where a DID belongs would follow the wrong account or none."""
    client = routed(lambda r: httpx.Response(200, json={"uri": "at://x"}))
    with pytest.raises(FollowError, match="not a DID"):
        await create_follow(client, "someone.bsky.social")
    await client.aclose()


async def test_a_write_is_never_retried(signed_in):
    """A retried ambiguous write is how you end up following twice."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(502, json={"error": "upstream"})

    client = routed(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await create_follow(client, "did:plc:them")
    await client.aclose()
    assert calls["n"] == 1, f"the write was attempted {calls['n']} times"


# ------------------------------------------------- the guard

async def test_only_current_unhidden_followers_can_be_followed_back(db):
    await follower("did:plc:ok")
    await follower("did:plc:gone", current=False)
    await follower("did:plc:hidden", ignored=True)

    assert (await store.follow_back_target("did:plc:ok"))["is_current"] == 1
    assert (await store.follow_back_target("did:plc:gone"))["is_current"] == 0
    assert (await store.follow_back_target("did:plc:hidden"))["ignored_at"]
    assert await store.follow_back_target("did:plc:stranger") is None


async def test_the_follow_uri_is_stored_so_it_can_be_undone(db):
    await follower("did:plc:a")
    await store.record_my_follow("did:plc:a", "at://did:plc:me/app.bsky.graph.follow/k1")

    target = await store.follow_back_target("did:plc:a")
    assert target["already"] == 1
    assert target["follow_uri"].endswith("/k1")


async def test_a_follows_sweep_does_not_lose_the_uri(db):
    """`replace_my_follows` deletes and reinserts. The URI cannot be re-fetched.

    `getFollows` returns subjects, not record keys, so a dropped URI means the
    follow can never be undone from sonde again.
    """
    await follower("did:plc:a")
    await store.record_my_follow("did:plc:a", "at://did:plc:me/app.bsky.graph.follow/k1")

    await store.replace_my_follows(["did:plc:a", "did:plc:other"])

    target = await store.follow_back_target("did:plc:a")
    assert target["follow_uri"] and target["follow_uri"].endswith("/k1")


async def test_following_and_unfollowing_are_both_logged(db):
    await follower("did:plc:a")
    await store.record_my_follow("did:plc:a", "at://x/y/z")
    await store.forget_my_follow("did:plc:a")

    db_ = await store._db()
    async with db_.execute(
        "SELECT event FROM follow_events WHERE did = 'did:plc:a' ORDER BY id"
    ) as cur:
        events = [r["event"] for r in await cur.fetchall()]
    assert "followed_back" in events and "unfollowed_back" in events


async def test_a_failed_write_is_recorded_not_swallowed(db):
    await follower("did:plc:a")
    await store.record_follow_failure("did:plc:a", "boom", undo=False)

    db_ = await store._db()
    async with db_.execute(
        "SELECT event, detail FROM follow_events WHERE event = 'follow_failed'"
    ) as cur:
        row = await cur.fetchone()
    assert row and row["detail"] == "boom"


# ------------------------------------------------- the button

def render_who(row, *, enabled=True):
    from jinja2 import Environment, FileSystemLoader

    class S:
        enable_follow_write = enabled

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    return env.from_string(
        '{% from "_sort.html" import who with context %}{{ who(row) }}'
    ).render(row=row, settings=S())


def test_the_button_shows_only_for_people_you_do_not_follow():
    base = {"did": "did:plc:x", "handle": "a.bsky.social"}
    assert "+follow" in render_who({**base, "is_mutual": 0})
    assert "+follow" not in render_who({**base, "is_mutual": 1})


def test_no_button_where_the_query_never_checked():
    """Absent is_mutual must render nothing, not "you are not following"."""
    assert "+follow" not in render_who({"did": "did:plc:x", "handle": "a.b"})


def test_the_kill_switch_removes_the_button():
    row = {"did": "did:plc:x", "handle": "a.b", "is_mutual": 0}
    assert "+follow" not in render_who(row, enabled=False)


def test_every_listing_query_exposes_is_mutual():
    """A table whose query omits it silently loses the button."""
    source = (Path(__file__).resolve().parents[1] / "sonde" / "db"
              / "store.py").read_text()
    for fn in ("ranked_followers", "ranked_relationships", "group_members",
               "organisation_members", "interaction_leaderboard"):
        start = source.index(f"async def {fn}(")
        body = source[start:start + 2600]
        assert "is_mutual" in body, f"{fn} no longer exposes is_mutual"


def test_macros_are_imported_with_context():
    """Without it `settings` is undefined inside the macro and no button renders."""
    for page in TEMPLATES.glob("*.html"):
        text = page.read_text()
        if '_sort.html" import' in text:
            assert "with context" in text, f"{page.name} imports without context"


# ------------------------------------------------- the two failure domains

def _endpoint():
    """The route, over ASGI, on the caller's event loop."""
    from httpx import ASGITransport, AsyncClient

    from sonde.web.app import create_app

    return AsyncClient(transport=ASGITransport(app=create_app()),
                       base_url="http://sonde.test")


async def _events(did: str = "did:plc:a") -> list[str]:
    db_ = await store._db()
    async with db_.execute(
        "SELECT event FROM follow_events WHERE did = ?", (did,)
    ) as cur:
        return [row["event"] for row in await cur.fetchall()]


async def test_a_follow_bluesky_accepted_is_never_logged_as_a_failure(
        db, signed_in, monkeypatch):
    """The bug: a store error after createRecord discarded a real follow.

    `record_my_follow` throwing used to land in the same handler as a network
    failure, so `follow_events` recorded `follow_failed` for a follow that
    exists publicly on the operator's account — and the URI, the only handle on
    undoing it, went on the floor. The write and the bookkeeping are now
    separate stages because they are separate failure domains.
    """
    from sonde.api import follows as follows_module

    await follower("did:plc:a")

    async def accepted(client, did):
        return "at://did:plc:me/app.bsky.graph.follow/kept"

    async def boom(did, uri):
        raise RuntimeError("disk full")

    monkeypatch.setattr(follows_module, "create_follow", accepted)
    monkeypatch.setattr(store, "record_my_follow", boom)

    async with _endpoint() as c:
        r = await c.post("/followers/did:plc:a/follow", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].endswith("#not-recorded"), r.headers["location"]
    assert "follow_failed" not in await _events(), (
        "a follow Bluesky accepted was written down as a failure"
    )


async def test_the_uri_of_an_unrecorded_follow_reaches_the_log(
        db, signed_in, monkeypatch, caplog):
    """When the database cannot hold it, the log is the only surviving copy."""
    import logging

    from sonde.api import follows as follows_module

    await follower("did:plc:a")

    async def accepted(client, did):
        return "at://did:plc:me/app.bsky.graph.follow/kept"

    async def boom(did, uri):
        raise RuntimeError("disk full")

    monkeypatch.setattr(follows_module, "create_follow", accepted)
    monkeypatch.setattr(store, "record_my_follow", boom)

    with caplog.at_level(logging.ERROR):
        async with _endpoint() as c:
            await c.post("/followers/did:plc:a/follow", follow_redirects=False)

    assert any("kept" in rec.getMessage() for rec in caplog.records), (
        "the follow URI was not logged anywhere"
    )


async def test_a_network_failure_is_still_recorded_as_a_failure(
        db, signed_in, monkeypatch):
    """The other domain is unchanged: nothing was written, so say so."""
    from sonde.api import follows as follows_module

    await follower("did:plc:a")

    async def refused(client, did):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(follows_module, "create_follow", refused)

    async with _endpoint() as c:
        r = await c.post("/followers/did:plc:a/follow", follow_redirects=False)

    assert r.headers["location"].endswith("#follow-failed")
    assert "follow_failed" in await _events()
