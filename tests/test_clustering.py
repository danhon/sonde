"""M21 — latent groups found in the follow graph.

The point of this module is to find groups nobody wrote down, so the tests are
mostly about the ways that goes wrong: one cluster swallowing the graph, labels
made of URL fragments, and proposals that quietly become memberships.
"""

from __future__ import annotations

from collections import Counter

import pytest

from sonde import clustering


def community(prefix: str, people: int, sources: int, start: int = 0) -> dict:
    """A tight community: everyone follows the same distinct set of sources."""
    return {
        f"did:plc:{prefix}{i}": {f"src:{prefix}{s}" for s in range(sources)}
        for i in range(start, start + people)
    }


# ------------------------------------------------- the graph

def test_two_communities_come_back_as_two():
    graph = {**community("a", 8, 6), **community("b", 8, 6)}
    clusters = clustering.cluster(graph)

    assert len(clusters) == 2
    for members in clusters:
        prefixes = {m.split(":")[-1][0] for m in members}
        assert len(prefixes) == 1, f"communities were merged: {members}"


def test_a_hub_source_does_not_merge_everything():
    """The failure that made the kNN construction necessary.

    Keeping the globally strongest edges instead of each node's nearest
    neighbours collapsed the real graph into one 465-node cluster. A source
    everyone follows must not join separate communities together.
    """
    graph = {**community("a", 10, 5), **community("b", 10, 5)}
    for did in graph:
        graph[did].add("src:everyone")

    clusters = clustering.cluster(graph)
    assert len(clusters) >= 2, "a universally-followed source merged the graph"
    biggest = max(len(c) for c in clusters)
    assert biggest <= 12, f"one cluster swallowed the graph: {biggest}"


def test_followers_with_too_little_signal_are_not_placed():
    """One or two shared sources is coincidence, not a community."""
    graph = {**community("a", 6, 5)}
    graph["did:plc:thin"] = {"src:a0"}
    clusters = clustering.cluster(graph)

    assert all("did:plc:thin" not in members for members in clusters)


def test_clusters_outside_the_size_band_are_not_proposed():
    small = clustering.cluster(community("a", 3, 5))
    assert small == [], "a 3-person cluster is an anecdote"

    huge = clustering.cluster(community("a", 200, 6), max_members=120)
    assert huge == [], "a 200-person cluster is not a group"


def test_the_result_is_deterministic():
    """A review queue that reshuffles itself cannot be reviewed."""
    graph = {**community("a", 9, 6), **community("b", 9, 6),
             **community("c", 9, 6)}
    first = clustering.cluster(graph)
    for _ in range(4):
        assert clustering.cluster(graph) == first


def test_an_empty_graph_is_not_an_error():
    assert clustering.cluster({}) == []
    assert clustering.neighbour_graph({}) == {}


# ------------------------------------------------- naming

def corpus_of(values: dict[str, list[str]]) -> Counter:
    return Counter(t for terms in values.values() for t in set(terms))


def test_a_label_comes_from_lift_not_frequency():
    """Frequency is what made the old discovery propose "Personal Account"."""
    members = [f"did:plc:{i}" for i in range(10)]
    others = [f"did:plc:x{i}" for i in range(90)]
    text = {did: ["taxonomy", "writer"] for did in members}
    text.update({did: ["writer"] for did in others})

    named = clustering.name_cluster(
        members, {"text": text}, {"text": corpus_of(text)}, len(text))
    # "writer" is commoner in absolute terms; "taxonomy" is what marks them out.
    assert named["term"] == "taxonomy"


@pytest.mark.parametrize("noise", ["linktr", "org", "account", "personal",
                                   "senior", "guy", "stuff"])
def test_boilerplate_never_names_a_community(noise):
    """Measured: the first run labelled clusters "Org" and "Account · Senior"."""
    members = [f"did:plc:{i}" for i in range(10)]
    text = {did: [noise] for did in members}

    named = clustering.name_cluster(
        members, {"text": text}, {"text": corpus_of(text)}, 200)
    assert named["tier"] == "unnamed", f"{noise!r} was used as a label"


def test_urls_are_not_vocabulary():
    """'linktr' and 'org' were labelling communities, straight out of bio links."""
    tokens = clustering._tokens(
        "Game designer https://linktr.ee/someone and also foo.org")
    assert "linktr" not in tokens and "org" not in tokens and "ee" not in tokens
    assert "designer" in tokens


