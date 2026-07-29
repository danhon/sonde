"""M11 — overlapping groups, tiered by evidence."""

import pytest

from sonde.db import store
from sonde.groups import classify
from tests.fakes import actor, verified


def person(**kw) -> dict:
    base = {"affiliations": [], "wikidata_occupations": [], "wikidata_positions": [],
            "link_signals": [], "description": "", "post_texts": []}
    base.update(kw)
    return base


def slugs(actor_dict) -> set[str]:
    return {m.slug for m in classify(actor_dict)}


# --------------------------------------------------------- the tiers

def test_an_affiliation_places_someone_in_a_company_group():
    assert "microsoft" in slugs(person(
        affiliations=[{"org_name": "Microsoft", "kind": "employment"}]))


def test_a_former_affiliation_does_not():
    """Meredith Whittaker left Google in 2019, however long Wikidata keeps the
    statement. A past job is not membership."""
    assert "google" not in slugs(person(
        affiliations=[{"org_name": "Google", "kind": "former"}]))


def test_a_compound_occupation_still_matches():
    """Wikidata says 'artificial intelligence researcher' and 'site reliability
    engineer'; exact matching missed every researcher and engineer."""
    assert "technologists" in slugs(person(
        wikidata_occupations=["artificial intelligence researcher"]))
    assert "developers" in slugs(person(
        wikidata_occupations=["site reliability engineer"])) or "technologists" in slugs(
        person(wikidata_occupations=["site reliability engineer"]))


def test_occupation_matching_is_word_bounded():
    """'editor' must not match inside 'editorial assistant'."""
    assert "journalists" not in slugs(person(wikidata_occupations=["editorial assistant"]))


def test_a_newsletter_link_places_someone_without_any_employer():
    """The Petersen case — the influential thing is theirs."""
    assert "newsletter-writers" in slugs(person(
        link_signals=[{"kind": "newsletter", "host": "x.substack.com"}]))


def test_bio_text_is_used_when_nothing_stronger_exists():
    assert "designers" in slugs(person(description="Product design lead"))


def test_post_text_counts_too():
    assert "privacy" in slugs(person(post_texts=["more surveillance nonsense today"]))


def test_a_past_role_in_text_is_rejected():
    """Same discipline as institution matching."""
    assert "journalists" not in slugs(person(description="former reporter, now a chef"))
    assert "developers" not in slugs(person(description="aspiring developer"))


def test_membership_overlaps_on_purpose():
    both = slugs(person(
        wikidata_occupations=["journalist"],
        link_signals=[{"kind": "newsletter", "host": "x.substack.com"}]))
    assert {"journalists", "newsletter-writers"} <= both


def test_stronger_evidence_wins_for_the_same_group():
    memberships = classify(person(
        wikidata_occupations=["journalist"], description="reporter"))
    journalist = next(m for m in memberships if m.slug == "journalists")
    assert journalist.tier == "wikidata", "wikidata outranks a text match"


def test_every_membership_carries_its_evidence():
    for membership in classify(person(wikidata_occupations=["novelist"])):
        assert membership.evidence
        assert 0 < membership.confidence <= 1


# ------------------------------------------------------------ storage

@pytest.fixture
async def db(tmp_path):
    store.set_db_path(str(tmp_path / "groups.db"))
    await store.connect()
    yield store
    await store.close()
    store.set_db_path(None)


async def add(did: str, **fields) -> None:
    profile = verified(did) if fields.pop("is_verified", False) else actor(did)
    profile["description"] = fields.pop("description", None)
    await store.upsert_actor(profile)
    await store.mark_seen(did, 0)
    if fields:
        conn = await store._db()
        sets = ", ".join(f"{k} = ?" for k in fields)
        await conn.execute(f"UPDATE actors SET {sets} WHERE did = ?",
                           (*fields.values(), did))
        await store.commit()


async def test_only_the_enrichment_set_is_classified(db):
    await add("did:plc:vip", is_verified=True, wikidata_occupations='["journalist"]')
    result = await store.classify_groups()
    assert result["people"] == 1
    assert [m["did"] for m in await store.group_members("journalists")] == ["did:plc:vip"]


async def test_hidden_followers_are_not_grouped(db):
    await add("did:plc:h", is_verified=True, wikidata_occupations='["journalist"]')
    await store.set_ignored("did:plc:h", True)
    assert (await store.classify_groups())["people"] == 0


async def test_removing_someone_sticks_across_a_reclassify(db):
    await add("did:plc:w", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await store.review_group_member("journalists", "did:plc:w", False)

    await store.classify_groups()

    assert await store.group_members("journalists") == []
    assert "journalists" not in {g["slug"] for g in await store.groups_for("did:plc:w")}


async def test_the_summary_counts_members_per_group(db):
    await add("did:plc:a", is_verified=True, wikidata_occupations='["journalist"]')
    await add("did:plc:b", is_verified=True, wikidata_occupations='["novelist"]')
    await store.classify_groups()

    counts = {g["slug"]: g["members"] for g in await store.group_summary()}
    assert counts["journalists"] == 1
    assert counts["novelists"] == 1
    assert counts["apple"] == 0


async def test_the_groups_page_renders(db):
    from fastapi.testclient import TestClient

    from sonde.web.app import create_app

    await add("did:plc:a", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    with TestClient(create_app()) as client:
        assert client.get("/circles").status_code == 200
        page = client.get("/circles?slug=journalists")
    assert page.status_code == 200
    assert "a.bsky.social" in page.text
