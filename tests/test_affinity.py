"""M6c — the inverted affinity index.

Source selection here is the thing a pilot corrected. Taking "the most selective
accounts I follow" picks accounts following 0–15 people and reaches 0.8% of
followers; a band sample reaches 14% from a third as many sources. Selectivity
moved onto the hit weight instead.
"""

import httpx
import pytest

from sonde.api.client import BlueskyClient
from sonde.config import settings
from sonde.db import store
from sonde.sync import affinity
from tests.fakes import actor, routed


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "aff.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


async def add_follower(did: str, rank: int = 0) -> None:
    await store.upsert_actor(actor(did))
    await store.mark_seen(did, rank)


async def add_source(did: str, follows_count: int, follows: list[str],
                     verified: bool = False) -> None:
    profile = actor(did)
    await store.upsert_actor(profile)
    db = await store._db()
    await db.execute(
        "UPDATE actors SET follows_count = ?, verified_status = ? WHERE did = ?",
        (follows_count, "valid" if verified else "none", did),
    )
    await store.replace_my_follows(list(SOURCES) + [did])
    SOURCES[did] = follows


SOURCES: dict[str, list[str]] = {}


def index_client() -> BlueskyClient:
    def handler(request):
        actor_param = request.url.params.get("actor")
        follows = SOURCES.get(actor_param, [])
        return httpx.Response(
            200, json={"follows": [{"did": d, "handle": f"{d}.test"} for d in follows]}
        )

    return BlueskyClient(
        "https://fake", transport=routed({"app.bsky.graph.getFollows": handler}),
        per_second=1000,
    )


@pytest.fixture(autouse=True)
def _clear_sources():
    SOURCES.clear()
    yield
    SOURCES.clear()


# ------------------------------------------------------------ weighting

def test_a_selective_source_counts_for_more_than_a_promiscuous_one():
    """This is where 'following 200 endorses more than following 50,000' lives."""
    assert affinity.source_weight(150) == pytest.approx(1.0)
    assert affinity.source_weight(1500) == pytest.approx(0.1)
    assert affinity.source_weight(300) > affinity.source_weight(900)


def test_weight_is_floored_so_wide_sources_still_contribute():
    assert affinity.source_weight(100_000) == 0.1


def test_missing_follows_count_does_not_crash_the_weighting():
    assert affinity.source_weight(None) == 1.0
    assert affinity.source_weight(0) == 1.0


# --------------------------------------------------------- source band

async def test_sources_come_from_a_band_not_the_cheapest():
    """The corrected rule: accounts following almost nobody are excluded."""
    await add_follower("did:plc:f1", 0)
    await add_source("did:plc:tiny", 5, ["did:plc:f1"])
    await add_source("did:plc:good", 400, ["did:plc:f1"])
    await add_source("did:plc:huge", 90_000, ["did:plc:f1"])

    chosen = await store.choose_affinity_sources(150, 2000, 600)

    assert [s["did"] for s in chosen] == ["did:plc:good"]


async def test_source_cap_is_respected():
    await add_follower("did:plc:f1", 0)
    for i in range(10):
        await add_source(f"did:plc:s{i}", 200 + i, ["did:plc:f1"])

    chosen = await store.choose_affinity_sources(150, 2000, 3)
    assert len(chosen) == 3


async def test_cheapest_first_within_the_band():
    await add_follower("did:plc:f1", 0)
    await add_source("did:plc:big", 1900, ["did:plc:f1"])
    await add_source("did:plc:small", 160, ["did:plc:f1"])

    chosen = await store.choose_affinity_sources(150, 2000, 600)
    assert chosen[0]["did"] == "did:plc:small"


# ---------------------------------------------------------- index build

async def test_index_scores_only_my_followers():
    await add_follower("did:plc:mine", 0)
    await add_source("did:plc:src", 200, ["did:plc:mine", "did:plc:stranger"])

    c = index_client()
    result = await affinity.build_index(c)
    await c.aclose()

    assert result["reached"] == 1
    detail = await store.follower_detail("did:plc:mine")
    assert detail["affinity_sampled"] > 0
    assert await store.follower_detail("did:plc:stranger") is None