def test_a_stronger_tier_wins():
    """A resolved employer beats a bio word."""
    members = [f"did:plc:{i}" for i in range(8)]
    affiliation = {did: ["signal"] for did in members}
    text = {did: ["privacy"] for did in members}

    named = clustering.name_cluster(
        members, {"affiliation": affiliation, "text": text},
        {"affiliation": corpus_of(affiliation), "text": corpus_of(text)}, 400)
    assert named["tier"] == "affiliation"


def test_an_unnameable_cluster_is_still_proposed():
    """About a third of real clusters have no distinctive vocabulary.

    A real community with no label is worth reviewing; a wrong label is worse
    than none.
    """
    members = [f"did:plc:{i}" for i in range(9)]
    named = clustering.name_cluster(members, {"text": {}}, {}, 100)

    assert named["tier"] == "unnamed"
    assert named["label"] == ""
    assert str(len(members)) in named["why"]


# ------------------------------------------------- end to end, through the DB

from sonde.db import store          # noqa: E402
from tests.fakes import actor       # noqa: E402


async def seed_community(prefix: str, people: int, sources: int, bio: str) -> None:
    db = await store._db()
    for i in range(people):
        did = f"did:plc:{prefix}{i}"
        await store.upsert_actor(actor(did, description=bio))
        await store.mark_seen(did, 0)
        for s in range(sources):
            await db.execute(
                "INSERT INTO affinity_edges (source_did, did) VALUES (?,?) "
                "ON CONFLICT DO NOTHING", (f"did:plc:src{prefix}{s}", did))
    await db.commit()


async def test_latent_discovery_proposes_but_never_creates(db):
    """The rule that makes the whole feature safe to run unattended."""
    await seed_community("a", 8, 6, "narrative designer shipping games")
    await seed_community("b", 8, 6, "constitutional law professor")

    result = await store.discover_latent_groups()
    assert result["proposed"] >= 2

    dbc = await store._db()
    async with dbc.execute("SELECT COUNT(*) c FROM group_members") as cur:
        assert (await cur.fetchone())["c"] == 0, "discovery created memberships"


async def test_a_cluster_candidate_carries_its_members(db):
    """There is no term to re-run later, so the member list is the group.

    Two communities, because one on its own has nothing to be distinct *from*:
    every source would be followed by everybody, IDF would be zero throughout,
    and the correct answer is that there is no structure to find.
    """
    await seed_community("a", 8, 6, "narrative designer shipping games")
    await seed_community("b", 8, 6, "constitutional law professor")
    await store.discover_latent_groups()

    candidates = [c for c in await store.group_candidates()
                  if c["kind"] == "cluster"]
    assert candidates
    import json
    members = json.loads(candidates[0]["members"])
    assert len(members) == candidates[0]["member_count"] >= 4


async def test_accepting_a_cluster_uses_its_stored_members(db):
    await seed_community("a", 8, 6, "narrative designer shipping games")
    await seed_community("b", 8, 6, "constitutional law professor")
    await store.discover_latent_groups()
    candidate = [c for c in await store.group_candidates()
                 if c["kind"] == "cluster"][0]

    slug = await store.decide_candidate(candidate["id"], True)
    assert slug

    dbc = await store._db()
    async with dbc.execute(
        "SELECT COUNT(*) c FROM group_members m JOIN groups g ON g.id = m.group_id "
        "WHERE g.slug = ?", (slug,)) as cur:
        assert (await cur.fetchone())["c"] == candidate["member_count"]


async def test_hidden_and_departed_followers_are_not_clustered(db):
    await seed_community("a", 8, 6, "narrative designer shipping games")
    await seed_community("b", 8, 6, "constitutional law professor")
    dbc = await store._db()
    await dbc.execute(
        "UPDATE follower_state SET ignored_at = '2026-01-01' WHERE did = 'did:plc:a0'")
    await dbc.execute(
        "UPDATE follower_state SET is_current = 0 WHERE did = 'did:plc:a1'")
    await dbc.commit()

    await store.discover_latent_groups()
    import json
    for c in await store.group_candidates():
        if c["kind"] != "cluster":
            continue
        members = json.loads(c["members"])
        assert "did:plc:a0" not in members
        assert "did:plc:a1" not in members


async def test_rerunning_updates_rather_than_duplicates(db):
    await seed_community("a", 8, 6, "narrative designer shipping games")
    await seed_community("b", 8, 6, "constitutional law professor")
    first = await store.discover_latent_groups()
    second = await store.discover_latent_groups()

    assert first["proposed"] == second["proposed"]
    candidates = [c for c in await store.group_candidates()
                  if c["kind"] == "cluster"]
    assert len(candidates) == first["proposed"]
