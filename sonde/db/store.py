"""aiosqlite read/write helpers.

One module-level connection, WAL mode, opened lazily. SQLite handles a single
writer plus concurrent readers fine at this scale (~10k rows).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

from sonde.config import settings

_conn: aiosqlite.Connection | None = None
_db_path: str | None = None
_override_path: str | None = None

SCHEMA = Path(__file__).parent / "schema.sql"


def set_db_path(path: str | None) -> None:
    """Point the store at a different file. Used by tests; Settings is frozen."""
    global _override_path
    _override_path = path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def connect(path: str | None = None) -> aiosqlite.Connection:
    """Open (once) and migrate the database."""
    global _conn, _db_path
    target = path or _override_path or settings.db_path
    if _conn is not None and _db_path == target:
        return _conn
    if _conn is not None:
        await _conn.close()
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(target)
    _conn.row_factory = aiosqlite.Row
    _db_path = target
    await _conn.executescript(SCHEMA.read_text())
    await _conn.commit()
    return _conn


async def close() -> None:
    global _conn, _db_path
    if _conn is not None:
        await _conn.close()
    _conn, _db_path = None, None


async def _db() -> aiosqlite.Connection:
    # connect() is a no-op when the target file is already open.
    return await connect()


# ---------------------------------------------------------------- actors

def _labels_of(profile: dict) -> str:
    return json.dumps([lbl.get("val") for lbl in profile.get("labels") or []])


async def upsert_actor(profile: dict) -> None:
    """Insert or refresh an actor from a profileView (follower list) payload.

    Only touches the fields a profileView actually carries — follower counts
    live on profileViewDetailed and are written by the hydration pass instead.
    Existing values are preserved when the incoming payload omits a field.
    """
    db = await _db()
    verification = profile.get("verification") or {}
    now = utcnow()
    await db.execute(
        """
        INSERT INTO actors (did, handle, display_name, avatar_url, description,
                            account_created_at, labels, verified_status,
                            trusted_verifier_status, verifications, first_indexed_at)
        VALUES (:did, :handle, :display_name, :avatar_url, :description,
                :created_at, :labels, :vstatus, :tvstatus, :verifications, :now)
        ON CONFLICT (did) DO UPDATE SET
            handle                  = excluded.handle,
            display_name            = excluded.display_name,
            avatar_url              = excluded.avatar_url,
            description             = excluded.description,
            account_created_at      = COALESCE(excluded.account_created_at, actors.account_created_at),
            labels                  = excluded.labels,
            verified_status         = excluded.verified_status,
            trusted_verifier_status = excluded.trusted_verifier_status,
            verifications           = excluded.verifications
        """,
        {
            "did": profile["did"],
            "handle": profile.get("handle"),
            "display_name": profile.get("displayName"),
            "avatar_url": profile.get("avatar"),
            "description": profile.get("description"),
            "created_at": profile.get("createdAt"),
            "labels": _labels_of(profile),
            "vstatus": verification.get("verifiedStatus", "none"),
            "tvstatus": verification.get("trustedVerifierStatus", "none"),
            "verifications": json.dumps(verification.get("verifications") or []),
            "now": now,
        },
    )


async def get_handle(did: str) -> str | None:
    db = await _db()
    async with db.execute("SELECT handle FROM actors WHERE did = ?", (did,)) as cur:
        row = await cur.fetchone()
    return row["handle"] if row else None


async def known_dids() -> set[str]:
    db = await _db()
    async with db.execute("SELECT did FROM follower_state WHERE is_current = 1") as cur:
        return {r["did"] for r in await cur.fetchall()}


# --------------------------------------------------------- follower state

async def mark_seen(did: str, rank: int, *, backfilled: bool = False) -> bool:
    """Record that `did` currently follows us. Returns True if this is an arrival.

    `missed_sweeps` resets to 0 on *any* sighting, including a head sweep — that
    reset is what stops a transient page skip from accumulating toward departure.
    """
    db = await _db()
    now = utcnow()
    async with db.execute(
        "SELECT is_current, lost_at FROM follower_state WHERE did = ?", (did,)
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        await db.execute(
            """INSERT INTO follower_state
               (did, is_current, backfilled, first_seen_at, last_seen_at, missed_sweeps, list_rank)
               VALUES (?, 1, ?, ?, ?, 0, ?)""",
            (did, 1 if backfilled else 0, now, now, rank),
        )
        return True

    returning = not row["is_current"]
    await db.execute(
        """UPDATE follower_state
              SET is_current = 1, last_seen_at = ?, missed_sweeps = 0,
                  lost_at = NULL, list_rank = ?
            WHERE did = ?""",
        (now, rank, did),
    )
    return returning


async def bump_missed(dids: Iterable[str]) -> list[str]:
    """Increment missed_sweeps for absent followers; return those now past the threshold."""
    db = await _db()
    dids = list(dids)
    if not dids:
        return []
    await db.executemany(
        "UPDATE follower_state SET missed_sweeps = missed_sweeps + 1 WHERE did = ?",
        [(d,) for d in dids],
    )
    async with db.execute(
        "SELECT did FROM follower_state WHERE is_current = 1 AND missed_sweeps >= ?",
        (settings.departure_confirm_sweeps,),
    ) as cur:
        return [r["did"] for r in await cur.fetchall()]


async def pending_departures() -> list[str]:
    """Followers already past the miss threshold, without incrementing anything.

    Used by the operator override on a halted sweep — re-incrementing there
    would penalise people a second time for the same absence.
    """
    db = await _db()
    async with db.execute(
        "SELECT did FROM follower_state WHERE is_current = 1 AND missed_sweeps >= ?",
        (settings.departure_confirm_sweeps,),
    ) as cur:
        return [r["did"] for r in await cur.fetchall()]


async def mark_departed(dids: Sequence[str], reason: str) -> None:
    db = await _db()
    now = utcnow()
    await db.executemany(
        "UPDATE follower_state SET is_current = 0, lost_at = ? WHERE did = ?",
        [(now, d) for d in dids],
    )
    await db.executemany(
        """INSERT INTO follow_events (did, event, reason, detected_at)
           VALUES (?, 'departed', ?, ?)""",
        [(d, reason, now) for d in dids],
    )


async def add_event(did: str, event: str, reason: str | None = None, detail: str | None = None) -> None:
    db = await _db()
    await db.execute(
        "INSERT INTO follow_events (did, event, reason, detail, detected_at) VALUES (?,?,?,?,?)",
        (did, event, reason, detail, utcnow()),
    )


async def events_for(did: str) -> list[dict]:
    db = await _db()
    async with db.execute(
        "SELECT * FROM follow_events WHERE did = ? ORDER BY detected_at DESC, id DESC", (did,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ------------------------------------------------------------- sync runs

async def start_run(kind: str) -> int:
    db = await _db()
    cur = await db.execute(
        "INSERT INTO sync_runs (kind, started_at) VALUES (?, ?)", (kind, utcnow())
    )
    await db.commit()
    return int(cur.lastrowid)


async def finish_run(run_id: int, **fields: Any) -> None:
    db = await _db()
    fields.setdefault("ended_at", utcnow())
    allowed = {
        "ended_at", "completed", "status", "pages_fetched", "actors_seen",
        "new_followers", "lost_followers", "profiles_hydrated", "api_calls", "error",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k in sets)
    await db.execute(
        f"UPDATE sync_runs SET {clause} WHERE id = ?", (*sets.values(), run_id)
    )
    await db.commit()


async def recent_runs(limit: int = 8) -> list[dict]:
    db = await _db()
    async with db.execute(
        "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def last_sync_summary() -> dict | None:
    db = await _db()
    async with db.execute(
        "SELECT kind, started_at, status, actors_seen FROM sync_runs "
        "WHERE status != 'running' ORDER BY id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ meta

async def set_meta(key: str, value: str) -> None:
    db = await _db()
    await db.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


async def get_meta(key: str) -> str | None:
    db = await _db()
    async with db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else None


# ------------------------------------------------------------ dashboard

async def _scalar(sql: str, params: tuple = ()) -> int:
    db = await _db()
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return int(row[0] or 0)


async def counts() -> dict:
    tracked = await _scalar("SELECT COUNT(*) FROM follower_state WHERE is_current = 1")
    verified = await _scalar(
        "SELECT COUNT(*) FROM follower_state fs JOIN actors a USING (did) "
        "WHERE fs.is_current = 1 AND a.verified_status = 'valid'"
    )
    private = await _scalar(
        "SELECT COUNT(*) FROM follower_state fs JOIN actors a USING (did) "
        "WHERE fs.is_current = 1 AND a.labels LIKE '%no-unauthenticated%'"
    )
    departed = await _scalar("SELECT COUNT(*) FROM follower_state WHERE is_current = 0")
    reported = await get_meta("followers_reported")
    return {
        "tracked": tracked,
        "verified": verified,
        "private": private,
        "departed": departed,
        "reported": int(reported) if reported else None,
    }


async def dashboard_stats() -> dict:
    c = await counts()
    runs = await recent_runs()
    reported = c["reported"]
    gap_note = None
    if reported:
        gap = reported - c["tracked"]
        gap_note = f"{reported:,} reported by Bluesky · {gap:,} unservable"
    tiles = [
        ("Followers tracked", f"{c['tracked']:,}", gap_note),
        ("Verified", f"{c['verified']:,}", None),
        ("Private", f"{c['private']:,}", "hidden when logged out"),
        ("Departed", f"{c['departed']:,}", None),
    ]
    needs_review = await get_meta("needs_review_count")
    return {
        "tiles": tiles,
        "counts": c,
        "recent_syncs": runs,
        "empty": c["tracked"] == 0,
        "needs_review": int(needs_review) if needs_review else None,
    }
