"""The light notifications check: one call against a temporary database and a
fake Bluesky.

It runs every 15 minutes, so what matters is that it reads only what is new,
never loses what it read, never marks the operator's notifications as read,
and leaves every author it saw judged on current verification and follower
counts.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sonde.api.client import BlueskyClient
from sonde.db import store
from sonde.sync import notifications
from sonde.config import settings
from tests.fakes import actor, profiles_response, routed, verified

POST = "at://did:plc:me/app.bsky.feed.post/1"
_DAY = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
    hour=0, minute=0, second=0, microsecond=0)


def at(hour: int, minute: int = 0) -> str:
    """A time yesterday, as Bluesky writes it: recent, and in a fixed order."""
    when = _DAY + timedelta(hours=hour, minutes=minute)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def configure(monkeypatch, **values) -> None:
    """Settings is a frozen dataclass; patch through the instance dict."""
    for name, value in values.items():
        monkeypatch.setitem(settings.__dict__, name, value)


@pytest.fixture(autouse=True)
async def _db(db):
    yield


def note(author: dict, reason: str, at: str, n: int) -> dict:
    return {
        "uri": f"at://{author['did']}/app.bsky.feed.{reason}/{n}",
        "cid": f"cid{n}",
        "author": author,
        "reason": reason,
        "reasonSubject": POST,
        "record": {"subject": {"uri": POST}},
        "isRead": False,
        "indexedAt": at,
    }


class FakeBluesky:
    """Serves notification pages newest-first and records every request."""

    def __init__(self, pages: list[list[dict]], known: dict[str, dict] | None = None):
        self.pages = pages
        self.known = known if known is not None else {}
        self.requests: list[httpx.Request] = []
        self.fail_notifications = False
        self.fail_at_cursor: str | None = None
        self.fail_profiles = False
        profiles = profiles_response(self.known)

        def list_notifications(request: httpx.Request) -> httpx.Response:
            cursor = request.url.params.get("cursor")
            if self.fail_notifications or (cursor and cursor == self.fail_at_cursor):
                return httpx.Response(400, json={"error": "InvalidRequest"})
            index = int(cursor) if cursor else 0
            body: dict = {"notifications": self.pages[index] if self.pages else []}
            if index + 1 < len(self.pages):
                body["cursor"] = str(index + 1)
            return httpx.Response(200, json=body)

        def get_profiles(request: httpx.Request) -> httpx.Response:
            if self.fail_profiles:
                return httpx.Response(400, json={"error": "InvalidRequest"})
            return profiles(request)

        self.posts = {POST: "my post"}

        def get_posts(request: httpx.Request) -> httpx.Response:
            uris = request.url.params.get_list("uris")
            return httpx.Response(200, json={"posts": [
                {"uri": u, "record": {"text": self.posts[u]}}
                for u in uris if u in self.posts]})

        inner = routed({
            "app.bsky.notification.listNotifications": list_notifications,
            "app.bsky.actor.getProfiles": get_profiles,
            "app.bsky.feed.getPosts": get_posts,
        })

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return inner.handle_request(request)

        self.client = BlueskyClient("https://fake", transport=httpx.MockTransport(record),
                                    per_second=1000, max_retries=0)

    def calls(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/" + method)]


async def test_a_like_and_a_repost_are_stored():
    fan = actor("did:plc:fan")
    bsky = FakeBluesky([[note(fan, "repost", at(10, 1), 2),
                         note(fan, "like", at(10), 1)]])

    await notifications.check_notifications(bsky.client)

    kinds = sorted(r["kind"] for r in await store.interactions_for("did:plc:fan"))
    assert kinds == ["like", "repost"]


async def test_the_next_check_reads_back_only_to_where_the_last_one_stopped():
    fan = actor("did:plc:fan")
    older = [note(fan, "like", at(9), 1)]
    newer = [note(fan, "like", at(10), 2)]
    bsky = FakeBluesky([newer, older])
    await notifications.check_notifications(bsky.client)
    assert len(bsky.calls("app.bsky.notification.listNotifications")) == 2

    latest = [note(fan, "repost", at(11), 3)]
    bsky.pages = [latest + newer, older]
    bsky.requests.clear()
    await notifications.check_notifications(bsky.client)

    assert len(bsky.calls("app.bsky.notification.listNotifications")) == 1
    assert len(await store.interactions_for("did:plc:fan")) == 3


async def test_a_read_that_fails_partway_is_read_again_next_time():
    fan = actor("did:plc:fan")
    first = [note(fan, "like", at(10), 2)]
    second = [note(fan, "like", at(9), 1)]
    bsky = FakeBluesky([first, second])
    bsky.fail_at_cursor = "1"

    result = await notifications.check_notifications(bsky.client)
    assert result["status"] == "failed"

    bsky.fail_at_cursor = None
    await notifications.check_notifications(bsky.client)

    assert len(await store.interactions_for("did:plc:fan")) == 2


async def test_it_never_marks_notifications_as_read():
    fan = actor("did:plc:fan")
    bsky = FakeBluesky([[note(fan, "like", at(10), 1)]])

    await notifications.check_notifications(bsky.client)
    await notifications.check_notifications(bsky.client)

    paths = [r.url.path for r in bsky.requests]
    assert paths, "the check made no requests at all"
    assert not [p for p in paths if "updateSeen" in p]
    assert all(r.method == "GET" for r in bsky.requests)


async def test_a_verified_stranger_is_known_as_verified_but_not_as_a_follower():
    stranger = verified("did:plc:stranger", displayName="Somebody")
    bsky = FakeBluesky([[note(stranger, "like", at(10), 1)]])

    await notifications.check_notifications(bsky.client)

    who = await store.follower_detail("did:plc:stranger")
    assert who is not None
    assert who["verified_status"] == "valid"
    assert who["is_current"] is None, "a stranger must not become a follower"
    assert (await store.counts())["tracked"] == 0


async def test_a_lost_verification_is_recorded():
    """An unverified author omits `verification` entirely; that means none."""
    bsky = FakeBluesky([[note(verified("did:plc:was"), "like", at(10), 1)]])
    await notifications.check_notifications(bsky.client)

    bsky.pages = [[note(actor("did:plc:was"), "like", at(11), 2)]]
    await notifications.check_notifications(bsky.client)

    assert (await store.follower_detail("did:plc:was"))["verified_status"] == "none"


# ------------------------------------------------------------ follower counts

def counted(profile: dict, followers: int) -> dict:
    """The same person as getProfiles returns them, with a follower count."""
    return {**profile, "followersCount": followers, "followsCount": 1, "postsCount": 1}


async def test_an_author_with_no_follower_count_gets_one():
    big = actor("did:plc:big")
    bsky = FakeBluesky([[note(big, "like", at(10), 1)]],
                       known={"did:plc:big": counted(big, 10_001)})

    await notifications.check_notifications(bsky.client)

    assert (await store.follower_detail("did:plc:big"))["followers_count"] == 10_001


async def test_a_fresh_count_is_not_asked_for_again(monkeypatch):
    configure(monkeypatch, follower_count_max_age_days=7)
    big = actor("did:plc:big")
    bsky = FakeBluesky([[note(big, "like", at(10), 1)]],
                       known={"did:plc:big": counted(big, 20_000)})
    await notifications.check_notifications(bsky.client)

    bsky.pages = [[note(big, "repost", at(11), 2)]]
    bsky.requests.clear()
    await notifications.check_notifications(bsky.client)

    assert bsky.calls("app.bsky.actor.getProfiles") == []


async def test_a_count_older_than_the_setting_is_refreshed(monkeypatch):
    big = actor("did:plc:big")
    bsky = FakeBluesky([[note(big, "like", at(10), 1)]],
                       known={"did:plc:big": counted(big, 9_000)})
    await notifications.check_notifications(bsky.client)

    configure(monkeypatch, follower_count_max_age_days=0)   # everything is stale
    bsky.known["did:plc:big"] = counted(big, 12_000)
    bsky.pages = [[note(big, "repost", at(11), 2)]]
    await notifications.check_notifications(bsky.client)

    assert (await store.follower_detail("did:plc:big"))["followers_count"] == 12_000


async def test_counts_are_fetched_25_at_a_time():
    people = [actor(f"did:plc:p{i:02}") for i in range(30)]
    bsky = FakeBluesky([[note(p, "like", at(10), i) for i, p in enumerate(people)]],
                       known={p["did"]: counted(p, 100 + i) for i, p in enumerate(people)})

    await notifications.check_notifications(bsky.client)

    sizes = [len(r.url.params.get_list("actors"))
             for r in bsky.calls("app.bsky.actor.getProfiles")]
    assert sorted(sizes) == [5, 25]
    assert (await store.follower_detail("did:plc:p29"))["followers_count"] == 129


async def test_a_failed_count_lookup_keeps_the_notifications_and_is_retried():
    big = actor("did:plc:big")
    bsky = FakeBluesky([[note(big, "like", at(10), 1)]],
                       known={"did:plc:big": counted(big, 50_000)})
    bsky.fail_profiles = True
    await notifications.check_notifications(bsky.client)
    assert len(await store.interactions_for("did:plc:big")) == 1

    bsky.fail_profiles = False
    bsky.requests.clear()
    await notifications.check_notifications(bsky.client)

    # The notifications were not read again: the first check's marker held.
    assert len(bsky.calls("app.bsky.notification.listNotifications")) == 1
    assert (await store.follower_detail("did:plc:big"))["followers_count"] == 50_000


async def test_the_6_hourly_sync_still_finds_my_own_older_replies(monkeypatch):
    """The check stores inbound rows every 15 minutes. The sync's outbound walk
    must not stop at those, or every reply of mine made between syncs is lost."""
    from sonde.api import auth
    from sonde.sync import interactions

    async def token(self=None):
        return "fake"
    monkeypatch.setattr(auth.authenticator, "token", token)

    bsky = FakeBluesky([[note(actor("did:plc:fan"), "like", at(11), 1)]])
    await notifications.check_notifications(bsky.client)

    my_reply = {
        "post": {"uri": "at://did:plc:me/app.bsky.feed.post/9", "indexedAt": at(10, 30),
                 "author": {"did": "did:plc:me"},
                 "record": {"reply": {"root": {"uri": "at://t"}}}},
        "reply": {"parent": {"uri": "at://theirs", "author": {"did": "did:plc:them"}},
                  "root": {"uri": "at://t"}},
    }
    sync_client = BlueskyClient("https://fake", per_second=1000, transport=routed({
        "app.bsky.notification.listNotifications":
            lambda r: httpx.Response(200, json={"notifications": []}),
        "app.bsky.feed.getAuthorFeed":
            lambda r: httpx.Response(200, json={"feed": [my_reply]}),
    }))
    await interactions.sync(sync_client, max_pages=1)

    assert [r["kind"] for r in await store.interactions_for("did:plc:them")] == ["reply"]


@pytest.mark.parametrize("from_before_the_check", [False, True])
async def test_the_6_hourly_sync_backstops_what_the_capped_check_skipped(
        monkeypatch, from_before_the_check):
    """After an outage the check reads only its page cap. The sync must still
    walk back past the check's rows to its own last stop, or the gap is lost."""
    from sonde.api import auth
    from sonde.sync import interactions

    async def token(self=None):
        return "fake"
    monkeypatch.setattr(auth.authenticator, "token", token)

    fan = actor("did:plc:fan")
    # The sync ran once and stored an 08:00 like.
    sync_pages = [[note(fan, "like", at(8), 1)]]
    feed = {"app.bsky.feed.getAuthorFeed": lambda r: httpx.Response(200, json={"feed": []})}

    def serve(pages):
        def handler(request):
            cursor = request.url.params.get("cursor")
            index = int(cursor) if cursor else 0
            body = {"notifications": pages[index]}
            if index + 1 < len(pages):
                body["cursor"] = str(index + 1)
            return httpx.Response(200, json=body)
        return handler

    if from_before_the_check:
        # A database the old sync wrote: rows, but no sync marker in meta.
        await store.record_interactions([{
            "did": "did:plc:fan", "direction": "inbound", "kind": "like",
            "uri": sync_pages[0][0]["uri"], "subject": POST, "thread": POST,
            "occurred_at": at(8)}])
    else:
        await interactions.sync(BlueskyClient("https://fake", per_second=1000, transport=routed(
            {"app.bsky.notification.listNotifications": serve(sync_pages), **feed})),
            max_pages=5)

    # Then the check, capped at one page, sees only the newest of three.
    configure(monkeypatch, notification_check_max_pages=1)
    pages = [[note(fan, "like", at(11), 3)], [note(fan, "like", at(10), 2)],
             [note(fan, "like", at(8), 1)]]
    await notifications.check_notifications(FakeBluesky(pages).client)
    assert len(await store.interactions_for("did:plc:fan")) == 2

    await interactions.sync(BlueskyClient("https://fake", per_second=1000, transport=routed(
        {"app.bsky.notification.listNotifications": serve(pages), **feed})), max_pages=5)

    assert len(await store.interactions_for("did:plc:fan")) == 3


