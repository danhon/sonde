"""M14 — the relationship score.

Deliberately separate from influence. Influence asks whether someone matters;
this asks whether we know each other.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sonde.api import auth as auth_module
from sonde.api.client import BlueskyClient
from sonde.db import store
from sonde.relationships import KIND_WEIGHT, summarise
from sonde.sync import interactions as ix
from tests.fakes import actor, routed

NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def event(kind, direction="inbound", days_ago=1, thread=None, did="did:plc:a"):
    return {"did": did, "direction": direction, "kind": kind, "thread": thread,
            "occurred_at": (NOW - timedelta(days=days_ago)).isoformat()}


# --------------------------------------------------------- weighting

def test_a_conversation_outweighs_a_pile_of_likes():
    """A bot that likes everything must not beat someone I talk to."""
    likes = summarise([event("like", days_ago=1) for _ in range(40)], now=NOW)
    talk = summarise(
        [event("reply", "inbound", 2), event("reply", "outbound", 2)],
        conversations=2, now=NOW)
    assert talk.raw > likes.raw


def test_interaction_kinds_are_ranked_by_what_they_cost_to_give():
    assert KIND_WEIGHT["reply"] > KIND_WEIGHT["quote"] > KIND_WEIGHT["repost"] \
        > KIND_WEIGHT["mention"] > KIND_WEIGHT["like"]


def test_recent_interactions_outweigh_old_ones():
    fresh = summarise([event("reply", days_ago=1)], now=NOW)
    stale = summarise([event("reply", days_ago=800)], now=NOW)
    assert fresh.raw > stale.raw * 5


def test_reciprocity_separates_a_relationship_from_an_audience():
    one_way = summarise([event("like") for _ in range(20)], now=NOW)
    mutual = summarise(
        [event("reply", "inbound", 3), event("reply", "outbound", 4)], now=NOW)
    assert mutual.reciprocity == 1.0
    assert one_way.reciprocity == 0.0
    assert mutual.score(10) > one_way.score(10)


def test_one_burst_is_not_a_relationship():
    """Interacting across many separate days beats a single argument."""
    burst = summarise([event("reply", days_ago=5) for _ in range(6)], now=NOW)
    spread = summarise([event("reply", days_ago=d) for d in range(1, 7)], now=NOW)
    assert spread.days_active > burst.days_active
    assert spread.score(10) > burst.score(10)


def test_a_purely_one_way_stream_still_registers():
    """Low, but not zero — they are paying attention even if I am not."""
    assert summarise([event("like") for _ in range(10)], now=NOW).score(10) > 0


def test_no_interactions_is_no_relationship():
    assert summarise([], now=NOW).score() == 0.0


def test_a_malformed_timestamp_does_not_crash_the_fold():
    rel = summarise([{"did": "did:plc:a", "direction": "inbound",
                      "kind": "like", "occurred_at": "not-a-date"}], now=NOW)
    assert rel.score() == 0.0


# ----------------------------------------------------------- storage

@pytest.fixture
async def db(tmp_path):
    store.set_db_path(str(tmp_path / "rel14.db"))
    await store.connect()
    yield store
    await store.close()
    store.set_db_path(None)


async def follower(did: str) -> None:
    await store.upsert_actor(actor(did))
    await store.mark_seen(did, 0)


async def test_interactions_are_append_only(db):
    await follower("did:plc:a")
    rows = [event("like", days_ago=2)]
    await store.record_interactions(rows)
    await store.record_interactions(rows)   # same batch again

    assert len(await store.interactions_for("did:plc:a")) == 1, "deduplicated by uri"


async def test_a_thread_we_both_posted_in_counts_as_a_conversation(db):
    await follower("did:plc:a")
    await store.record_interactions([
        {**event("reply", "inbound", 2, thread="at://t1"), "uri": "at://r1"},
        {**event("reply", "outbound", 2, thread="at://t1"), "uri": "at://r2"},
    ])
    assert (await store.conversation_counts()).get("did:plc:a") == 1


async def test_a_one_sided_thread_is_not_a_conversation(db):
    await follower("did:plc:a")
    await store.record_interactions([
        {**event("reply", "inbound", 2, thread="at://t1"), "uri": "at://r1"},
        {**event("reply", "inbound", 2, thread="at://t1"), "uri": "at://r2"},
    ])
    assert await store.conversation_counts() == {}


async def test_scoring_writes_a_decomposition(db):
    await follower("did:plc:a")
    await store.record_interactions([
        {**event("reply", "inbound", 1, thread="at://t"), "uri": "at://x"},
        {**event("reply", "outbound", 1, thread="at://t"), "uri": "at://y"},
    ])
    result = await store.score_relationships()

    assert result["scored"] == 1
    detail = await store.follower_detail("did:plc:a")
    assert detail["relationship_score"] > 0
    assert detail["relationship"]["inbound"] == 1
    assert detail["relationship"]["outbound"] == 1


async def test_the_ranking_excludes_people_with_no_signal_at_all(db):
    """Silence with nothing else known is still exclusion.

    M17 qualified this rule rather than removing it: no interactions *and* no
    attention scarcity means no relationship. See the next test for the case
    where silence is not absence.
    """
    await follower("did:plc:quiet")
    await follower("did:plc:chatty")
    await store.record_interactions([
        {**event("reply", "inbound", 1, did="did:plc:chatty"), "uri": "at://x"}])
    await store.score_relationships()

    assert [r["did"] for r in await store.ranked_relationships()] == ["did:plc:chatty"]


# ------------------------------------------- M17 attention scarcity

async def scarce(did: str, followers: int, follows: int) -> None:
    """A follower whose attention is measurably scarce."""
    await store.upsert_actor(actor(did, followersCount=followers,
                                   followsCount=follows))
    await store.mark_seen(did, 0)
    await store.apply_detailed_profile(
        did, {"followersCount": followers, "followsCount": follows})


async def test_a_silent_but_scarce_follower_still_ranks(db):
    """The gap M17 exists to close.

    Someone who follows 1,037 accounts including mine, with 150,735 followers,
    has made a deliberate choice. Under interactions alone they scored zero.
    """
    await scarce("did:plc:whittaker", 150_735, 1_037)
    result = await store.score_relationships()

    assert result["with_attention"] == 1
    detail = await store.follower_detail("did:plc:whittaker")
    assert detail["relationship_score"] > 0
    assert detail["relationship"]["attention"] > 0
    assert detail["relationship"]["inbound"] == 0
    assert "1,037" in detail["relationship"]["attention_note"]


async def test_attention_cannot_outrank_a_real_conversation(db):
    """The cap, stated as an ordering rather than a constant."""
    await scarce("did:plc:scarce", 500_000, 60)
    await follower("did:plc:friend")
    await store.record_interactions([
        {**event("reply", "inbound", d, thread=f"at://t{d}"), "uri": f"at://a{d}",
         "did": "did:plc:friend"} for d in range(1, 12)
    ] + [
        {**event("reply", "outbound", d, thread=f"at://t{d}"), "uri": f"at://b{d}",
         "did": "did:plc:friend"} for d in range(1, 12)
    ])
    await store.score_relationships()

    ranked = [r["did"] for r in await store.ranked_relationships()]
    assert ranked.index("did:plc:friend") < ranked.index("did:plc:scarce")


async def test_attention_adds_to_interaction_rather_than_replacing_it(db):
    await scarce("did:plc:both", 150_735, 1_037)
    await store.record_interactions([
        {**event("reply", "inbound", 1, thread="at://t"), "uri": "at://x",
         "did": "did:plc:both"},
        {**event("reply", "outbound", 1, thread="at://t"), "uri": "at://y",
         "did": "did:plc:both"},
    ])
    await store.score_relationships()

    rel = (await store.follower_detail("did:plc:both"))["relationship"]
    assert rel["interaction_score"] > 0
    assert rel["attention"] > 0
    assert rel["score"] == pytest.approx(
        min(rel["interaction_score"] + rel["attention"], 100.0), abs=0.11)


async def test_unhydrated_followers_are_not_scored_as_zero_attention(db):
    """No counts means unknown, which must not look like a measured zero."""
    await follower("did:plc:unknown")
    result = await store.score_relationships()
    assert result["scored"] == 0


async def test_attention_is_sortable(db):
    await scarce("did:plc:more", 500_000, 200)
    await scarce("did:plc:less", 20_000, 900)
    await store.score_relationships()

    ranked = await store.ranked_relationships(order="attention", direction="desc")
    assert [r["did"] for r in ranked] == ["did:plc:more", "did:plc:less"]


async def test_incremental_sync_resumes_from_the_newest_stored(db):
    await follower("did:plc:a")
    await store.record_interactions([{**event("like", days_ago=3), "uri": "at://x"}])
    latest = await store.latest_interaction_at()
    assert latest and latest.startswith("2026-07-24")


# ------------------------------------------------------------ ingest

@pytest.fixture
def authed(monkeypatch):
    async def token(self=None):
        return "fake"
    monkeypatch.setattr(auth_module.authenticator, "token", token)


def notif_client(notifications, feed=None) -> BlueskyClient:
    def notes(request):
        return httpx.Response(200, json={"notifications": notifications})

    def author_feed(request):
        return httpx.Response(200, json={"feed": feed or []})

    return BlueskyClient(
        "https://fake",
        transport=routed({
            "app.bsky.notification.listNotifications": notes,
            "app.bsky.feed.getAuthorFeed": author_feed,
        }),
        per_second=1000,
    )


async def test_notifications_become_inbound_interactions(db, authed):
    await follower("did:plc:friend")
    client = notif_client([
        {"uri": "at://n1", "reason": "reply", "reasonSubject": "at://mine",
         "indexedAt": "2026-07-26T10:00:00Z",
         "author": {"did": "did:plc:friend"},
         "record": {"reply": {"root": {"uri": "at://t"}}}},
        {"uri": "at://n2", "reason": "starterpack-joined",
         "indexedAt": "2026-07-26T09:00:00Z",
         "author": {"did": "did:plc:friend"}, "record": {}},
    ])
    result = await ix.sync(client, max_pages=1, full=True)
    await client.aclose()

    assert result["inbound"] == 1, "only real interactions count"
    rows = await store.interactions_for("did:plc:friend")
    assert rows[0]["kind"] == "reply"
    assert rows[0]["direction"] == "inbound"


async def test_without_a_session_it_skips_rather_than_failing(db, monkeypatch):
    async def no_token(self=None):
        return None
    monkeypatch.setattr(auth_module.authenticator, "token", no_token)

    client = notif_client([])
    result = await ix.sync(client, max_pages=1)
    await client.aclose()

    assert result["skipped"] == "not authenticated"
    assert result["api_calls"] == 0


async def test_my_replies_become_outbound_interactions(db, authed):
    await follower("did:plc:them")
    client = notif_client([], feed=[{
        "post": {"uri": "at://mine", "indexedAt": "2026-07-26T10:00:00Z",
                 "author": {"did": "did:plc:me"},
                 "record": {"reply": {"root": {"uri": "at://t"}}}},
        "reply": {"parent": {"uri": "at://theirs",
                             "author": {"did": "did:plc:them"}},
                  "root": {"uri": "at://t"}},
    }])
    result = await ix.sync(client, max_pages=1, full=True)
    await client.aclose()

    assert result["outbound"] == 1
    rows = await store.interactions_for("did:plc:them")
    assert rows[0]["direction"] == "outbound"
    assert rows[0]["kind"] == "reply"
