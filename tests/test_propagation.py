"""M15c — guilt by association over the follow graph.

Rules cannot find "civic tech" or "privacy activist": those are communities
rather than job titles, and they are legible in who follows whom long before
they appear in a bio.
"""

import pytest

from sonde.db import store
from tests.fakes import verified


@pytest.fixture
async def db(tmp_path):
    store.set_db_path(str(tmp_path / "prop.db"))
    await store.connect()
    yield store
    await store.close()
    store.set_db_path(None)


async def follower(did: str, rank: int = 0, **fields) -> None:
    await store.upsert_actor(verified(did))
    await store.mark_seen(did, rank)
    if fields:
        conn = await store._db()
        sets = ", ".join(f"{k} = ?" for k in fields)
        await conn.execute(f"UPDATE actors SET {sets} WHERE did = ?",
                           (*fields.values(), did))
        await store.commit()


async def edges(pairs: list[tuple[str, str]]) -> None:
    await store.store_affinity_edges(pairs)


async def test_nothing_happens_without_a_follow_graph(db):
    await follower("did:plc:a", wikidata_occupations='["journalist"]')
    await store.classify_groups()
    result = await store.propagate_groups()
    assert result["proposed"] == 0
    assert "rebuild the affinity index" in result["skipped"]


async def test_someone_the_group_sources_follow_is_proposed(db):
    """Three journalists share sources; a fourth account they all follow is a
    candidate even with nothing in its bio."""
    for i in range(3):
        await follower(f"did:plc:j{i}", i, wikidata_occupations='["journalist"]')
    await follower("did:plc:unknown", 9)          # no occupation, no bio signal
    await store.classify_groups()

    shared = [(f"did:plc:src{s}", f"did:plc:j{i}") for s in range(4) for i in range(3)]
    shared += [(f"did:plc:src{s}", "did:plc:unknown") for s in range(4)]
    await edges(shared)

    result = await store.propagate_groups()

    assert result["proposed"] >= 1
    members = {m["did"] for m in await store.group_members("journalists")}
    assert "did:plc:unknown" in members


async def test_a_proposal_is_marked_as_such(db):
    for i in range(3):
        await follower(f"did:plc:j{i}", i, wikidata_occupations='["journalist"]')
    await follower("did:plc:maybe", 9)
    await store.classify_groups()
    await edges([(f"did:plc:s{s}", f"did:plc:j{i}") for s in range(4) for i in range(3)]
                + [(f"did:plc:s{s}", "did:plc:maybe") for s in range(4)])
    await store.propagate_groups()

    row = next(m for m in await store.group_members("journalists")
               if m["did"] == "did:plc:maybe")
    assert row["tier"] == "propagation"
    assert row["confidence"] < 0.6, "guilt by association is weak evidence"
    assert "characterise this group" in row["evidence"]


async def test_propagation_never_overrides_stronger_evidence(db):
    for i in range(3):
        await follower(f"did:plc:j{i}", i, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await edges([(f"did:plc:s{s}", f"did:plc:j{i}") for s in range(4) for i in range(3)])

    await store.propagate_groups()

    row = next(m for m in await store.group_members("journalists")
               if m["did"] == "did:plc:j0")
    assert row["tier"] == "wikidata", "an existing member keeps its stronger tier"


async def test_a_group_with_too_few_seeds_does_not_propagate(db):
    await follower("did:plc:only", 0, wikidata_occupations='["journalist"]')
    await follower("did:plc:other", 1)
    await store.classify_groups()
    await edges([("did:plc:s1", "did:plc:only"), ("did:plc:s1", "did:plc:other")])

    assert (await store.propagate_groups())["proposed"] == 0


async def test_a_weak_overlap_is_not_enough(db):
    for i in range(3):
        await follower(f"did:plc:j{i}", i, wikidata_occupations='["journalist"]')
    await follower("did:plc:stranger", 9)
    await store.classify_groups()
    # The group shares four sources; the stranger shares one of them.
    pairs = [(f"did:plc:s{s}", f"did:plc:j{i}") for s in range(4) for i in range(3)]
    pairs.append(("did:plc:s0", "did:plc:stranger"))
    await edges(pairs)

    await store.propagate_groups()

    assert "did:plc:stranger" not in {
        m["did"] for m in await store.group_members("journalists")}


async def test_rebuilding_the_index_replaces_edges(db):
    await edges([("did:plc:a", "did:plc:b")])
    await edges([("did:plc:c", "did:plc:d")])
    conn = await store._db()
    async with conn.execute("SELECT source_did FROM affinity_edges") as cur:
        assert {r["source_did"] for r in await cur.fetchall()} == {"did:plc:c"}
