"""Parsing and persistence of the awkward real cases.

Every fixture shape here was observed in live data on 2026-07-26:
37 followers with handle.invalid, 1,838 carrying !no-unauthenticated, 147
verified, and the overwhelming majority with no `verification` key at all.
"""

import json

import pytest

from sonde.db import store
from tests.fakes import actor, verified


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "store.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


async def _row(did: str) -> dict:
    db = await store._db()
    async with db.execute("SELECT * FROM actors WHERE did = ?", (did,)) as cur:
        return dict(await cur.fetchone())


async def test_unverified_actor_omits_verification_entirely():
    """The common case: the field is ABSENT, not set to "none". Parse defensively."""
    payload = actor("did:plc:plain")
    assert "verification" not in payload

    await store.upsert_actor(payload)
    row = await _row("did:plc:plain")

    assert row["verified_status"] == "none"
    assert row["trusted_verifier_status"] == "none"
    assert json.loads(row["verifications"]) == []


async def test_verified_actor_keeps_its_issuer():
    await store.upsert_actor(verified("did:plc:v", issuer="nytimes.com"))
    row = await _row("did:plc:v")

    assert row["verified_status"] == "valid"
    issuers = [v["issuerHandle"] for v in json.loads(row["verifications"])]
    assert issuers == ["nytimes.com"]


async def test_handle_invalid_is_stored_not_dropped():
    """37 real followers look like this — they are still followers."""
    await store.upsert_actor(actor("did:plc:broken", handle="handle.invalid"))
    assert await store.get_handle("did:plc:broken") == "handle.invalid"


async def test_no_unauthenticated_label_is_preserved():
    await store.upsert_actor(
        actor("did:plc:private", labels=[{"val": "!no-unauthenticated"}])
    )
    row = await _row("did:plc:private")
    assert "!no-unauthenticated" in json.loads(row["labels"])


async def test_private_followers_are_counted_separately():
    await store.upsert_actor(actor("did:plc:p", labels=[{"val": "!no-unauthenticated"}]))
    await store.upsert_actor(actor("did:plc:q"))
    await store.mark_seen("did:plc:p", 0)
    await store.mark_seen("did:plc:q", 1)

    counts = await store.counts()
    assert counts["tracked"] == 2
    assert counts["private"] == 1


async def test_upsert_preserves_hydrated_counts():
    """A profileView sweep must not wipe followersCount written by hydration."""
    await store.upsert_actor(actor("did:plc:h"))
    db = await store._db()
    await db.execute(
        "UPDATE actors SET followers_count = 5000, profile_fetched_at = '2026-07-26' "
        "WHERE did = 'did:plc:h'"
    )

    await store.upsert_actor(actor("did:plc:h", displayName="Renamed"))
    row = await _row("did:plc:h")

    assert row["followers_count"] == 5000, "sweep must not clobber hydrated fields"
    assert row["display_name"] == "Renamed"


async def test_created_at_survives_a_payload_that_omits_it():
    await store.upsert_actor(actor("did:plc:c", createdAt="2023-05-05T00:00:00.000Z"))
    payload = actor("did:plc:c")
    payload.pop("createdAt")
    await store.upsert_actor(payload)

    row = await _row("did:plc:c")
    assert row["account_created_at"] == "2023-05-05T00:00:00.000Z"


async def test_verified_count_only_includes_current_followers():
    await store.upsert_actor(verified("did:plc:v1"))
    await store.upsert_actor(verified("did:plc:v2"))
    await store.mark_seen("did:plc:v1", 0)
    await store.mark_seen("did:plc:v2", 1)
    await store.mark_departed(["did:plc:v2"], reason="unfollow")

    counts = await store.counts()
    assert counts["verified"] == 1
    assert counts["departed"] == 1


async def test_events_are_append_only_and_ordered():
    await store.upsert_actor(actor("did:plc:e"))
    await store.add_event("did:plc:e", "followed")
    await store.add_event("did:plc:e", "departed", reason="gone")
    await store.add_event("did:plc:e", "returned")

    events = await store.events_for("did:plc:e")
    assert [e["event"] for e in events] == ["returned", "departed", "followed"]
    assert events[1]["reason"] == "gone"


async def test_dashboard_reports_both_follower_numbers():
    """Tracked and reported never match; showing one invites a permanent question."""
    await store.upsert_actor(actor("did:plc:a"))
    await store.mark_seen("did:plc:a", 0)
    await store.set_meta("followers_reported", "11451")

    stats = await store.dashboard_stats()
    tracked_tile = stats["tiles"][0]

    assert tracked_tile[1] == "1"
    assert "11,451 reported" in tracked_tile[2]
    assert "unservable" in tracked_tile[2]


async def test_sync_run_lifecycle():
    run_id = await store.start_run("full")
    await store.finish_run(run_id, status="ok", completed=1, actors_seen=42, api_calls=115)

    runs = await store.recent_runs()
    assert runs[0]["kind"] == "full"
    assert runs[0]["actors_seen"] == 42
    assert runs[0]["api_calls"] == 115
    assert (await store.last_sync_summary())["status"] == "ok"


async def test_finish_run_ignores_unknown_columns():
    run_id = await store.start_run("head")
    await store.finish_run(run_id, status="ok", not_a_column="boom")
    assert (await store.recent_runs())[0]["status"] == "ok"
