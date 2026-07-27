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
    await _migrate(_conn)
    await _conn.commit()
    return _conn


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a no-op
# on an existing table, so new columns have to be added explicitly or an upgraded
# deploy fails on the first query against a live database.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "follower_state": [
        # Ignoring is a display preference: never a delete, always reversible.
        ("ignored_at", "TEXT"),
        ("ignored_reason", "TEXT"),     # "manual" or "moderation"
        # Set when a human decides either way. Automation must never overrule
        # it: an account you un-hid stays un-hidden on the next moderation run.
        ("ignore_locked", "INTEGER"),
        # Exact follow time, decoded from the TID in viewer.followedBy.
        ("followed_at", "TEXT"),
        ("follow_uri", "TEXT"),
    ],
    "actors": [
        ("posts_fetched_at", "TEXT"),
        ("institution_id", "INTEGER"),
        ("institution_name", "TEXT"),
        ("institution_score", "REAL"),
        ("institution_confidence", "REAL"),
        ("institution_method", "TEXT"),
        ("institution_role", "TEXT"),
        ("verified_affinity", "INTEGER"),
        ("wikidata_id", "TEXT"),
        ("wikidata_sitelinks", "INTEGER"),
        ("wikipedia_title", "TEXT"),
        ("wikipedia_views_30d", "INTEGER"),
        ("external_fetched_at", "TEXT"),
        ("wikidata_occupations", "TEXT"),
        ("wikidata_employers", "TEXT"),
        ("wikidata_positions", "TEXT"),
        # Its own clock. Sharing external_fetched_at with the Wikidata join
        # meant the join always looked "fresh", so pageviews never ran.
        ("pageviews_fetched_at", "TEXT"),
        ("link_signals", "TEXT"),      # JSON, derived from the bio — no calls
        ("wikidata_past_employers", "TEXT"),
    ],
}