async def test_hits_accumulate_weighted_across_sources():
    await add_follower("did:plc:popular", 0)
    await add_source("did:plc:a", 150, ["did:plc:popular"])   # weight 1.0
    await add_source("did:plc:b", 1500, ["did:plc:popular"])  # weight 0.1

    c = index_client()
    await affinity.build_index(c)
    await c.aclose()

    detail = await store.follower_detail("did:plc:popular")
    assert detail["affinity_sampled"] == pytest.approx(1.1, abs=0.01)


async def test_zero_is_recorded_as_measured_not_missing():
    """Once the index exists, no hits means zero — not 'unavailable'."""
    await add_follower("did:plc:ignored", 0)
    await add_follower("did:plc:seen", 1)
    await add_source("did:plc:src", 200, ["did:plc:seen"])

    c = index_client()
    await affinity.build_index(c)
    await c.aclose()

    detail = await store.follower_detail("did:plc:ignored")
    assert detail["affinity_sampled"] == 0
    component = next(c for c in detail["components"] if c["key"] == "affinity")
    assert component["available"] is True, "measured-and-none must beat never-measured"


async def test_verified_affinity_counts_only_verified_sources():
    await add_follower("did:plc:target", 0)
    await add_source("did:plc:plain", 200, ["did:plc:target"], verified=False)
    await add_source("did:plc:vsrc", 200, ["did:plc:target"], verified=True)

    c = index_client()
    result = await affinity.build_index(c)
    await c.aclose()

    detail = await store.follower_detail("did:plc:target")
    assert detail["verified_affinity"] == 1
    assert result["verified_reached"] == 1


async def test_one_broken_source_does_not_abort_the_run():
    """A 600-source run must survive an unreachable account."""
    await add_follower("did:plc:f", 0)
    await add_source("did:plc:ok", 200, ["did:plc:f"])
    await add_source("did:plc:broken", 200, ["did:plc:f"])

    def handler(request):
        if request.url.params.get("actor") == "did:plc:broken":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"follows": [{"did": "did:plc:f"}]})

    c = BlueskyClient("https://fake", transport=routed({"app.bsky.graph.getFollows": handler}),
                      per_second=1000, max_retries=0)
    result = await affinity.build_index(c)
    await c.aclose()

    assert result["status"] == "ok"
    assert result["sources"] == 1
    assert result["failed_sources"] == 1


async def test_build_is_a_noop_without_sources():
    await add_follower("did:plc:f", 0)
    c = index_client()
    result = await affinity.build_index(c)
    await c.aclose()

    assert result["status"] == "ok"
    assert "sync follows first" in result["skipped"]
    assert result["api_calls"] == 0


async def test_sources_are_recorded_for_reproducibility():
    await add_follower("did:plc:f", 0)
    await add_source("did:plc:src", 300, ["did:plc:f"])

    c = index_client()
    await affinity.build_index(c)
    await c.aclose()

    assert await store.affinity_source_count() == 1


async def test_rebuild_clears_previous_scores():
    """A follower who loses all hits must go to 0, not keep a stale number."""
    await add_follower("did:plc:was-popular", 0)
    await add_source("did:plc:src", 200, ["did:plc:was-popular"])
    c = index_client()
    await affinity.build_index(c)
    await c.aclose()
    assert (await store.follower_detail("did:plc:was-popular"))["affinity_sampled"] > 0

    SOURCES["did:plc:src"] = []
    c = index_client()
    await affinity.build_index(c)
    await c.aclose()

    assert (await store.follower_detail("did:plc:was-popular"))["affinity_sampled"] == 0


async def test_the_subject_never_ranks_among_its_own_followers():
    """We appear in other people's follow lists and would otherwise top the index."""
    from sonde.config import settings
    from sonde.sync import profiles

    await store.upsert_actor(actor("did:plc:me", handle=settings.actor))
    await store.mark_seen("did:plc:me", 0)
    await store.set_meta("subject_did", "did:plc:me")
    await add_follower("did:plc:other", 1)
    await add_source("did:plc:src", 200, ["did:plc:me", "did:plc:other"])

    c = index_client()
    await affinity.build_index(c)
    await c.aclose()
    await profiles.rescore()

    assert (await store.follower_detail("did:plc:me"))["influence_score"] is None
    assert [r["did"] for r in await store.ranked_followers()] == ["did:plc:other"]
