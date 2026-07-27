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
    """Millisecond precision on purpose.

    A full sweep imports ~10,000 followers in under 40 seconds, so second
    precision collides heavily and "newest arrival first" stops meaning
    anything. SQLite's julianday() parses the fractional form fine.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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


async def commit() -> None:
    db = await _db()
    await db.commit()


# ------------------------------------------------------- tier 1 hydration

async def stale_actor_dids(limit: int | None = None) -> list[str]:
    """Current followers needing a profile refresh, newest arrivals first.

    Never-hydrated actors sort ahead of merely-stale ones, and actors already
    known to be unservable are skipped — re-requesting them every cycle burns
    calls to learn nothing new.
    """
    db = await _db()
    sql = """
        SELECT a.did
          FROM actors a
          JOIN follower_state fs USING (did)
         WHERE fs.is_current = 1
           AND a.unservable_since IS NULL
           AND (a.profile_fetched_at IS NULL
                OR julianday('now') - julianday(a.profile_fetched_at) >= ?)
         ORDER BY (a.profile_fetched_at IS NOT NULL),
                  COALESCE(fs.list_rank, 2147483647) ASC,
                  fs.first_seen_at DESC
    """
    # list_rank, not first_seen_at, is the authoritative recency signal: it is
    # the live position in a newest-follow-first list, rewritten on every
    # sighting, so rank 0 is always the most recent follower. Timestamps can't
    # do this job — a sweep imports 10,000 rows in 38 seconds and they collide.
    params: list = [settings.profile_ttl_days]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    async with db.execute(sql, params) as cur:
        return [r["did"] for r in await cur.fetchall()]


async def apply_detailed_profile(did: str, profile: dict) -> None:
    """Write profileViewDetailed fields, then rescore that actor."""
    db = await _db()
    verification = profile.get("verification") or {}
    await db.execute(
        """UPDATE actors
              SET handle             = COALESCE(?, handle),
                  display_name       = ?,
                  description        = ?,
                  avatar_url         = ?,
                  followers_count    = ?,
                  follows_count      = ?,
                  posts_count        = ?,
                  verified_status    = ?,
                  trusted_verifier_status = ?,
                  verifications      = ?,
                  labels             = ?,
                  account_created_at = COALESCE(?, account_created_at),
                  profile_fetched_at = ?,
                  unservable_since   = NULL
            WHERE did = ?""",
        (
            profile.get("handle"),
            profile.get("displayName"),
            profile.get("description"),
            profile.get("avatar"),
            profile.get("followersCount"),
            profile.get("followsCount"),
            profile.get("postsCount"),
            verification.get("verifiedStatus", "none"),
            verification.get("trustedVerifierStatus", "none"),
            json.dumps(verification.get("verifications") or []),
            _labels_of(profile),
            profile.get("createdAt"),
            utcnow(),
            did,
        ),
    )
    await _rescore_did(did)


async def mark_unservable(dids: Sequence[str]) -> None:
    """Requested from getProfiles but not returned — the 'gone' marker."""
    db = await _db()
    now = utcnow()
    await db.executemany(
        "UPDATE actors SET unservable_since = COALESCE(unservable_since, ?) WHERE did = ?",
        [(now, d) for d in dids],
    )


# ------------------------------------------------------------- scoring

async def active_score_components() -> set[str]:
    """Which score components the corpus actually has data for."""
    from sonde import scoring

    db = await _db()
    coverage: dict[str, int] = {}
    probes = {
        "reach": "followers_count IS NOT NULL",
        "selectivity": "followers_count IS NOT NULL AND follows_count IS NOT NULL",
        "liveness": "last_post_at IS NOT NULL OR posts_count IS NOT NULL",
        "affinity": "affinity_sampled IS NOT NULL OR affinity_exact IS NOT NULL",
    }
    for key, predicate in probes.items():
        async with db.execute(f"SELECT COUNT(*) FROM actors WHERE {predicate}") as cur:
            coverage[key] = int((await cur.fetchone())[0] or 0)
    return scoring.active_components(coverage)


async def _rescore_did(did: str, active: set[str] | None = None) -> None:
    from sonde import scoring

    db = await _db()
    async with db.execute("SELECT * FROM actors WHERE did = ?", (did,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    score = scoring.score_actor(dict(row), active=active)
    await db.execute(
        "UPDATE actors SET influence_score = ?, score_components = ? WHERE did = ?",
        (score.normalised, score.as_json(), did),
    )


async def rescore_all() -> int:
    from sonde import scoring

    db = await _db()
    active = await active_score_components()
    async with db.execute(
        "SELECT a.* FROM actors a JOIN follower_state fs USING (did) WHERE fs.is_current = 1"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    updates = []
    for row in rows:
        score = scoring.score_actor(row, active=active)
        updates.append((score.normalised, score.as_json(), row["did"]))
    await db.executemany(
        "UPDATE actors SET influence_score = ?, score_components = ? WHERE did = ?", updates
    )
    return len(updates)


async def ranked_followers(
    limit: int = 50, offset: int = 0, *, order: str = "influence",
    verified_only: bool = False, min_followers: int | None = None,
    query: str | None = None,
) -> list[dict]:
    order_sql = {
        "influence": "a.influence_score DESC NULLS LAST, a.followers_count DESC",
        "followers": "a.followers_count DESC NULLS LAST",
        "recent": "fs.first_seen_at DESC",
        "handle": "a.handle ASC",
    }.get(order, "a.influence_score DESC NULLS LAST")

    where = ["fs.is_current = 1"]
    params: list = []
    if verified_only:
        where.append("a.verified_status = 'valid'")
    if min_followers is not None:
        where.append("COALESCE(a.followers_count, 0) >= ?")
        params.append(min_followers)
    if query:
        where.append(
            "(a.handle LIKE ? OR COALESCE(a.display_name,'') LIKE ? "
            "OR COALESCE(a.description,'') LIKE ?)"
        )
        params += [f"%{query}%"] * 3
    if settings.respect_no_unauthenticated:
        where.append("COALESCE(a.labels,'') NOT LIKE '%no-unauthenticated%'")

    db = await _db()
    sql = (
        "SELECT a.*, fs.first_seen_at, fs.list_rank FROM actors a "
        "JOIN follower_state fs USING (did) "
        f"WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ? OFFSET ?"
    )
    async with db.execute(sql, (*params, limit, offset)) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for row in rows:
        row["components"] = json.loads(row["score_components"] or "{}").get("components", [])
        row["is_private"] = "no-unauthenticated" in (row.get("labels") or "")
    return rows


async def hydration_progress() -> dict:
    total = await _scalar("SELECT COUNT(*) FROM follower_state WHERE is_current = 1")
    done = await _scalar(
        "SELECT COUNT(*) FROM actors a JOIN follower_state fs USING (did) "
        "WHERE fs.is_current = 1 AND a.profile_fetched_at IS NOT NULL"
    )
    unservable = await _scalar(
        "SELECT COUNT(*) FROM actors a JOIN follower_state fs USING (did) "
        "WHERE fs.is_current = 1 AND a.unservable_since IS NOT NULL"
    )
    return {
        "total": total, "hydrated": done, "unservable": unservable,
        "pct": round(done / total * 100, 1) if total else 0.0,
    }


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


async def verified_by_issuer() -> list[dict]:
    """Verified followers grouped by who vouched for them.

    Measured 2026-07-26: 147 verified, of which only 14 (10%) were verified by
    an institution — the other 134 by Bluesky itself, which says nothing about
    where someone works. That distribution is why the Institution component of
    the score can't lean on this alone.
    """
    db = await _db()
    async with db.execute(
        """SELECT a.did, a.handle, a.display_name, a.avatar_url, a.description,
                  a.verifications, a.trusted_verifier_status, a.followers_count
             FROM actors a JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND a.verified_status = 'valid'
            ORDER BY COALESCE(a.followers_count, 0) DESC, a.handle"""
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    groups: dict[str, dict] = {}
    for row in rows:
        records = json.loads(row["verifications"] or "[]")
        row["records"] = records
        if not records:
            # verifiedStatus is valid but no trusted-verifier record is exposed.
            key, label = "unknown", "Issuer not disclosed"
            groups.setdefault(key, {"issuer": label, "is_bluesky": False, "followers": []})
            groups[key]["followers"].append(row)
            continue
        for rec in records:
            key = rec.get("issuerHandle") or rec.get("issuer") or "unknown"
            grp = groups.setdefault(
                key,
                {
                    "issuer": key,
                    "issuer_name": rec.get("issuerDisplayName"),
                    "is_bluesky": key == "bsky.app",
                    "followers": [],
                },
            )
            grp["followers"].append({**row, "issued_at": rec.get("createdAt")})

    out = sorted(groups.values(), key=lambda g: (g.get("is_bluesky", False), -len(g["followers"])))
    for g in out:
        g["count"] = len(g["followers"])
    return out


async def verified_summary() -> dict:
    groups = await verified_by_issuer()
    institutional = [g for g in groups if not g.get("is_bluesky") and g["issuer"] != "unknown"]
    total = await _scalar(
        "SELECT COUNT(*) FROM follower_state fs JOIN actors a USING (did) "
        "WHERE fs.is_current = 1 AND a.verified_status = 'valid'"
    )
    trusted = await _scalar(
        "SELECT COUNT(*) FROM follower_state fs JOIN actors a USING (did) "
        "WHERE fs.is_current = 1 AND a.trusted_verifier_status = 'valid'"
    )
    # A record exists but fails validation — not the same as unverified.
    invalid = await _scalar(
        "SELECT COUNT(*) FROM follower_state fs JOIN actors a USING (did) "
        "WHERE fs.is_current = 1 AND a.verified_status = 'invalid'"
    )
    return {
        "groups": groups,
        "total": total,
        "trusted_verifiers": trusted,
        "invalid": invalid,
        "institutional": sum(g["count"] for g in institutional),
        "issuer_count": len(groups),
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
