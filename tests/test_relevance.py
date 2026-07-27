"""Tier 3b — exact relevance via getKnownFollowers."""

import httpx
import pytest

from sonde.api import auth as auth_module
from sonde.api.client import BlueskyClient
from sonde.db import store
from sonde.scoring import AFFINITY_EXACT_FULL
from sonde.sync import relevance
from tests.fakes import actor, routed


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "rel.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


@pytest.fixture
def authed(monkeypatch):
    """Pretend a session exists; the endpoint is 401 without one."""
    async def token(self=None):
        return "fake-token"

    monkeypatch.setattr(auth_module.authenticator, "token", token)
    yield


async def add_follower(did: str, rank: int = 0, score: float = 50.0) -> None:
    await store.upsert_actor(actor(did))
    await store.mark_seen(did, rank)
    db = await store._db()
    await db.execute("UPDATE actors SET influence_score = ? WHERE did = ?", (score, did))


def known_client(counts: dict[str, int]) -> BlueskyClient:
    """Serves `counts[did]` known followers, paginating at 100."""
    def handler(request):
        did = request.url.params.get("actor")
        cursor = int(request.url.params.get("cursor") or 0)
        total = counts.get(did, 0)
        remaining = max(total - cursor, 0)
        page = min(remaining, 100)
        body = {"subject": {"did": did},
                "followers": [{"did": f"{did}-f{i}"} for i in range(page)]}
        if cursor + page < total:
            body["cursor"] = str(cursor + page)
        return httpx.Response(200, json=body)

    return BlueskyClient(
        "https://fake",
        transport=routed({"app.bsky.graph.getKnownFollowers": handler}),
        per_second=1000,
    )


async def test_exact_counts_are_recorded(authed):
    await add_follower("did:plc:a", 0)
    c = known_client({"did:plc:a": 37})
    result = await relevance.enrich(c, limit=10)
    await c.aclose()

    assert result["enriched"] == 1
    assert (await store.follower_detail("did:plc:a"))["affinity_exact"] == 37


async def test_counting_stops_once_the_score_saturates(authed):
    """Beyond the ceiling the score is identical, so paying for more is waste."""
    await add_follower("did:plc:huge", 0)
    c = known_client({"did:plc:huge": 4000})
    await relevance.enrich(c, limit=10)
    calls = c.calls
    await c.aclose()

    count = (await store.follower_detail("did:plc:huge"))["affinity_exact"]
    assert count >= AFFINITY_EXACT_FULL
    assert calls <= relevance.MAX_PAGES, "must not walk 40 pages for one actor"


async def test_pagination_is_followed_up_to_the_cap(authed):
    await add_follower("did:plc:mid", 0)
    c = known_client({"did:plc:mid": 150})
    await relevance.enrich(c, limit=10)
    await c.aclose()

    assert (await store.follower_detail("did:plc:mid"))["affinity_exact"] == 150


async def test_the_exact_count_beats_the_sampled_one_in_scoring(authed):
    await add_follower("did:plc:s", 0)
    db = await store._db()
    await db.execute("UPDATE actors SET affinity_sampled = 1 WHERE did = 'did:plc:s'")

    c = known_client({"did:plc:s": AFFINITY_EXACT_FULL})
    await relevance.enrich(c, limit=10)
    await c.aclose()

    detail = await store.follower_detail("did:plc:s")
    affinity = next(x for x in detail["components"] if x["key"] == "affinity")
    assert "exact" in affinity["source"]
    assert affinity["value"] == pytest.approx(1.0)


async def test_without_a_session_it_skips_rather_than_burning_calls(monkeypatch):
    async def no_token(self=None):
        return None

    monkeypatch.setattr(auth_module.authenticator, "token", no_token)
    await add_follower("did:plc:a", 0)
    c = known_client({"did:plc:a": 10})
    result = await relevance.enrich(c, limit=10)
    await c.aclose()

    assert result["skipped"] == "not authenticated"
    assert result["api_calls"] == 0, "401 per actor teaches us nothing N times"


async def test_one_failing_actor_does_not_abort_the_run(authed):
    await add_follower("did:plc:ok", 0)
    await add_follower("did:plc:bad", 1)

    def handler(request):
        if request.url.params.get("actor") == "did:plc:bad":
            return httpx.Response(400, json={"error": "BlockedActor"})
        return httpx.Response(200, json={"subject": {}, "followers": [{"did": "x"}]})

    c = BlueskyClient("https://fake",
                      transport=routed({"app.bsky.graph.getKnownFollowers": handler}),
                      per_second=1000, max_retries=0)
    result = await relevance.enrich(c, limit=10)
    await c.aclose()

    assert result["status"] == "ok"
    assert result["enriched"] == 1
    assert result["failed"] == 1


async def test_hidden_and_self_are_not_enriched(authed):
    await add_follower("did:plc:hidden", 0)
    await store.set_ignored("did:plc:hidden", True)
    assert "did:plc:hidden" not in await store.relevance_targets()


async def test_unenriched_actors_are_prioritised(authed):
    await add_follower("did:plc:done", 0, score=90)
    await add_follower("did:plc:todo", 1, score=10)
    await store.record_exact_affinity("did:plc:done", 5)
    await store.commit()

    assert (await store.relevance_targets())[0] == "did:plc:todo"


async def test_agreement_is_none_without_enough_data(authed):
    assert await store.affinity_agreement() is None