async def test_the_page_cap_bounds_a_long_backlog(monkeypatch):
    configure(monkeypatch, notification_check_max_pages=2)
    fan = actor("did:plc:fan")
    pages = [[note(fan, "like", at(20 - p), p)] for p in range(6)]
    bsky = FakeBluesky(pages)

    await notifications.check_notifications(bsky.client)

    assert len(bsky.calls("app.bsky.notification.listNotifications")) == 2


# ------------------------------------------------- review fixes (sonde#11)

def _signed_in(monkeypatch) -> None:
    from sonde.api import auth

    async def token(self=None):
        return "fake"
    monkeypatch.setattr(auth.authenticator, "token", token)


def _sync_client(pages: list[list[dict]]) -> BlueskyClient:
    def handler(request):
        cursor = request.url.params.get("cursor")
        index = int(cursor) if cursor else 0
        body = {"notifications": pages[index] if pages else []}
        if index + 1 < len(pages):
            body["cursor"] = str(index + 1)
        return httpx.Response(200, json=body)
    return BlueskyClient("https://fake", per_second=1000, transport=routed({
        "app.bsky.notification.listNotifications": handler,
        "app.bsky.feed.getAuthorFeed": lambda r: httpx.Response(200, json={"feed": []}),
    }))


async def test_a_stranger_stored_only_by_the_6_hourly_sync_is_known(monkeypatch):
    """The sync stores likes the check never saw (its cap, an outage). Their
    authors need an actors row too, or notability can never be judged."""
    from sonde.sync import interactions
    _signed_in(monkeypatch)

    await interactions.sync(_sync_client(
        [[note(verified("did:plc:v"), "like", at(10), 1)]]), max_pages=5)

    since = (_DAY - timedelta(hours=1)).isoformat()
    assert [r["did"] for r in await store.notable_interactions_since(since)] == ["did:plc:v"]
    assert "did:plc:v" not in await store.known_dids()


