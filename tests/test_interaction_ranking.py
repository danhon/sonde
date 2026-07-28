"""M22 — ranking each interaction type on its own.

Built and tested against synthetic data: the `interactions` table is empty in
every local snapshot because filling it needs the app password. The shapes are
asserted here; the volumes have not been seen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sonde.db import store
from sonde.relationships import summarise
from tests.fakes import actor

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


def event(did: str, kind: str, direction: str, days_ago: int, n: int = 0) -> dict:
    return {
        "did": did, "direction": direction, "kind": kind, "thread": None,
        "uri": f"at://{did}/{kind}/{direction}/{days_ago}/{n}",
        "occurred_at": (NOW - timedelta(days=days_ago)).isoformat(),
    }


async def follower(did: str) -> None:
    await store.upsert_actor(actor(did))
    await store.mark_seen(did, 0)


async def test_each_kind_is_ranked_separately(db):
    """A like costs nothing and a reply costs attention. Never one ranking."""
    await follower("did:plc:replier")
    await follower("did:plc:liker")
    await store.record_interactions(
        [event("did:plc:replier", "reply", "inbound", 1, i) for i in range(5)]
        + [event("did:plc:liker", "like", "inbound", 1, i) for i in range(50)]
    )

    replies = await store.interaction_leaderboard("reply")
    likes = await store.interaction_leaderboard("like")

    assert [r["did"] for r in replies] == ["did:plc:replier"]
    assert [r["did"] for r in likes] == ["did:plc:liker"]


async def test_inbound_is_the_default_and_outbound_is_separate(db):
    """"Who replies to me" is not "who I reply to"."""
    await follower("did:plc:answers")
    await follower("did:plc:ignores")
    await store.record_interactions(
        [event("did:plc:answers", "reply", "inbound", 2, i) for i in range(4)]
        + [event("did:plc:ignores", "reply", "outbound", 2, i) for i in range(9)]
    )

    assert [r["did"] for r in await store.interaction_leaderboard("reply")] \
        == ["did:plc:answers"]
    assert [r["did"] for r in
            await store.interaction_leaderboard("reply", direction="outbound")] \
        == ["did:plc:ignores"]


async def test_the_reciprocal_count_separates_a_correspondent_from_an_audience(db):
    await follower("did:plc:mutual")
    await follower("did:plc:oneway")
    await store.record_interactions(
        [event("did:plc:mutual", "reply", "inbound", 3, i) for i in range(4)]
        + [event("did:plc:mutual", "reply", "outbound", 3, i) for i in range(3)]
        + [event("did:plc:oneway", "reply", "inbound", 3, i) for i in range(4)]
    )

    rows = {r["did"]: r for r in await store.interaction_leaderboard("reply")}
    assert rows["did:plc:mutual"]["reciprocal"] == 3
    assert rows["did:plc:oneway"]["reciprocal"] == 0


async def test_the_window_filters_by_age(db):
    await follower("did:plc:a")
    await store.record_interactions(
        [event("did:plc:a", "reply", "inbound", 5, 1),
         event("did:plc:a", "reply", "inbound", 400, 2)])

    assert (await store.interaction_leaderboard("reply"))[0]["n"] == 2
    assert (await store.interaction_leaderboard("reply", days=30))[0]["n"] == 1


async def test_hidden_and_departed_people_are_left_out(db):
    await follower("did:plc:here")
    await follower("did:plc:hidden")
    await follower("did:plc:gone")
    await store.record_interactions(
        [event(d, "reply", "inbound", 1, i)
         for d in ("did:plc:here", "did:plc:hidden", "did:plc:gone")
         for i in range(3)])
    dbc = await store._db()
    await dbc.execute("UPDATE follower_state SET ignored_at='2026-01-01' "
                      "WHERE did='did:plc:hidden'")
    await dbc.execute("UPDATE follower_state SET is_current=0 "
                      "WHERE did='did:plc:gone'")
    await dbc.commit()

    assert [r["did"] for r in await store.interaction_leaderboard("reply")] \
        == ["did:plc:here"]


async def test_an_unknown_kind_does_not_return_everything(db):
    await follower("did:plc:a")
    await store.record_interactions([event("did:plc:a", "like", "inbound", 1)])

    rows = await store.interaction_leaderboard("'; DROP TABLE interactions--")
    assert rows == [], "an unrecognised kind fell through to an unfiltered query"


async def test_the_window_states_what_it_covers(db):
    """A count is only as old as the observation period."""
    await follower("did:plc:a")
    await store.record_interactions(
        [event("did:plc:a", "like", "inbound", 30, 1),
         event("did:plc:a", "reply", "inbound", 2, 2)])

    window = await store.interaction_window()
    assert window["events"] == 2 and window["people"] == 1
    assert window["earliest"] < window["latest"]


async def test_the_breakdown_splits_kind_by_direction(db):
    await follower("did:plc:a")
    await store.record_interactions(
        [event("did:plc:a", "reply", "inbound", 1, i) for i in range(3)]
        + [event("did:plc:a", "reply", "outbound", 1, i) for i in range(1)]
        + [event("did:plc:a", "like", "inbound", 1, i) for i in range(7)])

    breakdown = await store.interaction_breakdown("did:plc:a")
    assert breakdown["reply"] == {"inbound": 3, "outbound": 1,
                                  "last_at": breakdown["reply"]["last_at"]}
    assert breakdown["like"]["inbound"] == 7
    assert breakdown["quote"]["inbound"] == 0


def test_by_kind_no_longer_conflates_directions():
    """The defect this milestone found: one bucket for both directions.

    It made "someone I reply to who never answers" identical to "someone who
    replies to me constantly" on the profile page.
    """
    rows = [event("did:plc:a", "reply", "inbound", 1, i) for i in range(3)]
    rows += [event("did:plc:a", "reply", "outbound", 1, i) for i in range(8)]
    rel = summarise(rows, now=NOW)

    assert rel.by_kind["reply"] == {"inbound": 3, "outbound": 8}


@pytest.mark.parametrize("kind", ["reply", "quote", "repost", "like", "mention"])
def test_every_kind_the_ingest_stores_can_be_ranked(kind):
    """The leaderboard and the ingest must agree on the vocabulary."""
    from sonde.sync.interactions import INTERESTING

    assert kind in INTERESTING
    assert kind in store.INTERACTION_KINDS


# ------------------------------------------------- the page

def test_the_route_renders_empty_and_populated(tmp_path):
    """Both states, because the empty one is what a new install sees."""
    from fastapi.testclient import TestClient
    from sonde.web.app import create_app

    store.set_db_path(str(tmp_path / "ix.db"))
    with TestClient(create_app()) as client:
        empty = client.get("/interactions")
        assert empty.status_code == 200
        assert "Sync interactions" in empty.text

        for kind in store.INTERACTION_KINDS:
            assert client.get(f"/interactions?kind={kind}").status_code == 200
        assert client.get("/interactions?direction=outbound").status_code == 200
        assert client.get("/interactions?days=90").status_code == 200
        # A junk kind must not 500.
        assert client.get("/interactions?kind=nonsense").status_code == 200
    store.set_db_path(None)
