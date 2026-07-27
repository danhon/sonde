"""M6a — institutional affiliation.

The governing measurement: only 14 of 147 verified followers (10%) were verified
BY an institution. Jamelle Bouie, the case that motivated the feature, is in the
other 90% — verified by bsky.app, handle on his own domain, NYT mentioned only in
his bio. So bio matching has to work, and its confidence has to be conditional.
"""

import pytest

from sonde import institutions as inst
from sonde.db import store
from sonde.institutions import (
    ATTESTED,
    CLAIMED_PLAIN,
    CLAIMED_VERIFIED,
    DOMAIN,
    ROSTER,
    match_actor,
    seniority_of,
)
from tests.fakes import actor, verified

NYT = {
    "id": 1, "name": "The New York Times", "weight": 1.0,
    "domains": ["nytimes.com", "nyt.com"],
    "aliases": ["New York Times", "NYT"],
}
WIRED = {"id": 2, "name": "Wired", "weight": 0.95, "domains": ["wired.com"], "aliases": ["WIRED"]}
INSTS = [NYT, WIRED]


def person(**kw) -> dict:
    base = {
        "handle": "someone.bsky.social", "description": None,
        "verified_status": "none", "verification_records": [],
    }
    base.update(kw)
    return base


# ------------------------------------------------------ evidence paths

def test_attested_beats_everything():
    """Ryan Mac: verified BY nytimes.com. The cryptographic case."""
    m = match_actor(
        person(handle="rmac.bsky.social", verified_status="valid",
               verification_records=[{"issuerHandle": "nytimes.com"}]),
        INSTS,
    )
    assert m.name == "The New York Times"
    assert m.confidence == ATTESTED
    assert m.method == "attested"


def test_domain_match_on_a_subdomain():
    m = match_actor(person(handle="tomhannen.nytimes.com"), INSTS)
    assert m.confidence == DOMAIN
    assert m.method == "domain"


def test_domain_match_does_not_fire_on_a_lookalike():
    assert match_actor(person(handle="notnytimes.com"), INSTS) is None


def test_roster_membership_counts_as_near_attested():
    m = match_actor(person(handle="anyone.bsky.social"), INSTS, roster_ids={1})
    assert m.confidence == ROSTER
    assert m.method == "roster"


def test_bouie_the_motivating_case():
    """Verified by bsky.app, own-domain handle, NYT only in the bio."""
    m = match_actor(
        person(
            handle="jamellebouie.net",
            verified_status="valid",
            verification_records=[{"issuerHandle": "bsky.app"}],
            description="Columnist for the New York Times Opinion section.",
        ),
        INSTS,
    )
    assert m.name == "The New York Times"
    assert m.confidence == CLAIMED_VERIFIED, "a verified bio claim is trusted further"
    assert m.seniority == 1.0, "columnist is senior"
    assert m.role == "columnist"
    assert m.score == pytest.approx(0.85)


def test_an_unverified_bio_claim_is_discounted():
    m = match_actor(
        person(description="Columnist for the New York Times"), INSTS
    )
    assert m.confidence == CLAIMED_PLAIN
    assert m.score < 0.5


def test_corroboration_lifts_a_bio_claim():
    m = match_actor(
        person(description="Writes for the New York Times"), INSTS, corroborated_ids={1}
    )
    assert m.method == "corroborated"
    assert m.confidence == 0.95


def test_no_match_returns_none():
    assert match_actor(person(description="I like dogs"), INSTS) is None


def test_best_evidence_wins_rather_than_accumulating():
    """Attested at NYT and a bio mentioning Wired: one match, the strongest."""
    m = match_actor(
        person(
            handle="x.bsky.social",
            verification_records=[{"issuerHandle": "nytimes.com"}],
            description="formerly WIRED",
        ),
        INSTS,
    )
    assert m.name == "The New York Times"
    assert m.confidence == ATTESTED


def test_alias_matching_respects_word_boundaries():
    """'AP' must not match inside 'apple'."""
    ap = [{"id": 3, "name": "Associated Press", "weight": 1.0,
           "domains": ["ap.org"], "aliases": ["AP"]}]
    assert match_actor(person(description="I write about apples"), ap) is None
    assert match_actor(person(description="Reporter at AP"), ap) is not None


# ------------------------------------------------------------ seniority

@pytest.mark.parametrize(
    "bio,expected",
    [
        ("Columnist at the NYT", 1.0),
        ("Op-ed writer", 1.0),
        ("Staff reporter", 0.85),
        ("Summer intern", 0.6),
        ("Freelance contributor", 0.6),
        ("Just some guy", 0.85),
        (None, 0.85),
    ],
)
def test_seniority_tiers(bio, expected):
    assert seniority_of(bio)[0] == expected


def test_columnist_outranks_intern_at_the_same_masthead():
    columnist = match_actor(
        person(verified_status="valid", description="NYT columnist"), INSTS
    )
    intern = match_actor(
        person(verified_status="valid", description="NYT intern"), INSTS
    )
    assert columnist.score > intern.score


def test_absent_job_title_is_not_treated_as_junior():
    titled = match_actor(person(verified_status="valid", description="NYT reporter"), INSTS)
    untitled = match_actor(person(verified_status="valid", description="At the NYT"), INSTS)
    assert untitled.seniority == titled.seniority == 0.85


# ---------------------------------------------------- store integration

