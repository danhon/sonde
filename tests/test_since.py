""""Since" means since they followed, not since sonde noticed.

These are two different facts and the difference is large: the whole 10,041-row
initial backfill has a `first_seen_at` of the day sonde was first run, while
those people followed at any point over the preceding years. Showing the former
under a "Since" heading understates every follow date in the list, most of them
by a lot.

The exact time comes from the TID in `viewer.followedBy` and needs an
authenticated sweep, so the fallback cannot be removed — but it is a lower
bound, and is labelled as one rather than presented as the answer.
"""

from __future__ import annotations

from pathlib import Path

from sonde.db import store
from tests.fakes import actor

TEMPLATES = Path(__file__).resolve().parents[1] / "sonde" / "web" / "templates"


async def follower(did: str, *, followed_at: str | None = None,
                   seen: str | None = None) -> None:
    await store.upsert_actor(actor(did))
    await store.mark_seen(did, 0)
    db = await store._db()
    if followed_at:
        await db.execute(
            "UPDATE follower_state SET followed_at = ? WHERE did = ?",
            (followed_at, did))
    if seen:
        await db.execute(
            "UPDATE follower_state SET first_seen_at = ? WHERE did = ?",
            (seen, did))
    await db.commit()


async def test_since_prefers_the_follow_date(db):
    await follower("did:plc:a", followed_at="2023-04-01T09:00:00Z",
                   seen="2026-07-26T00:00:00Z")

    row = (await store.ranked_followers())[0]
    assert row["since"].startswith("2023-04-01")
    assert row["since_exact"] is True


async def test_since_falls_back_but_says_so(db):
    await follower("did:plc:a", seen="2026-07-26T00:00:00Z")

    row = (await store.ranked_followers())[0]
    assert row["since"].startswith("2026-07-26")
    assert row["since_exact"] is False, (
        "a first-seen date must never be presented as an exact follow date"
    )


async def test_the_detail_page_agrees_with_the_list(db):
    await follower("did:plc:a", followed_at="2023-04-01T09:00:00Z",
                   seen="2026-07-26T00:00:00Z")

    listed = (await store.ranked_followers())[0]
    detail = await store.follower_detail("did:plc:a")
    assert detail["since"] == listed["since"]
    assert detail["since_exact"] == listed["since_exact"]


async def test_sorting_by_since_uses_the_follow_date(db):
    """The bug this would otherwise hide: a 2023 follower sorting as newest.

    Backfilled rows all share one `first_seen_at`, so sorting on it puts
    somebody who followed in 2023 alongside somebody who followed last week.
    """
    await follower("did:plc:old", followed_at="2023-01-01T00:00:00Z",
                   seen="2026-07-26T00:00:00Z")
    await follower("did:plc:new", followed_at="2026-07-01T00:00:00Z",
                   seen="2026-07-26T00:00:00Z")

    newest = await store.ranked_followers(order="since", direction="desc")
    assert [r["did"] for r in newest] == ["did:plc:new", "did:plc:old"]

    oldest = await store.ranked_followers(order="since", direction="asc")
    assert [r["did"] for r in oldest] == ["did:plc:old", "did:plc:new"]


async def test_sorting_mixes_exact_and_fallback_dates_sensibly(db):
    """A known 2023 follow is older than an unknown one first seen in 2026."""
    await follower("did:plc:known", followed_at="2023-01-01T00:00:00Z",
                   seen="2026-07-26T00:00:00Z")
    await follower("did:plc:unknown", seen="2026-07-26T00:00:00Z")

    oldest = await store.ranked_followers(order="since", direction="asc")
    assert oldest[0]["did"] == "did:plc:known"


async def test_the_export_keeps_both_dates(db):
    """Conflating them in an export would be wrong permanently and silently."""
    await follower("did:plc:a", followed_at="2023-04-01T09:00:00Z",
                   seen="2026-07-26T00:00:00Z")

    row = (await store.export_rows())[0]
    assert row["following_since"].startswith("2023-04-01")
    assert row["first_seen_at"].startswith("2026-07-26")
    assert row["following_since_exact"] == 1


def test_the_fallback_is_marked_in_the_markup():
    """A reader must be able to tell the two apart without reading the code."""
    macro = (TEMPLATES / "_sort.html").read_text()
    assert "since_exact" in macro, "the macro no longer distinguishes the two"
    assert "≥" in macro, "the fallback is rendered as though it were exact"


def test_the_followers_table_uses_the_macro():
    source = (TEMPLATES / "followers.html").read_text()
    assert "since(row)" in source
    assert "first_seen_at" not in source, (
        "the Since column is showing first_seen_at again"
    )
