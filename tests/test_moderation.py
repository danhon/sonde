"""M9b/M9c/M10 — hiding, moderation lists, posts, follow dates.

The governing rule for hiding: a human's decision outranks the automation in
both directions, and nothing is ever deleted.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from sonde.api.client import BlueskyClient
from sonde.api.tid import decode, from_at_uri
from sonde.db import store
from sonde.sync import moderation, posts
from sonde.web.app import create_app
from tests.fakes import actor, routed, verified


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "mod.db"))
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


def list_client(lists: list[dict], members: dict[str, list[str]]) -> BlueskyClient:
    def get_lists(request):
        return httpx.Response(200, json={"lists": lists})

    def get_list(request):
        uri = request.url.params.get("list")
        return httpx.Response(200, json={
            "items": [{"subject": {"did": d}} for d in members.get(uri, [])]
        })

    return BlueskyClient(
        "https://fake",
        transport=routed({
            "app.bsky.graph.getLists": get_lists,
            "app.bsky.graph.getList": get_list,
        }),
        per_second=1000,
    )


SPAM = {"uri": "at://did:plc:sw/app.bsky.graph.list/spam", "name": "Spam",
        "purpose": "app.bsky.graph.defs#modlist"}
MAGA = {"uri": "at://did:plc:sw/app.bsky.graph.list/maga", "name": "MAGA",
        "purpose": "app.bsky.graph.defs#modlist"}


# ------------------------------------------------------------- hiding

async def test_hiding_never_deletes():
    await add_follower(actor("did:plc:h1"), 0)
    await store.set_ignored("did:plc:h1", True)

    assert await store.follower_detail("did:plc:h1") is not None
    assert "did:plc:h1" in await store.known_dids(), "still swept"
    counts = await store.counts()
    assert counts["tracked"] == 1, "totals must not silently shrink"
    assert counts["ignored"] == 1


async def test_hidden_accounts_leave_listings_and_exports():
    await add_follower(actor("did:plc:shown"), 0)
    await add_follower(actor("did:plc:hidden"), 1)
    await store.set_ignored("did:plc:hidden", True)

    assert [r["did"] for r in await store.ranked_followers()] == ["did:plc:shown"]
    assert [r["did"] for r in await store.export_rows()] == ["did:plc:shown"]


async def test_restoring_puts_them_back():
    await add_follower(actor("did:plc:r1"), 0)
    await store.set_ignored("did:plc:r1", True)
    await store.set_ignored("did:plc:r1", False)

    assert len(await store.ranked_followers()) == 1
    assert await store.ignored_count() == 0


# -------------------------------------------------- moderation lists

async def test_followers_on_a_list_are_hidden():
    await add_follower(actor("did:plc:spammer"), 0)
    await add_follower(actor("did:plc:fine"), 1)

    c = list_client([SPAM], {SPAM["uri"]: ["did:plc:spammer", "did:plc:stranger"]})
    result = await moderation.sync_lists(c, curators=["skywatch.blue"])
    await c.aclose()

    assert result["hidden"] == 1
    assert [r["did"] for r in await store.ranked_followers()] == ["did:plc:fine"]


async def test_a_restored_follower_is_not_re_hidden():
    """The important one. Un-hiding a false positive has to stick."""
    await add_follower(actor("did:plc:wrongly-listed"), 0)
    c = list_client([SPAM], {SPAM["uri"]: ["did:plc:wrongly-listed"]})
    await moderation.sync_lists(c, curators=["skywatch.blue"])
    await c.aclose()
    assert await store.ignored_count() == 1

    # Human overrules the list.
    await store.set_ignored("did:plc:wrongly-listed", False, lock=True)

    c = list_client([SPAM], {SPAM["uri"]: ["did:plc:wrongly-listed"]})
    result = await moderation.sync_lists(c, curators=["skywatch.blue"])
    await c.aclose()

    assert result["hidden"] == 0
    assert result["locked_skipped"] >= 1
    assert await store.ignored_count() == 0, "a later refresh must not overrule you"


async def test_a_manual_hide_survives_a_moderation_refresh():
    await add_follower(actor("did:plc:manual"), 0)
    await store.set_ignored("did:plc:manual", True, reason="manual", lock=True)

    c = list_client([SPAM], {SPAM["uri"]: []})
    await moderation.sync_lists(c, curators=["skywatch.blue"])
    await c.aclose()

    assert await store.ignored_count() == 1, "your hide is not the list's to undo"


async def test_dropping_off_every_list_restores_an_auto_hide():
    """A curator's correction should propagate."""
    await add_follower(actor("did:plc:reformed"), 0)
    c = list_client([SPAM], {SPAM["uri"]: ["did:plc:reformed"]})
    await moderation.sync_lists(c, curators=["skywatch.blue"])
    await c.aclose()
    assert await store.ignored_count() == 1

    c = list_client([SPAM], {SPAM["uri"]: []})
    result = await moderation.sync_lists(c, curators=["skywatch.blue"])
    await c.aclose()

    assert result["restored"] == 1
    assert await store.ignored_count() == 0


