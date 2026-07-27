"""Nightly database snapshots.

`follow_events` is the only table that cannot be re-fetched from Bluesky, so it
gets a nightly copy.

`VACUUM INTO` rather than a file copy: it takes a consistent snapshot of a live
WAL database without stopping the app.

SCOPE, stated so nobody mistakes it later: this is ON-BOX ONLY. It protects
against DB corruption, a bad migration or an accidental delete — not against
losing the host. Off-box backup was considered and deliberately dropped
(PLAN.md §9); it is not a priority for this app. The bind-mount target just
leaves the files somewhere an external tool could collect them without changes
here.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sonde.config import settings
from sonde.db import store

log = logging.getLogger("sonde.backup")

SNAPSHOT_RE = re.compile(r"^sonde-(\d{4}-\d{2}-\d{2})\.db$")


async def snapshot(backup_dir: str | None = None, *, keep: int | None = None) -> dict:
    target_dir = Path(backup_dir or settings.backup_dir)
    keep = keep if keep is not None else settings.backup_keep

    run_id = await store.start_run("backup")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = target_dir / f"sonde-{day}.db"
        if target.exists():
            target.unlink()  # VACUUM INTO refuses to overwrite

        db = await store._db()  # noqa: SLF001 - single writer by design
        # Parameter binding is not allowed in VACUUM INTO; the path is ours.
        await db.execute(f"VACUUM INTO '{target}'")

        pruned = _prune(target_dir, keep)
        size = target.stat().st_size
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc))
        log.exception("backup failed")
        raise

    await store.finish_run(run_id, status="ok", completed=1)
    await store.set_meta("last_backup_at", store.utcnow())
    await store.set_meta("last_backup_path", str(target))
    await store.commit()
    log.info("wrote %s (%.1f MB), pruned %d old snapshot(s)", target, size / 1e6, pruned)
    return {"status": "ok", "kind": "backup", "path": str(target),
            "bytes": size, "pruned": pruned}


def _prune(directory: Path, keep: int) -> int:
    """Keep the newest `keep` dated snapshots; ignore anything else in there."""
    snapshots = sorted(
        (p for p in directory.glob("sonde-*.db") if SNAPSHOT_RE.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    pruned = 0
    for old in snapshots[keep:]:
        old.unlink()
        pruned += 1
    return pruned


async def last_backup() -> dict | None:
    at = await store.get_meta("last_backup_at")
    if not at:
        return None
    path = await store.get_meta("last_backup_path")
    age_hours = None
    try:
        stamp = datetime.fromisoformat(at)
        age_hours = round(
            (datetime.now(timezone.utc) - stamp).total_seconds() / 3600, 1
        )
    except ValueError:
        pass
    return {"at": at, "path": path, "age_hours": age_hours}