@pytest.fixture
async def db(tmp_path):
    store.set_db_path(str(tmp_path / "inst.db"))
    await store.connect()
    yield store
    await store.close()
    store.set_db_path(None)


async def test_seeding_is_idempotent(db):
    first = await store.seed_institutions()
    second = await store.seed_institutions()
    assert first == len(inst.SEED_INSTITUTIONS)
    assert second == 0, "re-seeding must not duplicate or overwrite edits"


async def test_issuers_are_discovered_from_sweep_data(db):
    """The table grows itself as Bluesky's verifier programme expands."""
    await store.upsert_actor(verified("did:plc:a", issuer="thebulwark.com"))
    await store.mark_seen("did:plc:a", 0)

    added = await store.discover_institutions_from_issuers()
    names = [i["name"] for i in await store.all_institutions()]

    assert any("thebulwark" in n.lower() or n in added for n in names)


async def test_bluesky_itself_is_not_an_institution(db):
    await store.upsert_actor(verified("did:plc:b", issuer="bsky.app"))
    await store.mark_seen("did:plc:b", 0)

    await store.discover_institutions_from_issuers()
    domains = [d for i in await store.all_institutions() for d in i["domains"]]

    assert "bsky.app" not in domains, "verification by Bluesky says nothing about employer"


async def test_matches_are_applied_and_summarised(db):
    await store.seed_institutions()
    profile = verified("did:plc:c", issuer="bsky.app")
    profile["description"] = "Columnist for the New York Times."
    await store.upsert_actor(profile)
    await store.mark_seen("did:plc:c", 0)

    result = await store.apply_institution_matches()

    assert result["matched"] == 1
    assert result["by_method"] == {"claimed (verified)": 1}
    detail = await store.follower_detail("did:plc:c")
    assert detail["institution_name"] == "The New York Times"
    assert detail["institution_role"] == "columnist"


async def test_unmatched_actors_are_cleared_not_left_stale(db):
    await store.seed_institutions()
    await store.upsert_actor(actor("did:plc:d", description="I like dogs"))
    await store.mark_seen("did:plc:d", 0)

    await store.apply_institution_matches()

    detail = await store.follower_detail("did:plc:d")
    assert detail["institution_score"] is None


# ------------------------------ bio claims need employment context (6d)
# Found by inspecting the first real run: a bare substring match produced three
# distinct false-positive classes, all common in real bios.

@pytest.mark.parametrize(
    "bio",
    [
        "Software engineer @ Microsoft. Views my own.",
        "Product Design at Microsoft AI",
        "NYT columnist",
        "Reporter for the New York Times",
        "News editor with the BBC",
    ],
)
def test_genuine_employment_claims_still_match(bio):
    msft = [{"id": 9, "name": "Microsoft", "weight": 0.8,
             "domains": ["microsoft.com"], "aliases": ["Microsoft"]}]
    bbc = [{"id": 10, "name": "BBC", "weight": 1.0,
            "domains": ["bbc.co.uk"], "aliases": ["BBC"]}]
    assert match_actor(person(description=bio), INSTS + msft + bbc) is not None


@pytest.mark.parametrize(
    "bio,why",
    [
        ("Ex-Microsoft, Hearst, Advance. Occasional journalist.", "past employment"),
        ("20+ yrs at Apple, Microsoft, Samsung", "past employment"),
        ("Previously senior firestarter at BBC RD", "past employment"),
        ("ex BBC/CNN/DWTV Journo", "past employment"),
        ("NYT bestselling and award-winning writer", "product, not employer"),
        ("My posts are still better than NYT Wordle", "product, not employer"),
        ("Borrowing heavily from the BBC's Genome project", "bare mention"),
        ("I read the New York Times every morning", "bare mention"),
    ],
)
def test_false_positive_classes_are_rejected(bio, why):
    msft = [{"id": 9, "name": "Microsoft", "weight": 0.8,
             "domains": ["microsoft.com"], "aliases": ["Microsoft"]}]
    bbc = [{"id": 10, "name": "BBC", "weight": 1.0,
            "domains": ["bbc.co.uk"], "aliases": ["BBC"]}]
    assert match_actor(person(description=bio), INSTS + msft + bbc) is None, why


def test_a_past_claim_does_not_block_a_present_one():
    """'Ex-Wired, now at the NYT' should match the NYT."""
    m = match_actor(
        person(verified_status="valid", description="Ex-WIRED, now a reporter at the New York Times"),
        INSTS,
    )
    assert m is not None
    assert m.name == "The New York Times"


def test_attested_is_unaffected_by_bio_wording():
    """Cryptographic evidence doesn't care what the bio says."""
    m = match_actor(
        person(handle="x.bsky.social",
               verification_records=[{"issuerHandle": "nytimes.com"}],
               description="NYT bestselling author, ex-everything"),
        INSTS,
    )
    assert m.method == "attested"


@pytest.mark.parametrize(
    "bio",
    [
        "Ex-liontamer, writer/editor. I used to work for Wired, The Verge",
        "Adobe Firefly Ambassador. Avid listener of the BBC",
        "Longtime reader of the New York Times",
    ],
)
def test_more_false_positives_found_in_the_second_pass(bio):
    bbc = [{"id": 10, "name": "BBC", "weight": 1.0,
            "domains": ["bbc.co.uk"], "aliases": ["BBC", "the BBC"]}]
    assert match_actor(person(description=bio), INSTS + bbc) is None