async def test_the_first_sync_after_the_check_on_a_fresh_database_walks_all_history(
        monkeypatch):
    """A fresh install: the check runs (every 15 minutes) before the first
    6-hourly sync. The sync must still read inbound history in full rather
    than stop at the check's newest row."""
    from sonde.sync import interactions
    _signed_in(monkeypatch)

    fan = actor("did:plc:fan")
    pages = [[note(fan, "like", at(11), 3)], [note(fan, "like", at(10), 2)],
             [note(fan, "like", at(8), 1)]]
    configure(monkeypatch, notification_check_max_pages=1)
    await notifications.check_notifications(FakeBluesky(pages).client)
    assert len(await store.interactions_for("did:plc:fan")) == 1

    await interactions.sync(_sync_client(pages), max_pages=5)

    assert len(await store.interactions_for("did:plc:fan")) == 3


async def test_an_old_database_the_check_never_saw_still_syncs_incrementally(
        monkeypatch):
    """Before the check ever runs, an old database has no sync marker: the
    sync resumes from the table's newest inbound row, as it always did."""
    from sonde.sync import interactions
    _signed_in(monkeypatch)

    fan = actor("did:plc:fan")
    await store.record_interactions([{
        "did": "did:plc:fan", "direction": "inbound", "kind": "like",
        "uri": "at://old", "subject": POST, "thread": POST, "occurred_at": at(9)}])
    pages = [[note(fan, "like", at(10), 2)], [note(fan, "like", at(8), 1)]]

    await interactions.sync(_sync_client(pages), max_pages=5)

    uris = {r["uri"] for r in await store.interactions_for("did:plc:fan")}
    assert pages[1][0]["uri"] not in uris
    assert pages[0][0]["uri"] in uris


