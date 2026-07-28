"""Institutions — the index, and getting from it to one organisation.

Reported: clicking an organisation "simply leads to the institution index
page". It did not — the detail section rendered *after* the 76-row table, so
the click reloaded at scroll-top showing an identical index with the answer far
below the fold. Nothing tested the detail path at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sonde.db import store
from tests.fakes import actor

TEMPLATE = Path(__file__).resolve().parents[1] / "sonde" / "web" / "templates" \
    / "institutions.html"


async def affiliated(did: str, org: str, *, kind: str = "employment",
                     current: bool = True, ignored: bool = False,
                     score: float = 10.0) -> None:
    await store.upsert_actor(actor(did, followersCount=5_000, followsCount=500))
    await store.mark_seen(did, 0)
    await store.apply_detailed_profile(did, {"followersCount": 5_000,
                                             "followsCount": 500})
    db = await store._db()
    await db.execute("UPDATE actors SET influence_score = ? WHERE did = ?",
                     (score, did))
    await db.execute(
        "INSERT INTO organisations (name, weight) VALUES (?, 0.7) "
        "ON CONFLICT (name) DO NOTHING", (org,))
    async with db.execute("SELECT id FROM organisations WHERE name = ?",
                          (org,)) as cur:
        org_id = (await cur.fetchone())["id"]
    await db.execute(
        "INSERT INTO affiliations (did, org_id, org_name, kind, method, "
        "confidence, first_seen_at) "
        "VALUES (?,?,?,?,'wikidata',0.95,'2026-07-01T00:00:00Z')",
        (did, org_id, org, kind))
    if not current:
        await db.execute(
            "UPDATE follower_state SET is_current = 0 WHERE did = ?", (did,))
    if ignored:
        await db.execute(
            "UPDATE follower_state SET ignored_at = '2026-01-01' WHERE did = ?",
            (did,))
    await db.commit()


# ------------------------------------------------- the template

def test_the_detail_renders_before_the_index_table():
    """The reported bug: the answer was below 76 rows of table."""
    source = TEMPLATE.read_text()
    detail = source.index("{% if detail")
    table = source.index("<table")
    assert detail < table, (
        "the organisation detail renders after the index table again — a click "
        "will look like it did nothing"
    )


def test_the_detail_is_not_a_lookalike_of_the_index():
    """It needs something that says 'you are looking at one organisation'."""
    source = TEMPLATE.read_text()
    assert "detail.found" in source, "no not-found state"
    assert re.search(r"clear|all institutions", source), "no way back to the index"


# ------------------------------------------------- the query

async def test_clicking_an_organisation_returns_its_people(db):
    await affiliated("did:plc:a", "Wikimedia Foundation")
    detail = await store.organisation_members("Wikimedia Foundation")

    assert detail["found"]
    assert [r["did"] for r in detail["current"]] == ["did:plc:a"]


@pytest.mark.parametrize("typed", ["wikimedia foundation", "WIKIMEDIA FOUNDATION",
                                   "Wikimedia Foundation"])
async def test_the_name_is_matched_without_case(db, typed):
    """`?name=eventbrite` returned an empty page against a stored 'Eventbrite'."""
    await affiliated("did:plc:a", "Wikimedia Foundation")
    detail = await store.organisation_members(typed)

    assert detail["found"], f"{typed!r} did not resolve"
    assert len(detail["current"]) == 1
    # The stored capitalisation is what gets shown, not what was typed.
    assert detail["organisation"]["name"] == "Wikimedia Foundation"


async def test_an_unknown_name_says_so_rather_than_looking_empty(db):
    await affiliated("did:plc:a", "Wikimedia Foundation")
    detail = await store.organisation_members("Not A Real Org")

    assert not detail["found"]
    assert detail["requested"] == "Not A Real Org"
    assert detail["current"] == [] and detail["former"] == []


async def test_former_affiliations_are_kept_apart(db):
    await affiliated("did:plc:now", "Wired")
    await affiliated("did:plc:was", "Wired", kind="former")
    detail = await store.organisation_members("Wired")

    assert [r["did"] for r in detail["current"]] == ["did:plc:now"]
    assert [r["did"] for r in detail["former"]] == ["did:plc:was"]


# ------------------------------------------------- the counts

async def test_departed_followers_are_not_counted(db):
    """"The numbers seem off" — a roster padded by people who left."""
    await affiliated("did:plc:here", "Wired")
    await affiliated("did:plc:gone", "Wired", current=False)

    summary = {o["name"]: o for o in await store.organisation_summary()}
    assert summary["Wired"]["members"] == 1
    assert [r["did"] for r in
            (await store.organisation_members("Wired"))["current"]] == ["did:plc:here"]


async def test_hidden_followers_are_not_counted(db):
    await affiliated("did:plc:here", "Wired")
    await affiliated("did:plc:hidden", "Wired", ignored=True)

    summary = {o["name"]: o for o in await store.organisation_summary()}
    assert summary["Wired"]["members"] == 1


async def test_the_index_and_the_detail_agree(db):
    """The index counts by org_id, the detail looks up by name. They must match.

    Two different keys for the same question is how a row can show "6 people"
    and its page show none.
    """
    await affiliated("did:plc:a", "Wired")
    await affiliated("did:plc:b", "Wired")
    await affiliated("did:plc:c", "Wired", kind="former")
    await affiliated("did:plc:d", "Wired", current=False)

    for org in await store.organisation_summary():
        detail = await store.organisation_members(org["name"])
        assert org["members"] == len(detail["current"]), org["name"]
        assert org["former"] == len(detail["former"]), org["name"]