async def _migrate(conn: aiosqlite.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        async with conn.execute(f"PRAGMA table_info({table})") as cur:
            existing = {r[1] for r in await cur.fetchall()}
        for name, decl in columns:
            if name not in existing:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


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


async def subject_did() -> str | None:
    """Our own DID, cached from the reported-count refresh."""
    return await get_meta("subject_did")


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


# ------------------------------------------------------- M9 ignore

async def set_ignored(did: str, ignored: bool, *, reason: str = "manual",
                      lock: bool = True) -> None:
    """Hide from listings. Never a delete — the record and history are kept,
    the account is still swept, and it still counts in totals.

    `lock` marks the decision as a human's. Automation passes lock=False and
    skips locked rows, so un-hiding a false positive sticks.
    """
    db = await _db()
    await db.execute(
        "UPDATE follower_state SET ignored_at = ?, ignored_reason = ?, "
        "ignore_locked = COALESCE(?, ignore_locked) WHERE did = ?",
        (utcnow() if ignored else None, reason if ignored else None,
         1 if lock else None, did),
    )
    await db.commit()


async def apply_moderation_hides() -> dict:
    """Hide followers on any enabled moderation list.

    Skips anyone whose visibility a human has decided — in either direction.
    Also un-hides accounts that were auto-hidden and have since dropped off
    every list, so a curator's correction propagates.
    """
    db = await _db()
    async with db.execute(
        """SELECT DISTINCT m.did FROM moderation_list_members m
             JOIN moderation_lists l ON l.uri = m.list_uri
            WHERE l.enabled = 1"""
    ) as cur:
        listed = {r["did"] for r in await cur.fetchall()}

    async with db.execute(
        "SELECT did, ignored_at, ignored_reason, ignore_locked "
        "FROM follower_state WHERE is_current = 1"
    ) as cur:
        current = [dict(r) for r in await cur.fetchall()]

    hidden = restored = skipped = 0
    now = utcnow()
    for row in current:
        if row["ignore_locked"]:
            skipped += 1
            continue
        on_list = row["did"] in listed
        if on_list and not row["ignored_at"]:
            await db.execute(
                "UPDATE follower_state SET ignored_at = ?, ignored_reason = 'moderation' "
                "WHERE did = ?", (now, row["did"]),
            )
            hidden += 1
        elif not on_list and row["ignored_reason"] == "moderation":
            await db.execute(
                "UPDATE follower_state SET ignored_at = NULL, ignored_reason = NULL "
                "WHERE did = ?", (row["did"],),
            )
            restored += 1
    await db.commit()
    return {"hidden": hidden, "restored": restored,
            "locked_skipped": skipped, "listed": len(listed)}


async def save_moderation_list(meta: dict, members: list[str]) -> int:
    db = await _db()
    await db.execute(
        """INSERT INTO moderation_lists
             (uri, curator, name, purpose, description, member_count, fetched_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (uri) DO UPDATE SET
             name = excluded.name, purpose = excluded.purpose,
             description = excluded.description,
             member_count = excluded.member_count, fetched_at = excluded.fetched_at""",
        (meta["uri"], meta["curator"], meta["name"], meta.get("purpose"),
         meta.get("description"), len(members), utcnow()),
    )
    await db.execute(
        "DELETE FROM moderation_list_members WHERE list_uri = ?", (meta["uri"],)
    )
    await db.executemany(
        "INSERT INTO moderation_list_members (list_uri, did) VALUES (?,?) "
        "ON CONFLICT DO NOTHING",
        [(meta["uri"], did) for did in members],
    )
    await db.commit()
    return len(members)


async def moderation_lists() -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT l.*,
                  (SELECT COUNT(*) FROM moderation_list_members m
                    JOIN follower_state fs ON fs.did = m.did AND fs.is_current = 1
                   WHERE m.list_uri = l.uri) AS matched
             FROM moderation_lists l ORDER BY matched DESC, l.name"""
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def set_list_enabled(uri: str, enabled: bool) -> None:
    db = await _db()
    await db.execute(
        "UPDATE moderation_lists SET enabled = ? WHERE uri = ?", (int(enabled), uri)
    )
    await db.commit()


async def lists_matching(did: str) -> list[dict]:
    """Which lists flagged this person — shown on their detail page."""
    db = await _db()
    async with db.execute(
        """SELECT l.name, l.curator, l.uri, l.enabled FROM moderation_list_members m
             JOIN moderation_lists l ON l.uri = m.list_uri
            WHERE m.did = ? ORDER BY l.name""",
        (did,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def ignored_followers() -> list[dict]:
    db = await _db()
    async with db.execute(
        "SELECT a.did, a.handle, a.display_name, a.followers_count, a.influence_score, "
        "fs.ignored_at FROM actors a JOIN follower_state fs USING (did) "
        "WHERE fs.ignored_at IS NOT NULL ORDER BY fs.ignored_at DESC"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def ignored_count() -> int:
    return await _scalar(
        "SELECT COUNT(*) FROM follower_state WHERE is_current = 1 AND ignored_at IS NOT NULL"
    )


async def record_follow_date(did: str, uri: str) -> bool:
    """Store the exact follow time decoded from a viewer.followedBy URI."""
    from sonde.api.tid import from_at_uri

    when = from_at_uri(uri)
    if when is None:
        return False
    db = await _db()
    await db.execute(
        "UPDATE follower_state SET followed_at = ?, follow_uri = ? WHERE did = ?",
        (when.isoformat(timespec="seconds"), uri, did),
    )
    return True


# -------------------------------------------------------- M10 posts

async def replace_posts(did: str, posts: list[dict]) -> int:
    """Keep only the newest few. This is a display signal, not an archive."""
    db = await _db()
    now = utcnow()
    await db.execute("DELETE FROM posts WHERE did = ?", (did,))
    await db.executemany(
        """INSERT INTO posts (did, uri, text, indexed_at, like_count,
                              repost_count, reply_count, is_repost, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT (did, uri) DO NOTHING""",
        [(did, p["uri"], p.get("text"), p.get("indexed_at"), p.get("like_count"),
          p.get("repost_count"), p.get("reply_count"), int(p.get("is_repost", False)), now)
         for p in posts],
    )
    await db.execute(
        "UPDATE actors SET posts_fetched_at = ? WHERE did = ?", (now, did)
    )
    # Real recency retires the lifetime-average liveness proxy.
    own = [p for p in posts if not p.get("is_repost") and p.get("indexed_at")]
    if own:
        await db.execute(
            "UPDATE actors SET last_post_at = ? WHERE did = ?",
            (max(p["indexed_at"] for p in own), did),
        )
    return len(posts)


async def posts_for(did: str) -> list[dict]:
    db = await _db()
    async with db.execute(
        "SELECT * FROM posts WHERE did = ? ORDER BY indexed_at DESC", (did,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def post_targets(ttl_hours: int = 20, limit: int | None = None) -> list[str]:
    """Who gets posts fetched automatically: the top 500 by influence plus every
    verified follower — the same set that gets grouped.

    Everyone else is fetched on demand from their own page. `getAuthorFeed` is
    one call per actor with no bulk equivalent, so covering all 10,042 would be
    ~12,400 calls a day for data nobody had asked to see.
    """
    db = await _db()
    targets = await group_target_dids(top_n=settings.posts_top_n)
    if not targets:
        return []
    placeholders = ",".join("?" for _ in targets)
    async with db.execute(
        f"""SELECT a.did FROM actors a
              JOIN follower_state fs USING (did)
             WHERE a.did IN ({placeholders})
               AND (a.posts_fetched_at IS NULL
                    OR (julianday('now') - julianday(a.posts_fetched_at)) * 24 >= ?)
             ORDER BY (a.verified_status = 'valid') DESC,
                      a.influence_score DESC NULLS LAST""",
        (*targets, ttl_hours),
    ) as cur:
        rows = [r["did"] for r in await cur.fetchall()]
    return rows[:limit] if limit else rows


async def group_target_dids(top_n: int = 500) -> list[str]:
    """Top N by influence plus every verified follower, ignored excluded."""
    db = await _db()
    async with db.execute(
        """SELECT did FROM (
               SELECT a.did, a.influence_score
                 FROM actors a JOIN follower_state fs USING (did)
                WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
                ORDER BY a.influence_score DESC NULLS LAST LIMIT ?
           )
           UNION
           SELECT a.did FROM actors a JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
              AND a.verified_status = 'valid'""",
        (top_n,),
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
          LEFT JOIN follower_state fs USING (did)
          LEFT JOIN my_follows mf USING (did)
         WHERE (fs.is_current = 1 OR mf.did IS NOT NULL)
           AND a.unservable_since IS NULL
           AND (a.profile_fetched_at IS NULL
                OR julianday('now') - julianday(a.profile_fetched_at) >= ?)
         ORDER BY (a.profile_fetched_at IS NOT NULL),
                  (fs.did IS NULL),                      -- followers before follows
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
        "institution": ("institution_score IS NOT NULL OR did IN "
                        "(SELECT did FROM affiliations)"),
        "verified_affinity": "verified_affinity IS NOT NULL",
        "public_profile": "wikidata_sitelinks IS NOT NULL OR wikipedia_views_30d IS NOT NULL",
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


async def affinity_scale() -> float | None:
    """99th-percentile weighted overlap — the self-calibrating affinity ceiling."""
    db = await _db()
    async with db.execute(
        "SELECT COUNT(*) FROM actors WHERE affinity_sampled IS NOT NULL AND affinity_sampled > 0"
    ) as cur:
        n = int((await cur.fetchone())[0] or 0)
    if n < 20:
        return None
    async with db.execute(
        "SELECT affinity_sampled FROM actors WHERE affinity_sampled > 0 "
        "ORDER BY affinity_sampled DESC LIMIT 1 OFFSET ?", (max(n // 100, 1),)
    ) as cur:
        row = await cur.fetchone()
    return float(row[0]) if row else None


async def rescore_all() -> int:
    from sonde import scoring

    db = await _db()
    active = await active_score_components()
    scale = await affinity_scale()
    # The subject account can appear in its own follower list and in other
    # people's follow lists; it must not rank among its own followers.
    async with db.execute(
        "SELECT a.* FROM actors a JOIN follower_state fs USING (did) "
        "WHERE fs.is_current = 1 AND a.handle IS NOT ? AND a.did IS NOT ?",
        (settings.actor, await get_meta("subject_did")),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    await db.execute(
        "UPDATE actors SET influence_score = NULL, score_components = NULL "
        "WHERE handle = ? OR did = ?",
        (settings.actor, await get_meta("subject_did")),
    )
    # Affiliations supersede the single institution_* columns where they exist.
    async with db.execute(
        """SELECT a.did, MAX(a.confidence *
                 CASE a.kind WHEN 'leadership' THEN 1.15 WHEN 'founder' THEN 1.1
                             WHEN 'former' THEN 0.35 WHEN 'board' THEN 0.85
                             ELSE 1.0 END
                 * COALESCE(o.weight, 0.7)) AS best,
                  a.org_name, a.kind, a.method
             FROM affiliations a LEFT JOIN organisations o ON o.id = a.org_id
            WHERE COALESCE(a.confirmed, 1) = 1
            GROUP BY a.did"""
    ) as cur:
        best_aff = {r["did"]: dict(r) for r in await cur.fetchall()}

    updates = []
    for row in rows:
        row["affinity_scale"] = scale
        aff = best_aff.get(row["did"])
        if aff and (aff["best"] or 0) > (row.get("institution_score") or 0):
            row["institution_score"] = min(aff["best"], 1.0)
            row["institution_name"] = aff["org_name"]
            row["institution_method"] = f"{aff['method']} ({aff['kind']})"
            row["institution_confidence"] = min(aff["best"], 1.0)
        score = scoring.score_actor(row, active=active)
        updates.append((score.normalised, score.as_json(), row["did"]))
    await db.executemany(
        "UPDATE actors SET influence_score = ?, score_components = ? WHERE did = ?", updates
    )
    return len(updates)


# Sortable columns, mapped to SQL. Anything not listed falls back to influence,
# so a hand-edited query string cannot inject into the ORDER BY.
SORTABLE = {
    "influence": "a.influence_score",
    "followers": "a.followers_count",
    "follows": "a.follows_count",
    "handle": "a.handle",
    "name": "a.display_name",
    "recent": "fs.list_rank",
    "since": "fs.first_seen_at",
}


async def ranked_followers(
    limit: int = 50, offset: int = 0, *, order: str = "influence",
    direction: str = "desc", verified_only: bool = False,
    min_followers: int | None = None, query: str | None = None,
    mutual_only: bool = False,
) -> list[dict]:
    column = SORTABLE.get(order, SORTABLE["influence"])
    # list_rank ascending IS most-recent-first, so "recent desc" has to invert.
    if order == "recent":
        direction = "asc" if direction == "desc" else "desc"
    arrow = "ASC" if direction == "asc" else "DESC"
    # NULLS LAST keeps un-hydrated rows out of the way whichever way we sort.
    order_sql = f"{column} {arrow} NULLS LAST, a.handle ASC"

    where = ["fs.is_current = 1", "fs.ignored_at IS NULL", "a.handle IS NOT ?"]
    params: list = [settings.actor]
    if verified_only:
        where.append("a.verified_status = 'valid'")
    if mutual_only:
        where.append("mf.did IS NOT NULL")
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
        "SELECT a.*, fs.first_seen_at, fs.list_rank, (mf.did IS NOT NULL) AS is_mutual "
        "FROM actors a JOIN follower_state fs USING (did) "
        "LEFT JOIN my_follows mf USING (did) "
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


async def reconcile_orphaned_runs() -> int:
    """Close out runs left `running` by a restart.

    Nothing else resolves them, so without this a container restart mid-sweep
    leaves a job that appears to run forever. They are marked `interrupted`
    rather than `failed`: they did not fail, and conflating the two would make
    real failures harder to spot. No data risk either way — only a *complete*
    sweep computes departures, so an interrupted one is inert.
    """
    db = await _db()
    cur = await db.execute(
        "UPDATE sync_runs SET status = 'interrupted', ended_at = ?, "
        "error = COALESCE(error, 'interrupted by restart') "
        "WHERE status = 'running'",
        (utcnow(),),
    )
    await db.commit()
    return cur.rowcount or 0


async def record_progress(run_id: int, *, pages: int | None = None,
                          actors: int | None = None, hydrated: int | None = None,
                          calls: int | None = None) -> None:
    """Persist partial progress so an interrupted run shows how far it reached."""
    sets, params = [], []
    for column, value in (("pages_fetched", pages), ("actors_seen", actors),
                          ("profiles_hydrated", hydrated), ("api_calls", calls)):
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    if not sets:
        return
    db = await _db()
    await db.execute(f"UPDATE sync_runs SET {', '.join(sets)} WHERE id = ?", (*params, run_id))
    await db.commit()


async def last_run_ages() -> dict[str, float]:
    """Seconds since each job kind last completed successfully.

    Interval schedules live only in memory, so every restart pushes them out by
    a full interval. Deploy more often than a job's interval and it never runs
    at all. This lets startup notice a job is overdue and catch it up.
    """
    db = await _db()
    async with db.execute(
        "SELECT kind, MAX(ended_at) AS last FROM sync_runs "
        "WHERE status = 'ok' AND ended_at IS NOT NULL GROUP BY kind"
    ) as cur:
        rows = await cur.fetchall()
    now = datetime.now(timezone.utc)
    ages: dict[str, float] = {}
    for row in rows:
        try:
            ages[row["kind"]] = (now - datetime.fromisoformat(row["last"])).total_seconds()
        except (ValueError, TypeError):
            continue
    return ages


async def last_sync_age_seconds() -> float | None:
    db = await _db()
    async with db.execute(
        "SELECT ended_at FROM sync_runs WHERE status = 'ok' AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    if not row or not row["ended_at"]:
        return None
    try:
        ended = datetime.fromisoformat(row["ended_at"])
    except ValueError:
        return None
    return round((datetime.now(timezone.utc) - ended).total_seconds(), 1)


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
        "WHERE status NOT IN ('running', 'interrupted') ORDER BY id DESC LIMIT 1"
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
    mutuals = await mutual_count()
    reported = await get_meta("followers_reported")
    return {
        "tracked": tracked,
        "verified": verified,
        "private": private,
        "departed": departed,
        "mutuals": mutuals,
        "ignored": await ignored_count(),
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


async def replace_my_follows(dids: Sequence[str]) -> None:
    """Rewrite the follow list. Rows that vanish mean I unfollowed someone."""
    db = await _db()
    now = utcnow()
    await db.executemany(
        "INSERT INTO my_follows (did, last_seen_at) VALUES (?,?) "
        "ON CONFLICT (did) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        [(d, now) for d in dids],
    )
    await db.execute("DELETE FROM my_follows WHERE last_seen_at < ?", (now,))


async def mutual_count() -> int:
    return await _scalar(
        "SELECT COUNT(*) FROM follower_state fs JOIN my_follows mf USING (did) "
        "WHERE fs.is_current = 1"
    )


async def follower_detail(did: str) -> dict | None:
    """Everything known about one person, for the detail page."""
    db = await _db()
    async with db.execute(
        """SELECT a.*, fs.is_current, fs.first_seen_at, fs.last_seen_at,
                  fs.backfilled, fs.list_rank, fs.lost_at, fs.missed_sweeps,
                  fs.ignored_at, fs.ignored_reason, fs.ignore_locked,
                  fs.followed_at, fs.follow_uri,
                  (mf.did IS NOT NULL) AS is_mutual
             FROM actors a
             LEFT JOIN follower_state fs USING (did)
             LEFT JOIN my_follows mf USING (did)
            WHERE a.did = ?""",
        (did,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["components"] = json.loads(out.get("score_components") or "{}").get("components", [])
    out["verification_records"] = json.loads(out.get("verifications") or "[]")
    # Parsed here too, not just in wikidata_matched(), or the detail page
    # renders the raw JSON string.
    for key in ("wikidata_occupations", "wikidata_employers", "wikidata_positions",
                "wikidata_past_employers", "link_signals"):
        try:
            out[key] = json.loads(out.get(key) or "[]")
        except (ValueError, TypeError):
            out[key] = []
    out["labels_list"] = json.loads(out.get("labels") or "[]")
    out["is_private"] = "!no-unauthenticated" in out["labels_list"]
    out["events"] = await events_for(did)
    return out


async def export_rows() -> list[dict]:
    """Flat rows for CSV. Honours the private-follower display policy."""
    db = await _db()
    where = "fs.is_current = 1 AND fs.ignored_at IS NULL"
    if settings.respect_no_unauthenticated:
        where += " AND COALESCE(a.labels,'') NOT LIKE '%no-unauthenticated%'"
    async with db.execute(
        f"""SELECT a.did, a.handle, a.display_name, a.followers_count, a.follows_count,
                   a.posts_count, a.verified_status, a.trusted_verifier_status,
                   a.influence_score, a.account_created_at,
                   fs.first_seen_at, fs.list_rank,
                   (mf.did IS NOT NULL) AS is_mutual,
                   (COALESCE(a.labels,'') LIKE '%no-unauthenticated%') AS is_private
              FROM actors a
              JOIN follower_state fs USING (did)
              LEFT JOIN my_follows mf USING (did)
             WHERE {where}
             ORDER BY a.influence_score DESC NULLS LAST"""
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ------------------------------------------------------- M6 institutions

async def seed_institutions() -> int:
    """Insert the starter list. Existing rows are left alone — they're editable."""
    from sonde.institutions import SEED_INSTITUTIONS

    db = await _db()
    added = 0
    for inst in SEED_INSTITUTIONS:
        cur = await db.execute(
            """INSERT INTO institutions (name, weight, domains, aliases, discovered_at)
               VALUES (?,?,?,?,?) ON CONFLICT (name) DO NOTHING""",
            (inst["name"], inst["weight"], json.dumps(inst["domains"]),
             json.dumps(inst["aliases"]), utcnow()),
        )
        added += cur.rowcount or 0
    await db.commit()
    return added


async def discover_institutions_from_issuers() -> list[str]:
    """Every verification issuer seen in a sweep is an institution candidate.

    This is what makes the table grow itself as Bluesky's verifier programme
    expands, rather than needing someone to maintain a list by hand.
    """
    db = await _db()
    async with db.execute(
        "SELECT verifications FROM actors WHERE verifications IS NOT NULL "
        "AND verifications != '[]'"
    ) as cur:
        rows = await cur.fetchall()

    seen: dict[str, str] = {}
    for row in rows:
        for rec in json.loads(row["verifications"] or "[]"):
            handle = (rec.get("issuerHandle") or "").lower()
            if handle and handle != "bsky.app":
                seen[handle] = rec.get("issuerDisplayName") or handle

    added = []
    for handle, display in seen.items():
        async with db.execute(
            "SELECT id FROM institutions WHERE domains LIKE ?", (f'%"{handle}"%',)
        ) as cur:
            if await cur.fetchone():
                continue
        cur = await db.execute(
            """INSERT INTO institutions (name, weight, domains, aliases, discovered_at, notes)
               VALUES (?,?,?,?,?,?) ON CONFLICT (name) DO NOTHING""",
            (display, 0.9, json.dumps([handle]), json.dumps([display]), utcnow(),
             "auto-discovered from a verification issuer"),
        )
        if cur.rowcount:
            added.append(display)
    await db.commit()
    return added


async def all_institutions() -> list[dict]:
    db = await _db()
    async with db.execute("SELECT * FROM institutions ORDER BY name") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        r["domains"] = json.loads(r["domains"] or "[]")
        r["aliases"] = json.loads(r["aliases"] or "[]")
    return rows


async def roster_map() -> dict[str, set[int]]:
    """did -> institution ids that have verified them."""
    db = await _db()
    async with db.execute("SELECT institution_id, did FROM institution_roster") as cur:
        out: dict[str, set[int]] = {}
        for r in await cur.fetchall():
            out.setdefault(r["did"], set()).add(r["institution_id"])
    return out


async def save_roster(institution_id: int, records: list[dict]) -> int:
    db = await _db()
    await db.executemany(
        """INSERT INTO institution_roster (institution_id, did, handle, created_at)
           VALUES (?,?,?,?) ON CONFLICT (institution_id, did) DO UPDATE SET
             handle = excluded.handle""",
        [(institution_id, r["did"], r.get("handle"), r.get("createdAt")) for r in records],
    )
    await db.execute(
        "UPDATE institutions SET roster_fetched_at = ? WHERE id = ?",
        (utcnow(), institution_id),
    )
    await db.commit()
    return len(records)


# ------------------------------------------------------ M8 affiliations

async def upsert_organisation(name: str, *, kind: str | None = None,
                              wikidata_id: str | None = None,
                              sitelinks: int | None = None,
                              url: str | None = None) -> int:
    """Weight defaults from notability so there is no list to hand-maintain.

    A human-set weight is never overwritten: `weight_locked` marks the decision
    as theirs, which is the same rule hiding and moderation already follow.
    """
    from sonde.organisations import default_weight

    db = await _db()
    weight = default_weight(sitelinks, kind)
    await db.execute(
        """INSERT INTO organisations
             (name, kind, weight, wikidata_id, sitelinks, url, discovered_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (name) DO UPDATE SET
             kind        = COALESCE(excluded.kind, organisations.kind),
             wikidata_id = COALESCE(excluded.wikidata_id, organisations.wikidata_id),
             sitelinks   = COALESCE(excluded.sitelinks, organisations.sitelinks),
             url         = COALESCE(excluded.url, organisations.url),
             weight      = CASE WHEN organisations.weight_locked = 1
                                THEN organisations.weight ELSE excluded.weight END""",
        (name, kind, weight, wikidata_id, sitelinks, url, utcnow()),
    )
    async with db.execute("SELECT id FROM organisations WHERE name = ?", (name,)) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def save_affiliations(did: str, affiliations: list) -> int:
    """Replace derived affiliations, leaving anything a human touched alone."""
    db = await _db()
    await db.execute(
        "DELETE FROM affiliations WHERE did = ? AND confirmed IS NULL "
        "AND method != 'manual'", (did,),
    )
    now = utcnow()
    written = 0
    for aff in affiliations:
        org_id = await upsert_organisation(aff.org_name, url=aff.url)
        await db.execute(
            """INSERT INTO affiliations
                 (did, org_id, org_name, role, kind, method, confidence,
                  note, url, source_url, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (did, org_name, kind) DO UPDATE SET
                 role = excluded.role, method = excluded.method,
                 confidence = excluded.confidence, note = excluded.note,
                 url = excluded.url, source_url = excluded.source_url
               WHERE affiliations.confirmed IS NULL""",
            (did, org_id, aff.org_name, aff.role, aff.kind, aff.method,
             aff.confidence, aff.note, aff.url, aff.source_url, now),
        )
        written += 1
    return written


async def affiliations_for(did: str) -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT a.*, o.weight AS org_weight, o.kind AS org_kind,
                  o.sitelinks AS org_sitelinks
             FROM affiliations a LEFT JOIN organisations o ON o.id = a.org_id
            WHERE a.did = ? AND COALESCE(a.confirmed, 1) = 1
            ORDER BY a.confidence DESC""",
        (did,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def best_affiliation_score(did: str) -> tuple[float, dict | None]:
    """Best evidence wins rather than accumulating, so being both attested and
    bio-claimed at the same place is not counted twice."""
    from sonde.affiliations import KIND_WEIGHT

    best_score, best_row = 0.0, None
    for row in await affiliations_for(did):
        strength = min(row["confidence"] * KIND_WEIGHT.get(row["kind"], 1.0), 1.0)
        score = strength * (row["org_weight"] or 0.7)
        if score > best_score:
            best_score, best_row = score, row
    return best_score, best_row


async def rebuild_affiliations() -> dict:
    """Derive affiliations for the enrichment set from everything already stored."""
    from sonde.affiliations import from_links, from_wikidata
    from sonde.institutions import match_actor

    db = await _db()
    targets = await group_target_dids(top_n=settings.posts_top_n)
    if not targets:
        return {"scanned": 0, "affiliations": 0, "people": 0}
    placeholders = ",".join("?" for _ in targets)
    async with db.execute(
        f"""SELECT did, handle, description, verified_status, verifications,
                   wikidata_id, wikidata_employers, wikidata_positions,
                   wikidata_past_employers, link_signals
              FROM actors WHERE did IN ({placeholders})""",
        tuple(targets),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    institutions = await all_institutions()
    rosters = await roster_map()
    total = people = 0

    for row in rows:
        found = []
        row["verification_records"] = json.loads(row.get("verifications") or "[]")

        # 1. atproto evidence: attested / domain / roster / bio claim.
        match = match_actor(row, institutions, roster_ids=rosters.get(row["did"], set()))
        if match:
            from sonde.affiliations import Affiliation, kind_from_role

            found.append(Affiliation(
                org_name=match.name,
                kind=kind_from_role(match.role),
                method=match.method.split()[0],
                confidence=match.confidence,
                role=match.role,
                note=f"{match.method} via Bluesky",
                source_url=f"https://bsky.app/profile/{row['handle']}",
            ))

        # 2. Wikidata employers and positions — independent and reviewed.
        found += from_wikidata(
            json.loads(row.get("wikidata_employers") or "[]"),
            json.loads(row.get("wikidata_positions") or "[]"),
            row.get("wikidata_id"),
            past_employers=json.loads(row.get("wikidata_past_employers") or "[]"),
            description=row.get("description"),
        )

        # 3. Their own publications, from links they published themselves.
        found += from_links(json.loads(row.get("link_signals") or "[]"))

        if found:
            total += await save_affiliations(row["did"], found)
            people += 1

    await db.commit()
    return {"scanned": len(rows), "affiliations": total, "people": people}


# ------------------------------------------------- M15a institutions

ORG_SORTABLE = {
    "members": "members", "name": "o.name", "weight": "o.weight",
    "notability": "o.sitelinks", "kind": "o.kind",
}


async def organisation_summary(*, order: str = "members", direction: str = "desc",
                               kind: str | None = None,
                               min_members: int = 1) -> list[dict]:
    """Organisations with people in the enrichment set.

    An organisation with several people in it is already a group; this just
    names it. Former affiliations are counted separately rather than folded in —
    "used to be at Google" is worth knowing and must never read as current.
    """
    column = ORG_SORTABLE.get(order, ORG_SORTABLE["members"])
    arrow = "ASC" if direction == "asc" else "DESC"
    where = ["1 = 1"]
    params: list = []
    if kind:
        where.append("o.kind IS ?")
        params.append(kind)

    db = await _db()
    async with db.execute(
        f"""SELECT o.id, o.name, o.kind, o.weight, o.sitelinks, o.url,
                   o.weight_locked,
                   COUNT(DISTINCT CASE WHEN a.kind != 'former' THEN a.did END) AS members,
                   COUNT(DISTINCT CASE WHEN a.kind = 'former' THEN a.did END) AS former
              FROM organisations o
              JOIN affiliations a ON a.org_id = o.id
             WHERE {' AND '.join(where)} AND COALESCE(a.confirmed, 1) = 1
             GROUP BY o.id
            HAVING members + former >= ?
             ORDER BY {column} {arrow} NULLS LAST, o.name ASC""",
        (*params, min_members),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def organisation_members(name: str) -> dict:
    """Everyone affiliated with one organisation, current and former apart."""
    db = await _db()
    async with db.execute(
        """SELECT act.did, act.handle, act.display_name, act.followers_count,
                  act.influence_score, act.verified_status,
                  a.kind, a.role, a.method, a.confidence, a.note, a.source_url
             FROM affiliations a JOIN actors act USING (did)
            WHERE a.org_name = ? AND COALESCE(a.confirmed, 1) = 1
            ORDER BY (a.kind = 'former'), act.influence_score DESC NULLS LAST""",
        (name,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    async with db.execute("SELECT * FROM organisations WHERE name = ?", (name,)) as cur:
        org = await cur.fetchone()
    return {
        "organisation": dict(org) if org else {"name": name},
        "current": [r for r in rows if r["kind"] != "former"],
        "former": [r for r in rows if r["kind"] == "former"],
    }


async def organisation_kinds() -> list[str]:
    db = await _db()
    async with db.execute(
        "SELECT DISTINCT kind FROM organisations WHERE kind IS NOT NULL ORDER BY kind"
    ) as cur:
        return [r["kind"] for r in await cur.fetchall()]


async def set_organisation_weight(name: str, weight: float) -> None:
    """A human-set weight is locked; automation must not move it."""
    db = await _db()
    await db.execute(
        "UPDATE organisations SET weight = ?, weight_locked = 1 WHERE name = ?",
        (max(0.0, min(1.0, weight)), name),
    )
    await db.commit()


# ----------------------------------------------------------- M11 groups

async def seed_groups() -> int:
    from sonde.groups import GROUPS

    db = await _db()
    added = 0
    for group in GROUPS:
        cur = await db.execute(
            "INSERT INTO groups (slug, name, created_at) VALUES (?,?,?) "
            "ON CONFLICT (slug) DO NOTHING",
            (group["slug"], group["name"], utcnow()),
        )
        added += cur.rowcount or 0
    await db.commit()
    return added


async def classify_groups() -> dict:
    """Assign the enrichment set to groups from data already stored."""
    from sonde.groups import classify

    await seed_groups()
    db = await _db()
    async with db.execute("SELECT id, slug FROM groups") as cur:
        ids = {r["slug"]: r["id"] for r in await cur.fetchall()}

    targets = await group_target_dids(top_n=settings.posts_top_n)
    if not targets:
        return {"scanned": 0, "memberships": 0, "people": 0}
    placeholders = ",".join("?" for _ in targets)
    async with db.execute(
        f"""SELECT did, handle, description, wikidata_occupations,
                   wikidata_positions, link_signals
              FROM actors WHERE did IN ({placeholders})""",
        tuple(targets),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    # Post text is a strong signal and is already stored for this exact set.
    async with db.execute(
        f"SELECT did, text FROM posts WHERE did IN ({placeholders}) AND text IS NOT NULL",
        tuple(targets),
    ) as cur:
        texts: dict[str, list[str]] = {}
        for row in await cur.fetchall():
            texts.setdefault(row["did"], []).append(row["text"])

    # Derived rows are replaced; anything a human reviewed is left alone.
    await db.execute("DELETE FROM group_members WHERE confirmed IS NULL AND tier != 'manual'")

    now = utcnow()
    total = people = 0
    by_group: dict[str, int] = {}
    for row in rows:
        for key in ("wikidata_occupations", "wikidata_positions", "link_signals"):
            try:
                row[key] = json.loads(row.get(key) or "[]")
            except (ValueError, TypeError):
                row[key] = []
        row["affiliations"] = await affiliations_for(row["did"])
        row["post_texts"] = texts.get(row["did"], [])

        memberships = classify(row)
        if not memberships:
            continue
        people += 1
        for membership in memberships:
            group_id = ids.get(membership.slug)
            if group_id is None:
                continue
            await db.execute(
                """INSERT INTO group_members
                     (group_id, did, tier, confidence, evidence, source_url, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (group_id, did) DO UPDATE SET
                     tier = excluded.tier, confidence = excluded.confidence,
                     evidence = excluded.evidence
                   WHERE group_members.confirmed IS NULL""",
                (group_id, row["did"], membership.tier, membership.confidence,
                 membership.evidence, membership.source_url, now),
            )
            total += 1
            by_group[membership.slug] = by_group.get(membership.slug, 0) + 1

    await db.commit()
    return {"scanned": len(rows), "memberships": total, "people": people,
            "by_group": dict(sorted(by_group.items(), key=lambda kv: -kv[1]))}


async def group_summary() -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT g.slug, g.name, g.id,
                  COUNT(m.did) AS members,
                  SUM(CASE WHEN m.confirmed IS NULL THEN 1 ELSE 0 END) AS unreviewed
             FROM groups g
             LEFT JOIN group_members m ON m.group_id = g.id
                   AND COALESCE(m.confirmed, 1) = 1
            GROUP BY g.id ORDER BY members DESC, g.name"""
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# Sortable columns for member tables. Whitelisted so a hand-edited query
# string cannot reach the ORDER BY.
MEMBER_SORTABLE = {
    "influence": "a.influence_score",
    "followers": "a.followers_count",
    "handle": "a.handle",
    "name": "a.display_name",
    "confidence": "m.confidence",
    "tier": "m.tier",
}


async def group_members(slug: str, limit: int = 200, *, order: str = "influence",
                        direction: str = "desc") -> list[dict]:
    column = MEMBER_SORTABLE.get(order, MEMBER_SORTABLE["influence"])
    arrow = "ASC" if direction == "asc" else "DESC"
    db = await _db()
    async with db.execute(
        f"""SELECT a.did, a.handle, a.display_name, a.followers_count,
                   a.influence_score, a.verified_status,
                   m.tier, m.confidence, m.evidence, m.confirmed
              FROM group_members m
              JOIN groups g ON g.id = m.group_id
              JOIN actors a USING (did)
             WHERE g.slug = ? AND COALESCE(m.confirmed, 1) = 1
             ORDER BY {column} {arrow} NULLS LAST, a.handle ASC LIMIT ?""",
        (slug, limit),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def groups_for(did: str) -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT g.slug, g.name, m.tier, m.confidence, m.evidence, m.confirmed
             FROM group_members m JOIN groups g ON g.id = m.group_id
            WHERE m.did = ? AND COALESCE(m.confirmed, 1) = 1
            ORDER BY m.confidence DESC""",
        (did,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def review_group_member(group_slug: str, did: str, confirmed: bool) -> None:
    db = await _db()
    await db.execute(
        "UPDATE group_members SET confirmed = ? WHERE did = ? AND group_id = "
        "(SELECT id FROM groups WHERE slug = ?)",
        (1 if confirmed else 0, did, group_slug),
    )
    await db.commit()


async def unreviewed_affiliations(limit: int = 200) -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT a.*, act.handle, act.display_name, o.weight AS org_weight
             FROM affiliations a
             JOIN actors act USING (did)
             LEFT JOIN organisations o ON o.id = a.org_id
            WHERE a.confirmed IS NULL
            ORDER BY a.confidence DESC, act.handle LIMIT ?""",
        (limit,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def review_affiliation(affiliation_id: int, confirmed: bool) -> None:
    db = await _db()
    await db.execute(
        "UPDATE affiliations SET confirmed = ? WHERE id = ?",
        (1 if confirmed else 0, affiliation_id),
    )
    await db.commit()


async def apply_institution_matches() -> dict:
    """Match every current follower against the institution table."""
    from sonde.institutions import match_actor

    db = await _db()
    institutions = await all_institutions()
    rosters = await roster_map()

    async with db.execute(
        "SELECT a.did, a.handle, a.description, a.verified_status, a.verifications "
        "FROM actors a JOIN follower_state fs USING (did) WHERE fs.is_current = 1"
    ) as cur:
        actors = [dict(r) for r in await cur.fetchall()]

    updates, by_method = [], {}
    for actor in actors:
        actor["verification_records"] = json.loads(actor.get("verifications") or "[]")
        match = match_actor(actor, institutions, roster_ids=rosters.get(actor["did"], set()))
        if match is None:
            updates.append((None, None, None, None, None, None, actor["did"]))
            continue
        by_method[match.method] = by_method.get(match.method, 0) + 1
        updates.append((
            match.institution_id, match.name, round(match.score, 4),
            match.confidence, match.method, match.role, actor["did"],
        ))

    await db.executemany(
        """UPDATE actors SET institution_id = ?, institution_name = ?,
              institution_score = ?, institution_confidence = ?,
              institution_method = ?, institution_role = ?
            WHERE did = ?""",
        updates,
    )
    await db.commit()
    matched = sum(by_method.values())
    return {"matched": matched, "scanned": len(actors), "by_method": by_method}


async def institution_summary() -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT institution_name AS name, COUNT(*) AS n,
                  AVG(institution_score) AS avg_score,
                  GROUP_CONCAT(DISTINCT institution_method) AS methods
             FROM actors a JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND institution_name IS NOT NULL
            GROUP BY institution_name ORDER BY n DESC"""
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------- M6 affinity

async def choose_affinity_sources(
    min_follows: int, max_follows: int, cap: int
) -> list[dict]:
    """Pick index sources from a BAND of follow-list sizes.

    Not "the most selective accounts" — a pilot showed that rule selects
    accounts following 0–15 people, which cover 0.8% of followers because they
    endorse almost nobody. Selectivity belongs on the hit weight instead.
    Cheapest-first within the band keeps the call budget down.
    """
    db = await _db()
    async with db.execute(
        """SELECT a.did, a.handle, a.follows_count, a.verified_status
             FROM my_follows mf JOIN actors a USING (did)
            WHERE a.follows_count BETWEEN ? AND ?
            ORDER BY a.follows_count ASC
            LIMIT ?""",
        (min_follows, max_follows, cap),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def record_affinity_source(
    did: str, handle: str | None, follows_count: int | None,
    weight: float, is_verified: bool, pages: int,
) -> None:
    db = await _db()
    await db.execute(
        """INSERT INTO affinity_sources
             (did, handle, follows_count, weight, is_verified, pages_fetched, fetched_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (did) DO UPDATE SET
             follows_count = excluded.follows_count, weight = excluded.weight,
             is_verified = excluded.is_verified, pages_fetched = excluded.pages_fetched,
             fetched_at = excluded.fetched_at""",
        (did, handle, follows_count, weight, int(is_verified), pages, utcnow()),
    )


async def store_affinity(scores: dict[str, float], verified_hits: dict[str, int]) -> int:
    """Write the index results. Zero is a real value — it means 'measured, none'."""
    db = await _db()
    await db.execute(
        "UPDATE actors SET affinity_sampled = 0, verified_affinity = 0 "
        "WHERE did IN (SELECT did FROM follower_state WHERE is_current = 1)"
    )
    await db.executemany(
        "UPDATE actors SET affinity_sampled = ? WHERE did = ?",
        [(round(v, 2), d) for d, v in scores.items()],
    )
    await db.executemany(
        "UPDATE actors SET verified_affinity = ? WHERE did = ?",
        [(v, d) for d, v in verified_hits.items()],
    )
    await db.commit()
    return len(scores)


async def apply_wikidata(mapping: dict[str, dict]) -> int:
    """Join the bulk mapping onto followers by handle.

    Handle-exact only: a name-based match risks attaching the wrong person's
    reputation, which is a worse failure than attaching none.
    """
    db = await _db()
    async with db.execute(
        "SELECT did, handle FROM actors a JOIN follower_state fs USING (did) "
        "WHERE fs.is_current = 1 AND a.handle IS NOT NULL"
    ) as cur:
        followers = [(r["did"], (r["handle"] or "").lower()) for r in await cur.fetchall()]

    now = utcnow()
    updates = []
    for did, handle in followers:
        entity = mapping.get(handle)
        if not entity:
            continue
        updates.append((
            entity["qid"], entity["sitelinks"], entity.get("label"),
            json.dumps(entity.get("occupations") or []),
            json.dumps(entity.get("employers") or []),
            json.dumps(entity.get("positions") or []),
            json.dumps(entity.get("past_employers") or []),
            now, did,
        ))
    await db.executemany(
        """UPDATE actors SET wikidata_id = ?, wikidata_sitelinks = ?,
              wikipedia_title = ?, wikidata_occupations = ?,
              wikidata_employers = ?, wikidata_positions = ?,
              wikidata_past_employers = ?,
              external_fetched_at = ? WHERE did = ?""",
        updates,
    )
    await db.commit()
    return len(updates)


async def refresh_link_signals() -> dict:
    """Derive signals from bio links. No network calls — the text is stored."""
    from sonde.external.links import signals_for

    db = await _db()
    async with db.execute(
        "SELECT a.did, a.handle, a.description FROM actors a "
        "JOIN follower_state fs USING (did) WHERE fs.is_current = 1"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    updates, kinds = [], {}
    for row in rows:
        signals = signals_for(row["description"], row["handle"])
        updates.append((json.dumps(signals) if signals else None, row["did"]))
        for signal in signals:
            kinds[signal["kind"]] = kinds.get(signal["kind"], 0) + 1
    await db.executemany("UPDATE actors SET link_signals = ? WHERE did = ?", updates)
    await db.commit()
    return {"scanned": len(rows),
            "with_signals": sum(1 for u in updates if u[0]),
            "by_kind": dict(sorted(kinds.items(), key=lambda kv: -kv[1]))}


async def wikidata_qids_for_followers(mapping: dict[str, dict]) -> list[str]:
    """QIDs of entities that are actually followers — the ~1% worth detail on."""
    db = await _db()
    async with db.execute(
        "SELECT a.handle FROM actors a JOIN follower_state fs USING (did) "
        "WHERE fs.is_current = 1 AND a.handle IS NOT NULL"
    ) as cur:
        handles = {(r["handle"] or "").lower() for r in await cur.fetchall()}
    return sorted({
        entity["qid"] for handle, entity in mapping.items()
        if handle in handles and entity.get("qid")
    })


async def wikidata_matched() -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT a.did, a.handle, a.display_name, a.wikidata_id,
                  a.wikidata_sitelinks, a.wikipedia_title, a.wikipedia_views_30d,
                  a.wikidata_occupations, a.wikidata_employers, a.wikidata_positions,
                  a.influence_score
             FROM actors a JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
              AND a.wikidata_id IS NOT NULL
            ORDER BY a.wikidata_sitelinks DESC"""
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for row in rows:
        for key in ("wikidata_occupations", "wikidata_employers", "wikidata_positions"):
            try:
                row[key] = json.loads(row[key] or "[]")
            except ValueError:
                row[key] = []
    return rows


async def pageview_targets(limit: int = 200) -> list[dict]:
    """Wikidata-matched followers whose pageviews are stale. One call each."""
    db = await _db()
    async with db.execute(
        """SELECT did, wikipedia_title FROM actors a
             JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
              AND a.wikipedia_title IS NOT NULL
              AND a.wikidata_sitelinks > 0
              AND (a.pageviews_fetched_at IS NULL
                   OR julianday('now') - julianday(a.pageviews_fetched_at) >= 7)
            ORDER BY a.wikidata_sitelinks DESC LIMIT ?""",
        (limit,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def record_pageviews(did: str, views: int | None) -> None:
    db = await _db()
    await db.execute(
        "UPDATE actors SET wikipedia_views_30d = ?, pageviews_fetched_at = ? WHERE did = ?",
        (views, utcnow(), did),
    )


async def relevance_targets(limit: int = 1000) -> list[str]:
    """Top slice by influence, freshest-stale first. Auth-only and one call
    per actor, so it never covers everyone."""
    db = await _db()
    async with db.execute(
        """SELECT a.did FROM actors a JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
              AND a.handle IS NOT ?
            ORDER BY (a.affinity_exact IS NOT NULL),
                     a.influence_score DESC NULLS LAST
            LIMIT ?""",
        (settings.actor, limit),
    ) as cur:
        return [r["did"] for r in await cur.fetchall()]


async def record_exact_affinity(did: str, count: int) -> None:
    db = await _db()
    await db.execute(
        "UPDATE actors SET affinity_exact = ?, enriched_at = ? WHERE did = ?",
        (count, utcnow(), did),
    )


async def affinity_agreement() -> dict | None:
    """Does the sampled index agree with the exact counts?

    If it does not, the sample is too small — which is a fact worth surfacing
    rather than quietly scoring on.
    """
    db = await _db()
    async with db.execute(
        "SELECT affinity_sampled AS s, affinity_exact AS e FROM actors "
        "WHERE affinity_exact IS NOT NULL AND affinity_sampled IS NOT NULL"
    ) as cur:
        rows = [(r["s"] or 0, r["e"] or 0) for r in await cur.fetchall()]
    if len(rows) < 10:
        return None
    ranked_sampled = sorted(range(len(rows)), key=lambda i: -rows[i][0])
    ranked_exact = sorted(range(len(rows)), key=lambda i: -rows[i][1])
    top = max(len(rows) // 5, 5)
    overlap = len(set(ranked_sampled[:top]) & set(ranked_exact[:top]))
    return {
        "compared": len(rows),
        "top_overlap_pct": round(overlap / top * 100, 1),
        "note": "share of the top quintile the sampled index and exact counts agree on",
    }


async def affinity_source_count() -> int:
    return await _scalar("SELECT COUNT(*) FROM affinity_sources")


async def record_daily_snapshot() -> dict:
    """One rollup row per day. Gained/lost come from events, not from diffing."""
    db = await _db()
    day = datetime.now(timezone.utc).date().isoformat()
    c = await counts()
    mutuals = await _scalar(
        "SELECT COUNT(*) FROM follower_state fs JOIN my_follows mf USING (did) "
        "WHERE fs.is_current = 1"
    )
    gained = await _scalar(
        "SELECT COUNT(*) FROM follow_events WHERE event IN ('followed','returned') "
        "AND substr(detected_at, 1, 10) = ?", (day,)
    )
    lost = await _scalar(
        "SELECT COUNT(*) FROM follow_events WHERE event = 'departed' "
        "AND substr(detected_at, 1, 10) = ?", (day,)
    )
    await db.execute(
        """INSERT INTO daily_snapshots
             (day, followers_tracked, followers_reported, verified_total,
              mutuals_total, gained, lost)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (day) DO UPDATE SET
             followers_tracked  = excluded.followers_tracked,
             followers_reported = excluded.followers_reported,
             verified_total     = excluded.verified_total,
             mutuals_total      = excluded.mutuals_total,
             gained             = excluded.gained,
             lost               = excluded.lost""",
        (day, c["tracked"], c["reported"], c["verified"], mutuals, gained, lost),
    )
    await db.commit()
    return {"day": day, "tracked": c["tracked"], "gained": gained, "lost": lost}


async def growth_series(days: int = 90) -> list[dict]:
    db = await _db()
    async with db.execute(
        "SELECT * FROM daily_snapshots ORDER BY day DESC LIMIT ?", (days,)
    ) as cur:
        return [dict(r) for r in reversed(await cur.fetchall())]


async def recent_changes(limit: int = 100, event: str | None = None) -> list[dict]:
    db = await _db()
    sql = (
        "SELECT e.*, a.handle, a.display_name, a.followers_count, a.verified_status "
        "FROM follow_events e LEFT JOIN actors a USING (did) "
    )
    params: list = []
    if event:
        sql += "WHERE e.event = ? "
        params.append(event)
    sql += "ORDER BY e.detected_at DESC, e.id DESC LIMIT ?"
    params.append(limit)
    async with db.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def events_since(since_iso: str) -> list[dict]:
    """Events in a window, joined to what the digest needs to describe them."""
    db = await _db()
    async with db.execute(
        """SELECT e.did, e.event, e.reason, e.detail, e.detected_at,
                  a.handle, a.display_name, a.followers_count,
                  a.influence_score, a.verified_status
             FROM follow_events e LEFT JOIN actors a USING (did)
            WHERE e.detected_at >= ?
            ORDER BY e.detected_at DESC""",
        (since_iso,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def change_totals() -> dict:
    return {
        "followed": await _scalar("SELECT COUNT(*) FROM follow_events WHERE event = 'followed'"),
        "departed": await _scalar("SELECT COUNT(*) FROM follow_events WHERE event = 'departed'"),
        "returned": await _scalar("SELECT COUNT(*) FROM follow_events WHERE event = 'returned'"),
        "renamed": await _scalar(
            "SELECT COUNT(*) FROM follow_events WHERE event = 'handle_changed'"
        ),
    }


# --------------------------------------------------------- notices

async def _invalid_verification_subjects() -> list[dict]:
    db = await _db()
    async with db.execute(
        "SELECT a.did, a.handle, a.display_name FROM actors a "
        "JOIN follower_state fs USING (did) "
        "WHERE fs.is_current = 1 AND fs.ignored_at IS NULL "
        "AND a.verified_status = 'invalid' ORDER BY a.handle"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def active_notices() -> list[dict]:
    """Warnings not currently dismissed.

    The signature is the set of subjects, so dismissing "2 accounts" does not
    also silence a later "5 accounts" — the warning returns when what it is
    about actually changes.
    """
    import hashlib

    notices = []
    subjects = await _invalid_verification_subjects()
    if subjects:
        signature = hashlib.sha256(
            "|".join(sorted(s["did"] for s in subjects)).encode()
        ).hexdigest()[:16]
        notices.append({
            "kind": "invalid_verification",
            "signature": signature,
            "summary": (
                f"{len(subjects)} follower(s) carry a verification record that "
                f"fails validation"
            ),
            "detail": (
                "That is not the same as being unverified: a record exists but "
                "does not validate. Usually the issuer revoked it or the account "
                "changed handle after being verified."
            ),
            "subjects": subjects,
        })

    db = await _db()
    async with db.execute(
        "SELECT kind, signature FROM notice_dismissals"
    ) as cur:
        dismissed = {(r["kind"], r["signature"]) for r in await cur.fetchall()}
    return [n for n in notices if (n["kind"], n["signature"]) not in dismissed]


async def dismiss_notice(kind: str, signature: str) -> bool:
    """Record a dismissal. Append-only: dismissals are a log, not a flag."""
    for notice in await active_notices():
        if notice["kind"] == kind and notice["signature"] == signature:
            db = await _db()
            await db.execute(
                """INSERT INTO notice_dismissals
                     (kind, signature, summary, detail, dismissed_at)
                   VALUES (?,?,?,?,?)""",
                (kind, signature, notice["summary"],
                 json.dumps(notice.get("subjects", [])), utcnow()),
            )
            await db.commit()
            return True
    return False


async def dismissal_log(limit: int = 50) -> list[dict]:
    db = await _db()
    async with db.execute(
        "SELECT * FROM notice_dismissals ORDER BY id DESC LIMIT ?", (limit,)
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for row in rows:
        try:
            row["subjects"] = json.loads(row["detail"] or "[]")
        except ValueError:
            row["subjects"] = []
    return rows


async def restore_notice(dismissal_id: int) -> None:
    db = await _db()
    await db.execute("DELETE FROM notice_dismissals WHERE id = ?", (dismissal_id,))
    await db.commit()


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
        ("Mutuals", f"{c['mutuals']:,}", "we follow each other"),
        ("Departed", f"{c['departed']:,}", f"{c['private']:,} private"),
    ]
    needs_review = await get_meta("needs_review_count")
    return {
        "tiles": tiles,
        "counts": c,
        "recent_syncs": runs,
        "empty": c["tracked"] == 0,
        "needs_review": int(needs_review) if needs_review else None,
        "growth": await growth_series(60),
        "recent_changes": await recent_changes(12),
        "top": await ranked_followers(limit=10),
    }
