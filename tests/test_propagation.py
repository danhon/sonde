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


async def journalist_graph(extra_members: int = 6) -> None:
    """A realistic graph: sources distinctive to the group, plus background
    sources that follow everyone.

    Lift needs that background to measure against — a source following the
    whole network characterises nothing, which is exactly the failure the first
    real run produced.
    """
    for i in range(3):
        await follower(f"did:plc:j{i}", i, wikidata_occupations='["journalist"]')
    await follower("did:plc:unknown", 50)
    for k in range(extra_members):
        await follower(f"did:plc:other{k}", 60 + k)
    await store.classify_groups()

    pairs = []
    everyone = [f"did:plc:j{i}" for i in range(3)] + ["did:plc:unknown"] + \
               [f"did:plc:other{k}" for k in range(extra_members)]
    # Background sources follow everybody, so they carry no lift.
    for b in range(3):
        pairs += [(f"did:plc:bg{b}", d) for d in everyone]
    # Distinctive sources follow only the journalists and the unknown account.
    for s in range(4):
        pairs += [(f"did:plc:src{s}", f"did:plc:j{i}") for i in range(3)]
        pairs.append((f"did:plc:src{s}", "did:plc:unknown"))
    await edges(pairs)


async def test_someone_the_group_sources_follow_is_proposed(db):
    """Sources specific to the journalists also follow one unlabelled account,
    which is the whole point: no occupation, no bio signal, still a candidate."""
    await journalist_graph()

    result = await store.propagate_groups()

    assert result["proposed"] >= 1
    members = {m["did"] for m in await store.group_members("journalists")}
    assert "did:plc:unknown" in members


async def test_a_source_that_follows_everyone_characterises_nothing(db):
    """The failure the first real run produced: 25 people in five or more
    groups, because every source came from one person's follow graph."""
    for i in range(3):
        await follower(f"did:plc:j{i}", i, wikidata_occupations='["journalist"]')
    for k in range(6):
        await follower(f"did:plc:other{k}", 60 + k)
    await store.classify_groups()

    everyone = [f"did:plc:j{i}" for i in range(3)] + [f"did:plc:other{k}" for k in range(6)]
    await edges([(f"did:plc:bg{b}", d) for b in range(5) for d in everyone])

    assert (await store.propagate_groups())["proposed"] == 0


async def test_nobody_is_proposed_for_many_groups_at_once(db):
    await journalist_graph()
    await store.propagate_groups()

    conn = await store._db()
    async with conn.execute(
        "SELECT did, COUNT(*) n FROM group_members WHERE tier = 'propagation' "
        "GROUP BY did ORDER BY n DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    if row:
        assert row["n"] <= 2, "being proposed for seven groups means nothing"


async def test_a_proposal_is_marked_as_such(db):
    await journalist_graph()
    await store.propagate_groups()

    row = next(m for m in await store.group_members("journalists")
               if m["did"] == "did:plc:unknown")
    assert row["tier"] == "propagation"
    assert row["confidence"] < 0.6, "guilt by association is weak evidence"
    assert "distinctively follow" in row["evidence"]


async def test_propagation_never_overrides_stronger_evidence(db):
    await journalist_graph()
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
    await journalist_graph()
    await follower("did:plc:stranger", 99)
    # The stranger shares one distinctive source out of four.
    conn = await store._db()
    await conn.execute(
        "INSERT INTO affinity_edges (source_did, did) VALUES (?,?)",
        ("did:plc:src0", "did:plc:stranger"))
    await store.commit()

    await store.propagate_groups()

    assert "did:plc:stranger" not in {
        m["did"] for m in await store.group_members("journalists")}


async def test_rebuilding_the_index_replaces_edges(db):
    await edges([("did:plc:a", "did:plc:b")])
    await edges([("did:plc:c", "did:plc:d")])
    conn = await store._db()
    async with conn.execute("SELECT source_did FROM affinity_edges") as cur:
        assert {r["source_did"] for r in await cur.fetchall()} == {"did:plc:c"}