async def test_a_disabled_list_stops_hiding():
    """The lists are not homogeneous — abuse and politics sit side by side —
    so each has to be refusable on its own."""
    await add_follower(actor("did:plc:political"), 0)
    c = list_client([MAGA], {MAGA["uri"]: ["did:plc:political"]})
    await moderation.sync_lists(c, curators=["skywatch.blue"])
    await c.aclose()
    assert await store.ignored_count() == 1

    await store.set_list_enabled(MAGA["uri"], False)
    result = await store.apply_moderation_hides()

    assert result["restored"] == 1
    assert await store.ignored_count() == 0


async def test_the_lists_that_flagged_someone_are_recorded():
    await add_follower(actor("did:plc:flagged"), 0)
    c = list_client([SPAM, MAGA],
                    {SPAM["uri"]: ["did:plc:flagged"], MAGA["uri"]: ["did:plc:flagged"]})
    await moderation.sync_lists(c, curators=["skywatch.blue"])
    await c.aclose()

    names = {l["name"] for l in await store.lists_matching("did:plc:flagged")}
    assert names == {"Spam", "MAGA"}, "why is this person hidden must have an answer"


# ------------------------------------------------------------- posts

def posts_client(feeds: dict[str, list[dict]]) -> BlueskyClient:
    def handler(request):
        who = request.url.params.get("actor")
        if who == "did:plc:broken":
            return httpx.Response(400, json={"error": "BlockedActor"})
        return httpx.Response(200, json={"feed": feeds.get(who, [])})

    return BlueskyClient(
        "https://fake", transport=routed({"app.bsky.feed.getAuthorFeed": handler}),
        per_second=1000,
    )


def feed_item(uri: str, text: str, when: str, repost: bool = False) -> dict:
    item = {"post": {"uri": uri, "record": {"text": text}, "indexedAt": when,
                     "likeCount": 5, "repostCount": 1, "replyCount": 0}}
    if repost:
        item["reason"] = {"$type": "app.bsky.feed.defs#reasonRepost"}
    return item


async def test_three_recent_posts_are_stored():
    await add_follower(actor("did:plc:poster"), 0)
    c = posts_client({"did:plc:poster": [
        feed_item("at://a/1", "newest", "2026-07-27T10:00:00Z"),
        feed_item("at://a/2", "middle", "2026-07-26T10:00:00Z"),
        feed_item("at://a/3", "oldest", "2026-07-25T10:00:00Z"),
        feed_item("at://a/4", "too old", "2026-07-24T10:00:00Z"),
    ]})
    result = await posts.fetch_posts(c)
    await c.aclose()

    stored = await store.posts_for("did:plc:poster")
    assert result["fetched"] == 1
    assert len(stored) == 3, "three kept, not an archive"
    assert stored[0]["text"] == "newest"


