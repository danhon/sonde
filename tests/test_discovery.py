"""M15b — proposing groups nobody thought to name."""

from collections import Counter

import pytest

from sonde.db import store
from sonde.discovery import (
    covered_terms, link_candidates, occupation_candidates,
    organisation_candidates, phrase_candidates,
)
from tests.fakes import verified


def test_an_uncovered_occupation_becomes_a_candidate():
    out = occupation_candidates(Counter({"blogger": 9}), covered_terms())
    assert out and out[0]["label"] == "Blogger"
    assert "9 people" in out[0]["why"]


def test_an_occupation_a_group_already_claims_is_not_proposed():
    assert occupation_candidates(Counter({"journalist": 20}), covered_terms()) == []


def test_a_lone_example_is_not_a_group():
    assert occupation_candidates(Counter({"lighthouse keeper": 1}), set()) == []


def test_link_kinds_without_a_group_are_proposed():
    out = link_candidates(Counter({"supported": 3}), covered_terms())
    assert out[0]["term"] == "supported"


def test_the_catch_all_site_kind_is_never_proposed():
    """Every self-hosted domain lands in `site`; that is not a group."""
    assert link_candidates(Counter({"site": 1226}), set()) == []


def test_an_organisation_with_several_people_is_already_a_group():
    out = organisation_candidates([{"name": "Wired", "members": 6}], set())
    assert out[0]["label"] == "Wired"
    assert "already a group" in out[0]["why"]


# ------------------------------------------------------------ phrases

def test_url_fragments_are_not_phrases():
    """The first real run proposed 'Bsky Social', 'Bsky App', 'Mastodon Social'
    and 'App Profile' — all fragments of links, none written by anyone."""
    docs = ["find me at https://bsky.app/profile/x" for _ in range(10)]
    labels = {c["label"] for c in phrase_candidates(docs, set(), min_count=2)}
    assert not any("Bsky" in l or "Profile" in l for l in labels)


def test_bigrams_do_not_span_punctuation():
    """'digital rights, human dignity' must not yield 'rights human'."""
    docs = ["digital rights, human dignity" for _ in range(6)]
    terms = {c["term"] for c in phrase_candidates(docs, set(), min_count=2)}
    assert "rights human" not in terms


def test_a_reversed_duplicate_keeps_the_commoner_form():
    # Padded with unrelated bios so the share cap does not treat the phrase as
    # filler — it is a community signal at 10 of 100, not at 10 of 10.
    docs = (["trans rights human rights"] * 5 + ["human rights matter"] * 5
            + [f"unrelated bio number {i}" for i in range(90)])
    terms = {c["term"] for c in phrase_candidates(docs, set(), min_count=2)}
    assert "human rights" in terms
    assert "rights human" not in terms


def test_a_phrase_in_almost_every_bio_is_filler_not_a_community():
    docs = ["personal account here" for _ in range(100)]
    assert phrase_candidates(docs, set(), min_count=2, max_share=0.15) == []


def test_phrases_matching_an_existing_group_are_skipped():
    docs = ["freelance newsletter writer" for _ in range(6)]
    assert not any("newsletter" in c["term"]
                   for c in phrase_candidates(docs, covered_terms(), min_count=2))


# ------------------------------------------------------------ storage

@pytest.fixture
async def db(tmp_path):
    store.set_db_path(str(tmp_path / "disc.db"))
    await store.connect()
    yield store
    await store.close()
    store.set_db_path(None)


async def add(did: str, **fields) -> None:
    await store.upsert_actor(verified(did))
    await store.mark_seen(did, 0)
    if fields:
        conn = await store._db()
        sets = ", ".join(f"{k} = ?" for k in fields)
        await conn.execute(f"UPDATE actors SET {sets} WHERE did = ?",
                           (*fields.values(), did))
        await store.commit()


async def test_discovery_proposes_without_creating(db):
    await add("did:plc:a", wikidata_occupations='["blogger"]')
    await add("did:plc:b", wikidata_occupations='["blogger"]')

    result = await store.discover_group_candidates()

    assert result["candidates"] >= 1
    assert any(c["term"] == "blogger" for c in await store.group_candidates())
    assert not any(g["slug"] == "blogger" for g in await store.group_summary()), \
        "a proposal is not a group"


async def test_accepting_creates_the_group_and_fills_it(db):
    await add("did:plc:a", wikidata_occupations='["blogger"]')
    await add("did:plc:b", wikidata_occupations='["blogger"]')
    await store.discover_group_candidates()
    candidate = next(c for c in await store.group_candidates() if c["term"] == "blogger")

    slug = await store.decide_candidate(candidate["id"], True)

    assert slug == "blogger"
    assert {m["did"] for m in await store.group_members(slug)} == {"did:plc:a", "did:plc:b"}


async def test_a_rejected_proposal_is_not_raised_again(db):
    await add("did:plc:a", wikidata_occupations='["blogger"]')
    await add("did:plc:b", wikidata_occupations='["blogger"]')
    await store.discover_group_candidates()
    candidate = next(c for c in await store.group_candidates() if c["term"] == "blogger")
    await store.decide_candidate(candidate["id"], False)

    await store.discover_group_candidates()

    assert not any(c["term"] == "blogger" for c in await store.group_candidates())
    assert any(c["term"] == "blogger" for c in await store.group_candidates(decided=False))


async def test_an_accepted_organisation_group_uses_current_affiliations_only(db):
    # Two current, so the organisation clears the "several people" bar, plus one
    # former who must not be swept in.
    await add("did:plc:now")
    await add("did:plc:now2")
    await add("did:plc:then")
    conn = await store._db()
    org_id = await store.upsert_organisation("Wired")
    for did, kind in (("did:plc:now", "employment"), ("did:plc:now2", "employment"),
                      ("did:plc:then", "former")):
        await conn.execute(
            """INSERT INTO affiliations (did, org_id, org_name, kind, method,
                 confidence, first_seen_at) VALUES (?,?,?,?,?,?,?)""",
            (did, org_id, "Wired", kind, "attested", 1.0, store.utcnow()))
    await store.commit()

    await store.discover_group_candidates()
    candidate = next(c for c in await store.group_candidates() if c["term"] == "Wired")
    slug = await store.decide_candidate(candidate["id"], True)

    members = {m["did"] for m in await store.group_members(slug)}
    assert members == {"did:plc:now", "did:plc:now2"}
    assert "did:plc:then" not in members, "a former affiliation is not membership"


async def test_the_discovery_page_renders(db):
    from fastapi.testclient import TestClient

    from sonde.web.app import create_app

    await add("did:plc:a", wikidata_occupations='["blogger"]')
    await add("did:plc:b", wikidata_occupations='["blogger"]')
    await store.discover_group_candidates()
    with TestClient(create_app()) as client:
        page = client.get("/groups/discover")
    assert page.status_code == 200
    assert "Blogger" in page.text
