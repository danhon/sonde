"""M8 — affiliations of different kinds, from several sources."""

import pytest

from sonde.affiliations import (
    Affiliation, KIND_WEIGHT, from_links, from_wikidata, kind_from_role, role_near,
)
from sonde.db import store
from sonde.organisations import default_weight
from tests.fakes import actor


@pytest.mark.parametrize(
    "role,kind",
    [
        ("President", "leadership"),
        ("Chief Executive", "leadership"),
        ("Editor-in-Chief", "leadership"),
        ("co-founder", "founder"),
        ("Board member", "board"),
        ("Professor", "academic"),
        ("PhD candidate", "academic"),
        ("Former reporter", "former"),
        ("Reporter", "employment"),
        (None, "employment"),
    ],
)
def test_role_phrases_map_to_a_kind(role, kind):
    assert kind_from_role(role) == kind


def test_leading_beats_working_there():
    """The Whittaker case: President of Signal is not the same as employed by it."""
    assert KIND_WEIGHT["leadership"] > KIND_WEIGHT["employment"]
    assert KIND_WEIGHT["former"] < KIND_WEIGHT["employment"] / 2


def test_role_is_read_from_either_side_of_the_organisation():
    assert "president" in (role_near("President of Signal", "Signal") or "")
    assert "editor" in (role_near("The Verge features editor", "The Verge") or "")


def test_a_missing_organisation_yields_no_role():
    assert role_near("nothing relevant here", "Signal") is None


def test_wikidata_positions_become_leadership_by_default():
    affs = from_wikidata([], ["chairperson"], qid="Q1")
    assert affs[0].kind == "leadership"
    assert affs[0].source_url.endswith("Q1")


def test_wikidata_employers_become_employment():
    affs = from_wikidata(["Microsoft"], [], qid="Q1")
    assert affs[0].kind == "employment"
    assert affs[0].confidence > 0.9


def test_a_newsletter_link_is_an_own_publication():
    """The Petersen case: the influential thing is theirs, so there is no
    employer to look up."""
    affs = from_links([{"kind": "newsletter", "host": "foo.substack.com",
                        "url": "https://foo.substack.com", "note": "publishes"}])
    assert affs[0].kind == "own_publication"
    assert affs[0].source_url == "https://foo.substack.com"


def test_opaque_link_kinds_are_not_affiliations():
    assert from_links([{"kind": "site", "host": "x.com", "url": "https://x.com"}]) == []


def test_strength_combines_confidence_and_kind():
    lead = Affiliation("Signal", "leadership", "wikidata", 0.97)
    former = Affiliation("Signal", "former", "claimed", 0.97)
    assert lead.strength > former.strength
    assert lead.strength <= 1.0


# ------------------------------------------------- organisation weights

def test_weight_derives_from_notability_not_a_list():
    """The point: Signal gets a sensible weight without anyone adding Signal."""
    assert default_weight(150) > default_weight(20) >= default_weight(None)


def test_a_kind_floor_applies_when_notability_is_unknown():
    assert default_weight(None, "news") > default_weight(None, "newsletter")


def test_weights_stay_in_range():
    for n in (None, 0, 1, 50, 5000):
        assert 0.3 <= default_weight(n) <= 1.0


# ------------------------------------------------------------ storage

@pytest.fixture
async def db(tmp_path):
    store.set_db_path(str(tmp_path / "aff8.db"))
    await store.connect()
    yield store
    await store.close()
    store.set_db_path(None)


async def add(did: str, **fields) -> None:
    profile = actor(did)
    profile["description"] = fields.pop("description", None)
    await store.upsert_actor(profile)
    await store.mark_seen(did, 0)
    if fields:
        conn = await store._db()
        sets = ", ".join(f"{k} = ?" for k in fields)
        await conn.execute(f"UPDATE actors SET {sets} WHERE did = ?",
                           (*fields.values(), did))
        await store.commit()


async def test_several_kinds_coexist_for_one_person(db):
    """The single institution_* column set could not express this."""
    await add("did:plc:m", wikidata_employers='["Signal"]',
              wikidata_positions='["president"]',
              link_signals='[{"kind":"newsletter","host":"m.substack.com","url":"https://m.substack.com"}]')
    await store.rebuild_affiliations()

    kinds = {a["kind"] for a in await store.affiliations_for("did:plc:m")}
    assert {"employment", "leadership", "own_publication"} <= kinds


async def test_best_evidence_wins_rather_than_accumulating(db):
    await add("did:plc:b", wikidata_employers='["Wired"]')
    await store.rebuild_affiliations()
    score, row = await store.best_affiliation_score("did:plc:b")
    assert 0 < score <= 1.0
    assert row["org_name"] == "Wired"


async def test_a_confirmed_affiliation_survives_a_rebuild(db):
    await add("did:plc:c", wikidata_employers='["Wired"]')
    await store.rebuild_affiliations()
    aff = (await store.unreviewed_affiliations())[0]
    await store.review_affiliation(aff["id"], True)

    await add("did:plc:c", wikidata_employers='[]')
    await store.rebuild_affiliations()

    assert [a["org_name"] for a in await store.affiliations_for("did:plc:c")] == ["Wired"]


async def test_a_rejected_affiliation_stops_counting(db):
    await add("did:plc:r", wikidata_employers='["Wrong Place"]')
    await store.rebuild_affiliations()
    aff = (await store.unreviewed_affiliations())[0]
    await store.review_affiliation(aff["id"], False)

    assert await store.affiliations_for("did:plc:r") == []


async def test_a_human_set_organisation_weight_is_never_overwritten(db):
    org_id = await store.upsert_organisation("Signal", sitelinks=5)
    conn = await store._db()
    await conn.execute(
        "UPDATE organisations SET weight = 0.99, weight_locked = 1 WHERE id = ?", (org_id,))
    await store.commit()

    await store.upsert_organisation("Signal", sitelinks=120)

    async with conn.execute("SELECT weight FROM organisations WHERE id = ?", (org_id,)) as cur:
        assert (await cur.fetchone())["weight"] == 0.99