async def test_posts_retire_the_lifetime_average_liveness_proxy():
    await add_follower(actor("did:plc:live"), 0)
    c = posts_client({"did:plc:live": [
        feed_item("at://a/1", "hello", "2026-07-27T10:00:00Z")]})
    await posts.fetch_posts(c)
    await c.aclose()

    detail = await store.follower_detail("did:plc:live")
    assert detail["last_post_at"] == "2026-07-27T10:00:00Z"


async def test_a_repost_does_not_count_as_their_own_latest_post():
    await add_follower(actor("did:plc:reposter"), 0)
    c = posts_client({"did:plc:reposter": [
        feed_item("at://a/1", "someone else's", "2026-07-27T10:00:00Z", repost=True),
        feed_item("at://a/2", "mine", "2026-07-20T10:00:00Z"),
    ]})
    await posts.fetch_posts(c)
    await c.aclose()

    detail = await store.follower_detail("did:plc:reposter")
    assert detail["last_post_at"] == "2026-07-20T10:00:00Z"
    assert [p["is_repost"] for p in await store.posts_for("did:plc:reposter")] == [1, 0]


async def test_an_unreadable_account_does_not_stop_the_run():
    await add_follower(actor("did:plc:broken"), 0)
    await add_follower(actor("did:plc:ok"), 1)
    c = posts_client({"did:plc:ok": [feed_item("at://a/1", "hi", "2026-07-27T10:00:00Z")]})
    result = await posts.fetch_posts(c)
    await c.aclose()

    assert result["status"] == "ok"
    assert result["fetched"] == 1
    assert result["failed"] == 1


async def test_hidden_followers_are_not_fetched():
    await add_follower(actor("did:plc:hidden-poster"), 0)
    await store.set_ignored("did:plc:hidden-poster", True)
    assert await store.post_targets() == []


async def test_priority_order_puts_verified_first():
    await add_follower(actor("did:plc:plain"), 0)
    await add_follower(verified("did:plc:vip"), 1)
    assert (await store.post_targets())[0] == "did:plc:vip"


# ------------------------------------------------------- follow dates

def test_a_tid_decodes_to_its_write_time():
    """Verified against three live records; agrees with createdAt to 0.15s."""
    assert decode("3mrasxegoj72j").isoformat().startswith("2026-07-22T17:11:24")
    assert from_at_uri(
        "at://did:plc:x/app.bsky.graph.follow/3mqngzdx45m2w"
    ).isoformat().startswith("2026-07-15T00:17:10")


@pytest.mark.parametrize("bad", ["", "short", "x" * 13, None, "!!!!!!!!!!!!!"])
def test_malformed_tids_are_rejected_not_guessed(bad):
    assert decode(bad) is None


async def test_a_follow_uri_records_an_exact_follow_date():
    await add_follower(actor("did:plc:dated"), 0)
    ok = await store.record_follow_date(
        "did:plc:dated", "at://did:plc:dated/app.bsky.graph.follow/3mrasxegoj72j"
    )
    await store.commit()

    assert ok
    detail = await store.follower_detail("did:plc:dated")
    assert detail["followed_at"].startswith("2026-07-22T17:11")


async def test_an_unparseable_follow_uri_is_ignored():
    await add_follower(actor("did:plc:undated"), 0)
    assert await store.record_follow_date("did:plc:undated", "at://nonsense") is False


# -------------------------------------------------------------- routes

async def test_hidden_page_renders(client):
    await add_follower(actor("did:plc:page"), 0)
    await store.set_ignored("did:plc:page", True)
    r = client.get("/ignored")
    assert r.status_code == 200
    assert "page.bsky.social" in r.text


async def test_hide_and_restore_from_the_ui(client):
    await add_follower(actor("did:plc:ui"), 0)

    client.post("/followers/did:plc:ui/ignore", follow_redirects=False)
    assert await store.ignored_count() == 1

    client.post("/followers/did:plc:ui/ignore?restore=true", follow_redirects=False)
    assert await store.ignored_count() == 0
