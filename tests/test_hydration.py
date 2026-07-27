"""Tier 1 hydration.

The rule under test: getProfiles returns HTTP 200 with unresolvable actors
silently omitted, so results must be keyed by DID. Index-zipping would assign
follower counts to the wrong people with no error anywhere — the most dangerous
bug available in this codebase.
"""

import httpx
import pytest

from sonde.api.client import BlueskyClient
from sonde.db import store
from sonde.sync import profiles
from tests.fakes import actor, profiles_response, routed, verified


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "hydrate.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


async def add_followers(n: int, **fields) -> list[str]:
    dids = []
    for i in range(n):
        did = f"did:plc:h{i}"
        await store.upsert_actor(actor(did))
        await store.mark_seen(did, i)
        dids.append(did)
    return dids


def detailed(did: str, followers: int, follows: int = 100) -> dict:
    return {
        **actor(did),
        "followersCount": followers,
        "followsCount": follows,
        "postsCount": 500,
    }


def client_for(known: dict[str, dict]) -> BlueskyClient:
    return BlueskyClient(
        "https://fake",
        transport=routed({"app.bsky.actor.getProfiles": profiles_response(known)}),
        per_second=1000,
    )


async def test_hydration_assigns_counts_to_the_right_people():
    """The core guarantee. Two actors in the batch are unresolvable."""
    dids = await add_followers(6)
    resolvable = {
        d: detailed(d, followers=(i + 1) * 1000)
        for i, d in enumerate(dids)
        if i not in (1, 4)
    }

    client = client_for(resolvable)
    result = await profiles.hydrate(client)
    await client.aclose()

    assert result["hydrated"] == 4
    assert result["unservable"] == 2

    db = await store._db()
    for i, did in enumerate(dids):
        async with db.execute(
            "SELECT followers_count, unservable_since FROM actors WHERE did = ?", (did,)
        ) as cur:
            row = await cur.fetchone()
        if i in (1, 4):
            assert row["followers_count"] is None
            assert row["unservable_since"] is not None, "omission is the 'gone' marker"
        else:
            assert row["followers_count"] == (i + 1) * 1000, "wrong count on the wrong actor"


async def test_unservable_actors_are_not_retried_next_cycle():
    """Re-requesting known-dead accounts every pass burns calls to learn nothing."""
    dids = await add_followers(3)
    client = client_for({dids[0]: detailed(dids[0], 1000)})
    await profiles.hydrate(client)
    await client.aclose()

    remaining = await store.stale_actor_dids()
    assert dids[1] not in remaining
    assert dids[2] not in remaining


async def test_hydration_batches_at_25():
    dids = await add_followers(60)
    client = client_for({d: detailed(d, 100) for d in dids})
    result = await profiles.hydrate(client)
    await client.aclose()

    assert result["hydrated"] == 60
    assert client.calls == 3, "60 actors is 3 batches of 25"


async def test_hydration_respects_the_limit():
    dids = await add_followers(60)
    client = client_for({d: detailed(d, 100) for d in dids})
    result = await profiles.hydrate(client, limit=25)
    await client.aclose()

    assert result["hydrated"] == 25
    assert client.calls == 1


async def test_fresh_profiles_are_skipped_on_the_ttl():
    dids = await add_followers(3)
    client = client_for({d: detailed(d, 100) for d in dids})
    await profiles.hydrate(client)
    await client.aclose()

    assert await store.stale_actor_dids() == [], "nothing should be stale immediately after"


async def test_newest_followers_hydrate_first():
    """A brand-new follower shouldn't wait behind 10,000 stale ones.

    Ordering keys on list_rank, not first_seen_at: a sweep writes ~10,000 rows
    in under 40s so timestamps collide, while list_rank is the live position in
    a newest-follow-first list. Rank 0 is always the most recent follower.
    """
    await add_followers(3)  # ranks 0,1,2
    await store.upsert_actor(actor("did:plc:newest"))
    await store.mark_seen("did:plc:newest", 0)
    # The incumbent at rank 0 is pushed down, as a real sweep would do.
    await store.mark_seen("did:plc:h0", 1)
    await store.mark_seen("did:plc:h1", 2)
    await store.mark_seen("did:plc:h2", 3)

    order = await store.stale_actor_dids()
    assert order[0] == "did:plc:newest"
    assert order == ["did:plc:newest", "did:plc:h0", "did:plc:h1", "did:plc:h2"]


async def test_hydration_order_survives_identical_timestamps():
    """The production case: every row in a sweep shares a timestamp."""
    for i in range(5):
        did = f"did:plc:t{i}"
        await store.upsert_actor(actor(did))
        await store.mark_seen(did, i)

    order = await store.stale_actor_dids()
    assert order == [f"did:plc:t{i}" for i in range(5)]


async def test_hydration_writes_a_score():
    dids = await add_followers(1)
    client = client_for({dids[0]: detailed(dids[0], followers=50_000, follows=200)})
    await profiles.hydrate(client)
    await client.aclose()

    rows = await store.ranked_followers()
    assert rows[0]["influence_score"] is not None
    assert rows[0]["components"], "the UI needs the decomposition"


async def test_hydration_preserves_verification_from_the_sweep():
    await store.upsert_actor(verified("did:plc:v", issuer="nytimes.com"))
    await store.mark_seen("did:plc:v", 0)

    payload = detailed("did:plc:v", 9000)
    payload["verification"] = verified("did:plc:v", issuer="nytimes.com")["verification"]
    client = client_for({"did:plc:v": payload})
    await profiles.hydrate(client)
    await client.aclose()

    summary = await store.verified_summary()
    assert summary["total"] == 1
    assert summary["groups"][0]["issuer"] == "nytimes.com"


async def test_ranking_orders_by_score():
    dids = await add_followers(3)
    known = {
        dids[0]: detailed(dids[0], followers=100, follows=100),
        dids[1]: detailed(dids[1], followers=500_000, follows=300),
        dids[2]: detailed(dids[2], followers=5_000, follows=4_000),
    }
    client = client_for(known)
    await profiles.hydrate(client)
    await client.aclose()

    rows = await store.ranked_followers()
    assert rows[0]["did"] == dids[1], "the large, selective account ranks first"
    assert rows[0]["influence_score"] >= rows[-1]["influence_score"]


async def test_rescore_is_idempotent():
    dids = await add_followers(2)
    client = client_for({d: detailed(d, 10_000) for d in dids})
    await profiles.hydrate(client)
    await client.aclose()

    before = [r["influence_score"] for r in await store.ranked_followers()]
    await profiles.rescore()
    after = [r["influence_score"] for r in await store.ranked_followers()]
    assert before == after


async def test_departed_followers_are_not_hydrated():
    dids = await add_followers(2)
    await store.mark_departed([dids[1]], reason="unfollow")

    stale = await store.stale_actor_dids()
    assert dids[1] not in stale


async def test_hydration_progress_reports_coverage():
    dids = await add_followers(4)
    client = client_for({dids[0]: detailed(dids[0], 100), dids[1]: detailed(dids[1], 200)})
    await profiles.hydrate(client, limit=2)
    await client.aclose()

    progress = await store.hydration_progress()
    assert progress["total"] == 4
    assert progress["hydrated"] == 2
    assert progress["pct"] == 50.0