async def test_an_author_bluesky_cannot_serve_is_not_asked_about_every_check():
    gone = actor("did:plc:gone")
    bsky = FakeBluesky([[note(gone, "like", at(10), 1)]], known={})
    await notifications.check_notifications(bsky.client)
    assert len(bsky.calls("app.bsky.actor.getProfiles")) == 1

    bsky.pages = [[note(gone, "repost", at(11), 2)]]
    bsky.requests.clear()
    await notifications.check_notifications(bsky.client)

    assert bsky.calls("app.bsky.actor.getProfiles") == []


async def test_a_notable_likes_post_text_is_fetched_even_with_alerts_off():
    """The digest names each post by its first line; with the alert email
    switched off, nothing else would ever fetch it."""
    other = "at://did:plc:me/app.bsky.feed.post/2"
    bsky = FakeBluesky([[note(verified("did:plc:v"), "like", at(10), 1),
                         {**note(actor("did:plc:plain"), "like", at(9), 2),
                          "reasonSubject": other}]])

    await notifications.check_notifications(bsky.client)

    posts = bsky.calls("app.bsky.feed.getPosts")
    assert [r.url.params.get_list("uris") for r in posts] == [[POST]]
    assert await store.post_previews([POST, other]) == {POST: "my post"}

    bsky.requests.clear()
    await notifications.check_notifications(bsky.client)
    assert bsky.calls("app.bsky.feed.getPosts") == []


async def test_a_failed_check_is_recorded_as_a_failed_run():
    bsky = FakeBluesky([[note(actor("did:plc:fan"), "like", at(10), 1)]])
    bsky.fail_notifications = True

    await notifications.check_notifications(bsky.client)

    runs = await store.recent_runs(1)
    assert runs[0]["kind"] == "notifications"
    assert runs[0]["status"] == "failed"
