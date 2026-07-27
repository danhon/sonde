"""Snapshots and the daily rollup.

follow_events is the only table that cannot be re-fetched from Bluesky, and
Docker volumes on ubuntuplex are not backed up. These tests cover the mechanism
that gets history off the box.
"""

import sqlite3

import pytest

from sonde.db import store
from sonde.sync import backup
from tests.fakes import actor, verified


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "live.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


async def seed(n: int = 3) -> None:
    for i in range(n):
        did = f"did:plc:b{i}"
        await store.upsert_actor(actor(did))
        await store.mark_seen(did, i)
        await store.add_event(did, "followed")
    await store.commit()


async def test_snapshot_is_readable_while_the_app_writes():
    """VACUUM INTO, not a file copy — the app keeps running mid-backup."""
    await seed(5)
    out = tmp = None

    result = await backup.snapshot(backup_dir=str(_dir()), keep=14)
    # Keep writing after the snapshot; the copy must be unaffected.
    await store.upsert_actor(actor("did:plc:after"))
    await store.commit()

    conn = sqlite3.connect(result["path"])
    rows = conn.execute("SELECT COUNT(*) FROM follow_events").fetchone()[0]
    actors = conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0]
    conn.close()

    assert rows == 5, "the irreplaceable table must be in the snapshot"
    assert actors == 5, "snapshot is a point-in-time copy, not a live view"


_DIR = {}


def _dir():
    return _DIR["path"]


@pytest.fixture(autouse=True)
def _backup_dir(tmp_path):
    _DIR["path"] = tmp_path / "backups"
    return _DIR["path"]


async def test_snapshot_preserves_events_exactly():
    await seed(2)
    await store.add_event("did:plc:b0", "departed", reason="gone")
    await store.commit()

    result = await backup.snapshot(backup_dir=str(_dir()))
    conn = sqlite3.connect(result["path"])
    events = conn.execute(
        "SELECT event, reason FROM follow_events ORDER BY id"
    ).fetchall()
    conn.close()

    assert ("departed", "gone") in events


async def test_retention_keeps_the_newest_n():
    await seed(1)
    directory = _dir()
    directory.mkdir(parents=True, exist_ok=True)
    for day in range(1, 21):
        (directory / f"sonde-2026-06-{day:02d}.db").write_bytes(b"old")

    result = await backup.snapshot(backup_dir=str(directory), keep=14)

    remaining = sorted(p.name for p in directory.glob("sonde-*.db"))
    assert len(remaining) == 14
    assert result["pruned"] == 7  # 20 stubs + today's = 21, keep 14
    assert remaining[-1].endswith(f"{result['path'].split('sonde-')[-1]}")


async def test_retention_ignores_unrelated_files():
    await seed(1)
    directory = _dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "notes.txt").write_text("keep me")
    (directory / "sonde-backup-manual.db").write_bytes(b"keep me too")

    await backup.snapshot(backup_dir=str(directory), keep=1)

    assert (directory / "notes.txt").exists()
    assert (directory / "sonde-backup-manual.db").exists(), "only dated snapshots are pruned"


async def test_same_day_snapshot_overwrites_cleanly():
    """VACUUM INTO refuses an existing target, so the old one is removed first."""
    await seed(1)
    first = await backup.snapshot(backup_dir=str(_dir()))
    await store.upsert_actor(actor("did:plc:later"))
    await store.mark_seen("did:plc:later", 9)
    await store.commit()
    second = await backup.snapshot(backup_dir=str(_dir()))

    assert first["path"] == second["path"]
    conn = sqlite3.connect(second["path"])
    assert conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0] == 2
    conn.close()


async def test_backup_records_its_own_run_and_timestamp():
    await seed(1)
    await backup.snapshot(backup_dir=str(_dir()))

    assert (await store.recent_runs())[0]["kind"] == "backup"
    last = await backup.last_backup()
    assert last is not None
    assert last["age_hours"] is not None and last["age_hours"] < 1


# ------------------------------------------------------- daily rollup

async def test_daily_snapshot_records_both_follower_numbers():
    await seed(3)
    await store.set_meta("followers_reported", "11451")

    row = await store.record_daily_snapshot()
    series = await store.growth_series()

    assert row["tracked"] == 3
    assert series[-1]["followers_tracked"] == 3
    assert series[-1]["followers_reported"] == 11451


async def test_daily_snapshot_counts_todays_events():
    await seed(2)
    await store.add_event("did:plc:b0", "departed", reason="unfollow")
    await store.commit()

    row = await store.record_daily_snapshot()
    assert row["gained"] == 2
    assert row["lost"] == 1


async def test_daily_snapshot_is_idempotent_within_a_day():
    await seed(2)
    await store.record_daily_snapshot()
    await store.upsert_actor(actor("did:plc:extra"))
    await store.mark_seen("did:plc:extra", 5)
    await store.record_daily_snapshot()

    series = await store.growth_series()
    assert len(series) == 1, "one row per day, updated in place"
    assert series[-1]["followers_tracked"] == 3


async def test_growth_series_is_chronological():
    await seed(1)
    db = await store._db()
    for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
        await db.execute(
            "INSERT INTO daily_snapshots (day, followers_tracked) VALUES (?,?)",
            (day, 100),
        )
    await store.commit()

    series = await store.growth_series()
    assert [r["day"] for r in series][:3] == ["2026-07-01", "2026-07-02", "2026-07-03"]


async def test_change_totals_group_by_event():
    await seed(2)
    await store.add_event("did:plc:b0", "departed", reason="gone")
    await store.add_event("did:plc:b1", "returned")
    await store.add_event("did:plc:b1", "handle_changed", detail="a → b")
    await store.commit()

    totals = await store.change_totals()
    assert totals == {"followed": 2, "departed": 1, "returned": 1, "renamed": 1}


async def test_verified_are_visible_in_the_change_feed():
    await store.upsert_actor(verified("did:plc:vv"))
    await store.mark_seen("did:plc:vv", 0)
    await store.add_event("did:plc:vv", "followed")
    await store.commit()

    events = await store.recent_changes()
    assert events[0]["verified_status"] == "valid"
    assert events[0]["handle"] == "vv.bsky.social"
