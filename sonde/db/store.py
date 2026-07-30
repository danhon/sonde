"""aiosqlite read/write helpers.

One module-level connection, WAL mode, opened lazily. SQLite handles a single
writer plus concurrent readers fine at this scale (~10k rows).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
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
    """Open (once) and migrate the database.

    An explicit `path` is remembered as the override. Without that, this
    function set `_db_path` but not `_override_path`, so the *next* no-argument
    call — which is every `_db()` — resolved to `settings.db_path` instead,
    saw a different target, and quietly closed the open database to reopen the
    default one. Tests that connected to a tmp file were therefore writing to
    the real `./sonde.db` from their first query onwards, sharing one file
    between every test and polluting the working copy.
    """
    global _conn, _db_path, _override_path
    if path is not None:
        _override_path = path
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
        ("relationship_score", "REAL"),
        ("relationship_components", "TEXT"),
    ],
    "my_follows": [
        # Needed to undo. Only set for follows sonde created; ones discovered by
        # the follows sweep have no URI, and their button says so rather than
        # offering an undo that would fail.
        ("follow_uri", "TEXT"),
        ("followed_by_sonde_at", "TEXT"),
    ],
    "group_candidates": [
        # A cluster candidate IS its members — unlike a rule-based one, there is
        # no term to re-run later, so accepting it has to use the stored list.
        ("members", "TEXT"),           # JSON array of DIDs
        ("tier", "TEXT"),              # which naming tier produced the label
    ],
    "groups": [
        ("archived_at", "TEXT"),
        # Where a merged group went. Kept so an archived source can say what
        # absorbed it, rather than looking like it was thrown away.
        ("merged_into", "TEXT"),
    ],
    "group_members": [
        ("decided_at", "TEXT"),
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
        "SELECT a.did, a.handle, a.display_name, a.avatar_url, "
        "a.followers_count, a.influence_score, "
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
    """Top N by influence, every verified follower, plus sweep candidates.

    The third term is M18. A top-N-by-influence set silently excludes any group
    whose members are less prominent than the corpus average: 198 followers have
    a game-industry bio and only 11 were in scope, because every influence
    component reads reach and studio staff are not famous the way journalists
    are. Groups that opt into `sweep_all` are matched against the whole list.

    Costs nothing extra — `getFollowers` returns `description` with the sweep,
    so roughly 8,000 of 10,041 bios are already on disk.
    """
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
        dids = {r["did"] for r in await cur.fetchall()}
    return sorted(dids | set(await sweep_candidate_dids()))


async def sweep_candidate_dids() -> list[str]:
    """Followers whose own bio carries evidence precise enough to be in scope."""
    from sonde.groups import SWEEP_GROUPS, sweep_match

    if not SWEEP_GROUPS:
        return []
    db = await _db()
    async with db.execute(
        """SELECT a.did, a.description FROM actors a
             JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
              AND a.description IS NOT NULL AND a.description != ''"""
    ) as cur:
        return [r["did"] for r in await cur.fetchall()
                if sweep_match(r["description"])]


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
    # "Since" means since they followed me, not since sonde noticed them. The
    # exact follow time is decoded from the TID in `viewer.followedBy` and is
    # only available on authenticated sweeps, so `first_seen_at` remains the
    # fallback — but it is a fallback, and the UI says so rather than passing
    # off "when this tool started watching" as "when they followed".
    "since": "COALESCE(fs.followed_at, fs.first_seen_at)",
}


def _with_since(row: dict) -> dict:
    """Attach the best available follow date, and whether it is exact."""
    row["since"] = row.get("followed_at") or row.get("first_seen_at")
    row["since_exact"] = bool(row.get("followed_at"))
    return row


async def ranked_followers(
    limit: int = 50, offset: int = 0, *, order: str = "influence",
    direction: str = "desc", verified_only: bool = False,
    min_followers: int | None = None, query: str | None = None,
    mutual_only: bool = False, tag: str | None = None,
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
    if tag:
        # EXISTS rather than a JOIN: a join against group_members emits one row
        # per membership, so anyone in two tags would be listed twice.
        where.append(
            """EXISTS (SELECT 1 FROM group_members m2
                         JOIN groups g2 ON g2.id = m2.group_id
                        WHERE m2.did = a.did AND g2.slug = ?
                          AND COALESCE(m2.confirmed, 1) = 1
                          AND g2.archived_at IS NULL)""")
        params.append(tag)
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
        "SELECT a.*, fs.first_seen_at, fs.followed_at, fs.backfilled, "
        "fs.list_rank, (mf.did IS NOT NULL) AS is_mutual "
        "FROM actors a JOIN follower_state fs USING (did) "
        "LEFT JOIN my_follows mf USING (did) "
        f"WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ? OFFSET ?"
    )
    async with db.execute(sql, (*params, limit, offset)) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for row in rows:
        row["components"] = json.loads(row["score_components"] or "{}").get("components", [])
        row["is_private"] = "no-unauthenticated" in (row.get("labels") or "")
        _with_since(row)
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
    """Rewrite the follow list. Rows that vanish mean I unfollowed someone.

    Replaced wholesale rather than diffed by timestamp. Two earlier versions
    marked survivors with `now` and deleted the rest by comparing timestamps —
    first `< now`, then `!= now` — and both fail identically when two syncs land
    in the same millisecond, because the stale row then carries `now` as well.
    An unfollow would simply go undetected.

    `my_follows` holds nothing worth preserving beyond the DID and a timestamp
    we overwrite anyway, so delete-then-insert inside one transaction is both
    simpler and exactly correct.
    """
    db = await _db()
    now = utcnow()
    # The follow URIs sonde created are the only thing here that cannot be
    # re-fetched — `getFollows` returns subjects, not record keys — and without
    # one a follow cannot be undone. Delete-then-insert would drop them on the
    # next sweep, so they are carried across.
    async with db.execute(
        "SELECT did, follow_uri, followed_by_sonde_at FROM my_follows "
        "WHERE follow_uri IS NOT NULL"
    ) as cur:
        created = {r["did"]: (r["follow_uri"], r["followed_by_sonde_at"])
                   for r in await cur.fetchall()}
    await db.execute("DELETE FROM my_follows")
    await db.executemany(
        "INSERT INTO my_follows (did, last_seen_at, follow_uri, "
        "                        followed_by_sonde_at) VALUES (?,?,?,?) "
        "ON CONFLICT (did) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        [(did, now, *created.get(did, (None, None))) for did in dids],
    )


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
    out = _with_since(dict(row))
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
    try:
        out["relationship"] = json.loads(out.get("relationship_components") or "{}")
    except (ValueError, TypeError):
        out["relationship"] = {}
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
                   fs.first_seen_at, fs.followed_at,
                   COALESCE(fs.followed_at, fs.first_seen_at) AS following_since,
                   (fs.followed_at IS NOT NULL) AS following_since_exact,
                   fs.list_rank,
                   (mf.did IS NOT NULL) AS is_mutual,
                   (COALESCE(a.labels,'') LIKE '%no-unauthenticated%') AS is_private,
                   (SELECT GROUP_CONCAT(g.slug, ';')
                      FROM group_members m
                      JOIN groups g ON g.id = m.group_id
                     WHERE m.did = a.did
                       AND COALESCE(m.confirmed, 1) = 1
                       AND g.archived_at IS NULL) AS tags
              FROM actors a
              JOIN follower_state fs USING (did)
              LEFT JOIN my_follows mf USING (did)
             WHERE {where}
             ORDER BY a.influence_score DESC NULLS LAST"""
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    # GROUP_CONCAT has no ordering guarantee in SQLite, and a file people diff
    # must not reshuffle its own cells between runs.
    for row in rows:
        row["tags"] = ";".join(sorted(row["tags"].split(";"))) if row["tags"] else ""
    return rows


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


# ------------------------------------------- M14 relationships

async def latest_interaction_at() -> str | None:
    db = await _db()
    async with db.execute("SELECT MAX(occurred_at) AS m FROM interactions") as cur:
        row = await cur.fetchone()
    return row["m"] if row else None


async def record_interactions(rows: list[dict]) -> int:
    """Append-only. The API's retention window is finite; ours is not."""
    if not rows:
        return 0
    db = await _db()
    await db.executemany(
        """INSERT INTO interactions
             (did, direction, kind, uri, subject, thread, occurred_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (did, direction, kind, uri) DO NOTHING""",
        # A NULL uri would defeat the UNIQUE constraint — SQLite treats NULLs
        # as distinct — so an interaction without one would re-insert on every
        # sync. Notifications always carry a uri, but that should not be what
        # the deduplication depends on.
        [(r["did"], r["direction"], r["kind"],
          r.get("uri") or f"synthetic:{r['did']}:{r['kind']}:{r['occurred_at']}",
          r.get("subject"), r.get("thread"), r["occurred_at"]) for r in rows],
    )
    await db.commit()
    return len(rows)


async def conversation_counts() -> dict[str, int]:
    """Threads where both of us posted more than once.

    The strongest single signal of a relationship, and the hardest to fake.
    """
    db = await _db()
    async with db.execute(
        """SELECT did, COUNT(*) AS threads FROM (
               SELECT did, thread
                 FROM interactions
                WHERE thread IS NOT NULL AND kind IN ('reply', 'quote')
                GROUP BY did, thread
               HAVING COUNT(DISTINCT direction) = 2 AND COUNT(*) >= 2
           ) GROUP BY did"""
    ) as cur:
        return {r["did"]: r["threads"] for r in await cur.fetchall()}


async def relationship_scale() -> float:
    """Self-calibrating ceiling, like the affinity index uses.

    A fixed constant would silently rescale every relationship as history
    accumulates, which it does by design here.
    """
    from sonde.relationships import DEFAULT_SCALE

    db = await _db()
    async with db.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT did FROM interactions)"
    ) as cur:
        people = int((await cur.fetchone())[0] or 0)
    if people < 20:
        return DEFAULT_SCALE
    return DEFAULT_SCALE


async def score_relationships() -> dict:
    """Fold interactions and attention scarcity into a score per person.

    Deliberately scores people with **no** interactions at all: attention
    scarcity (M17) is relationship evidence on its own, so an early return on
    an empty `interactions` table would discard the whole signal.
    """
    from sonde import attention as attn
    from sonde import relationships as rel

    db = await _db()
    conversations = await conversation_counts()
    async with db.execute(
        "SELECT did, direction, kind, thread, occurred_at FROM interactions "
        "ORDER BY did"
    ) as cur:
        by_did: dict[str, list[dict]] = {}
        for row in await cur.fetchall():
            by_did.setdefault(row["did"], []).append(dict(row))

    # Attention needs both counts; hydration fills them, so unhydrated
    # followers simply contribute nothing rather than a misleading zero.
    async with db.execute(
        "SELECT did, followers_count, follows_count FROM actors "
        "WHERE followers_count IS NOT NULL AND follows_count IS NOT NULL"
    ) as cur:
        counts = {
            row["did"]: (row["followers_count"], row["follows_count"])
            for row in await cur.fetchall()
        }
    attention_raw = {
        did: attn.raw(followers, follows)
        for did, (followers, follows) in counts.items()
    }
    attention_scale = attn.calibrate(list(attention_raw.values()))

    scored_dids = set(by_did) | {d for d, v in attention_raw.items() if v > 0}
    if not scored_dids:
        return {"scored": 0}

    summaries = {
        did: rel.summarise(by_did[did], conversations.get(did, 0))
        if by_did.get(did) else rel.Relationship(did=did)
        for did in scored_dids
    }
    for did, summary in summaries.items():
        followers, follows = counts.get(did, (None, None))
        summary.attention = attn.points(followers, follows, attention_scale)
        summary.attention_note = attn.describe(followers, follows)
        summary.attention_detail = attn.explain(followers, follows, attention_scale)

    # Calibrate the interaction half against the observed spread rather than a
    # fixed constant. Only people who actually interacted set that scale.
    raws = sorted((s.raw for s in summaries.values() if s.raw > 0), reverse=True)
    scale = max(raws[min(len(raws) // 20, len(raws) - 1)], 1.0) if raws else 1.0

    updates = []
    for did, summary in summaries.items():
        payload = summary.as_dict(scale)
        updates.append((payload["score"], json.dumps(payload), did))
    await db.executemany(
        "UPDATE actors SET relationship_score = ?, relationship_components = ? "
        "WHERE did = ?", updates,
    )
    await db.commit()
    with_attention = sum(1 for s in summaries.values() if s.attention > 0)
    return {"scored": len(updates), "scale": round(scale, 2),
            "with_conversations": len(conversations),
            "with_attention": with_attention,
            "attention_scale": round(attention_scale, 3)}


REL_SORTABLE = {
    "relationship": "a.relationship_score",
    "influence": "a.influence_score",
    "followers": "a.followers_count",
    "handle": "a.handle",
    # Attention lives in the components JSON rather than its own column: it is
    # derived from two columns already stored, so a third would be a cache to
    # keep in sync for no gain.
    "attention": "json_extract(a.relationship_components, '$.attention')",
}


async def ranked_relationships(limit: int = 100, offset: int = 0, *,
                               order: str = "relationship",
                               direction: str = "desc") -> list[dict]:
    column = REL_SORTABLE.get(order, REL_SORTABLE["relationship"])
    arrow = "ASC" if direction == "asc" else "DESC"
    db = await _db()
    async with db.execute(
        f"""SELECT a.did, a.handle, a.display_name, a.avatar_url,
                   a.followers_count, a.influence_score, a.relationship_score,
                   a.relationship_components, a.verified_status,
                   (mf.did IS NOT NULL) AS is_mutual
              FROM actors a JOIN follower_state fs USING (did)
              LEFT JOIN my_follows mf ON mf.did = a.did
             WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
               AND a.relationship_score > 0
             ORDER BY {column} {arrow} NULLS LAST, a.handle LIMIT ? OFFSET ?""",
        (limit, offset),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for row in rows:
        try:
            row["relationship"] = json.loads(row["relationship_components"] or "{}")
        except ValueError:
            row["relationship"] = {}
    return rows


INTERACTION_KINDS = ("reply", "quote", "repost", "like", "mention")


async def record_follow_failure(did: str, error: str, *, undo: bool) -> None:
    """A failed write is logged, not swallowed."""
    db = await _db()
    await db.execute(
        "INSERT INTO follow_events (did, event, reason, detail, detected_at) "
        "VALUES (?,?,?,?,?)",
        (did, "unfollow_failed" if undo else "follow_failed", "error",
         error[:500], utcnow()),
    )
    await db.commit()


# ------------------------------------------------------------- M24 charts

async def account_cohorts() -> list[dict]:
    """When current followers' accounts were created, by month."""
    db = await _db()
    async with db.execute(
        """SELECT substr(a.account_created_at, 1, 7) AS month, COUNT(*) AS n
             FROM actors a JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
              AND a.account_created_at IS NOT NULL
            GROUP BY month ORDER BY month"""
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def weekly_arrivals(weeks: int = 4) -> list[dict]:
    """New followers per rolling week, oldest first.

    Rolling seven-day windows counted back from today, not calendar weeks: the
    homepage answers "what happened lately", and a calendar bucket makes the
    current week look like a collapse every Monday.

    `followed_at` where the authenticated sweep decoded it from the follow
    record, `first_seen_at` otherwise — the same COALESCE every other "since"
    reading in the app uses, so the numbers agree with /changes.
    """
    db = await _db()
    async with db.execute(
        """SELECT CAST(julianday('now') - julianday(
                      COALESCE(fs.followed_at, fs.first_seen_at)) AS INTEGER) / 7
                    AS weeks_ago,
                  COUNT(*) AS n
             FROM follower_state fs
            WHERE COALESCE(fs.followed_at, fs.first_seen_at) >= date('now', ?)
              AND fs.ignored_at IS NULL
            GROUP BY weeks_ago""",
        (f"-{weeks * 7} day",),
    ) as cur:
        counts = {row["weeks_ago"]: row["n"] for row in await cur.fetchall()}

    # Every bucket present, including the empty ones. A quiet week is a fact
    # about the week, and a chart that silently omits it misreports the trend.
    return [{"label": "this week" if ago == 0 else f"{ago + 1}w ago",
             "weeks_ago": ago, "n": counts.get(ago, 0)}
            for ago in range(weeks - 1, -1, -1)]


async def attention_points(limit: int = 2000) -> list[dict]:
    """Hydrated followers as (follows, followers) for the scarcity scatter."""
    db = await _db()
    async with db.execute(
        """SELECT a.did, a.handle, a.followers_count AS followers,
                  a.follows_count AS follows, a.relationship_score,
                  a.influence_score
             FROM actors a JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
              AND a.followers_count IS NOT NULL AND a.follows_count IS NOT NULL
            ORDER BY a.followers_count DESC LIMIT ?""",
        (limit,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def interaction_series(*, direction: str = "inbound") -> dict:
    """Monthly counts per kind. Separate series, never summed."""
    direction = "outbound" if direction == "outbound" else "inbound"
    db = await _db()
    async with db.execute(
        """SELECT kind, substr(occurred_at, 1, 7) AS month, COUNT(*) AS n
             FROM interactions WHERE direction = ?
            GROUP BY kind, month ORDER BY month""",
        (direction,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    months = sorted({r["month"] for r in rows})
    series = {}
    for kind in INTERACTION_KINDS:
        by_month = {r["month"]: r["n"] for r in rows if r["kind"] == kind}
        if by_month:
            series[kind] = [by_month.get(m, 0) for m in months]
    return {"months": months, "series": series, "direction": direction}


async def _in_scope_count() -> int:
    """The grouping set, plus anyone a human tagged by hand.

    A hand-applied tag is a fact about a person regardless of how influential
    they are, so manual members exist outside the target set — and a
    denominator that excluded them would under-report the moment the first one
    was tagged.
    """
    targets = set(await group_target_dids(top_n=settings.posts_top_n))
    db = await _db()
    async with db.execute(
        """SELECT DISTINCT m.did FROM group_members m
             JOIN groups g ON g.id = m.group_id
             JOIN follower_state fs ON fs.did = m.did
            WHERE m.tier = 'manual' AND COALESCE(m.confirmed, 1) = 1
              AND g.archived_at IS NULL
              AND fs.is_current = 1 AND fs.ignored_at IS NULL"""
    ) as cur:
        targets.update(r["did"] for r in await cur.fetchall())
    return len(targets)


async def composition() -> dict:
    """Named groups by size, and how much of the audience they cover."""
    db = await _db()
    async with db.execute(
        """SELECT g.slug, g.name, COUNT(DISTINCT m.did) AS n
             FROM groups g JOIN group_members m ON m.group_id = g.id
             JOIN follower_state fs ON fs.did = m.did
            WHERE COALESCE(m.confirmed, 1) = 1 AND g.archived_at IS NULL
              AND fs.is_current = 1 AND fs.ignored_at IS NULL
            GROUP BY g.id HAVING n > 0 ORDER BY n DESC"""
    ) as cur:
        groups = [dict(r) for r in await cur.fetchall()]
    grouped = await _scalar(
        """SELECT COUNT(DISTINCT m.did) FROM group_members m
             JOIN follower_state fs ON fs.did = m.did
            WHERE COALESCE(m.confirmed, 1) = 1
              AND fs.is_current = 1 AND fs.ignored_at IS NULL""")
    clustered = await _scalar(
        "SELECT COUNT(*) FROM group_candidates WHERE kind = 'cluster' "
        "AND decided IS NULL")
    return {"groups": groups, "people_in_a_group": grouped,
            "clusters_proposed": clustered,
            "in_scope": await _in_scope_count()}


async def verification_reach() -> dict:
    """Verified followers by issuer, with the reach of each cohort."""
    db = await _db()
    async with db.execute(
        """SELECT a.verifications, a.followers_count
             FROM actors a JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
              AND a.verified_status = 'valid'"""
    ) as cur:
        rows = await cur.fetchall()
    by_issuer: dict[str, list[int]] = {}
    for row in rows:
        try:
            records = json.loads(row["verifications"] or "[]")
        except (ValueError, TypeError):
            records = []
        for record in records:
            issuer = record.get("issuerHandle") or record.get("issuer") or "unknown"
            by_issuer.setdefault(issuer, []).append(row["followers_count"] or 0)
    unverified = await _scalar(
        """SELECT COUNT(*) FROM actors a JOIN follower_state fs USING (did)
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL
              AND COALESCE(a.verified_status, 'none') != 'valid'""")
    return {
        "issuers": sorted(
            ({"issuer": k, "n": len(v),
              "median_followers": sorted(v)[len(v) // 2] if v else 0}
             for k, v in by_issuer.items()),
            key=lambda d: -d["n"]),
        "verified_total": len(rows), "unverified_total": unverified,
    }


async def interaction_window() -> dict:
    """What period these counts actually cover — **per direction**.

    The two directions come from different places and have wildly different
    coverage, so one combined window is a misleading number:

      inbound   `listNotifications`, which retains for a finite, undocumented
                period. This is the one that truncates.
      outbound  my own author feed, which is my whole posting history. It
                reaches back as far as I have posted.

    Reported together, the outbound floor sets the earliest date and makes
    inbound look complete back to it. On real data that read as "observed from
    2023-12-16" when the inbound half almost certainly starts much later.
    """
    db = await _db()
    async with db.execute(
        """SELECT direction, COUNT(*) AS events, MIN(occurred_at) AS earliest,
                  MAX(occurred_at) AS latest, COUNT(DISTINCT did) AS people
             FROM interactions GROUP BY direction"""
    ) as cur:
        by_direction = {r["direction"]: dict(r) for r in await cur.fetchall()}
    async with db.execute(
        """SELECT COUNT(*) AS events, MIN(occurred_at) AS earliest,
                  MAX(occurred_at) AS latest,
                  COUNT(DISTINCT did) AS people FROM interactions"""
    ) as cur:
        out = dict(await cur.fetchone())
    out["by_direction"] = by_direction
    return out


# Sorting the aggregate, not the row: these are GROUP BY results, so the
# sortable set is the computed columns plus the actor's own.
IX_SORTABLE = {
    "count": "n",
    "back": "reciprocal",
    "days": "days_active",
    "last": "last_at",
    "first": "first_at",
    "handle": "a.handle",
    "followers": "a.followers_count",
    "influence": "a.influence_score",
    "relationship": "a.relationship_score",
}


async def interaction_leaderboard(kind: str, *, direction: str = "inbound",
                                  days: int | None = None,
                                  order: str = "count",
                                  sort_direction: str = "desc",
                                  limit: int = 100) -> list[dict]:
    """Who does the most of one kind of interaction, ranked.

    One kind at a time on purpose. A like costs nothing and a reply costs real
    attention, so a combined ranking is a like-count leaderboard in disguise.
    """
    if kind not in INTERACTION_KINDS:
        kind = "reply"
    direction = "outbound" if direction == "outbound" else "inbound"
    other = "outbound" if direction == "inbound" else "inbound"
    column = IX_SORTABLE.get(order, IX_SORTABLE["count"])
    arrow = "ASC" if sort_direction == "asc" else "DESC"

    where = ["i.kind = ?", "i.direction = ?", "fs.is_current = 1",
             "fs.ignored_at IS NULL"]
    params: list = [kind, direction]
    if days:
        where.append("i.occurred_at >= ?")
        params.append(
            (datetime.now(timezone.utc) - timedelta(days=days)).isoformat())

    db = await _db()
    async with db.execute(
        f"""SELECT i.did, COUNT(*) AS n, MAX(i.occurred_at) AS last_at,
                   MIN(i.occurred_at) AS first_at,
                   COUNT(DISTINCT substr(i.occurred_at, 1, 10)) AS days_active,
                   a.handle, a.display_name, a.avatar_url, a.followers_count,
                   a.influence_score, a.relationship_score, a.verified_status,
                   (mf.did IS NOT NULL) AS is_mutual,
                   (SELECT COUNT(*) FROM interactions r
                     WHERE r.did = i.did AND r.kind = i.kind
                       AND r.direction = '{other}') AS reciprocal
              FROM interactions i
              JOIN actors a USING (did)
              JOIN follower_state fs USING (did)
              LEFT JOIN my_follows mf ON mf.did = i.did
             WHERE {' AND '.join(where)}
             GROUP BY i.did
             ORDER BY {column} {arrow} NULLS LAST, n DESC, a.handle ASC
             LIMIT ?""".replace("{column}", column).replace("{arrow}", arrow),
        (*params, limit),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def interaction_totals(*, days: int | None = None) -> dict:
    """Per-kind, per-direction totals, for the tab counts."""
    where = ""
    params: list = []
    if days:
        where = "WHERE occurred_at >= ?"
        params.append(
            (datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
    db = await _db()
    async with db.execute(
        f"""SELECT kind, direction, COUNT(*) AS n,
                   COUNT(DISTINCT did) AS people
              FROM interactions {where} GROUP BY kind, direction""",
        tuple(params),
    ) as cur:
        out: dict[str, dict[str, dict]] = {}
        for row in await cur.fetchall():
            out.setdefault(row["kind"], {})[row["direction"]] = {
                "n": row["n"], "people": row["people"]}
    return out


async def interaction_breakdown(did: str) -> dict:
    """One person's counts, split by kind AND direction.

    Split, because a person I reply to constantly who never answers is not the
    same as one who replies to me constantly, and a single number cannot tell
    them apart.
    """
    db = await _db()
    async with db.execute(
        """SELECT kind, direction, COUNT(*) AS n, MAX(occurred_at) AS last_at
             FROM interactions WHERE did = ? GROUP BY kind, direction""",
        (did,),
    ) as cur:
        out = {k: {"inbound": 0, "outbound": 0, "last_at": None}
               for k in INTERACTION_KINDS}
        for row in await cur.fetchall():
            entry = out.setdefault(
                row["kind"], {"inbound": 0, "outbound": 0, "last_at": None})
            entry[row["direction"]] = row["n"]
            if not entry["last_at"] or row["last_at"] > entry["last_at"]:
                entry["last_at"] = row["last_at"]
    return out


async def follow_back_target(did: str) -> dict | None:
    """Check a follow-back is legitimate before anything is written.

    Returns the row when following is possible, or None. This is the only guard
    between a URL and a public write on a real account, so it is deliberately
    strict: the subject must be a current, non-hidden follower we are not
    already following.
    """
    db = await _db()
    async with db.execute(
        """SELECT a.did, a.handle, fs.is_current, fs.ignored_at,
                  mf.did IS NOT NULL AS already, mf.follow_uri
             FROM actors a
             LEFT JOIN follower_state fs USING (did)
             LEFT JOIN my_follows mf USING (did)
            WHERE a.did = ?""",
        (did,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return dict(row)


async def record_my_follow(did: str, uri: str | None) -> None:
    db = await _db()
    now = utcnow()
    await db.execute(
        """INSERT INTO my_follows (did, last_seen_at, follow_uri,
                                   followed_by_sonde_at)
           VALUES (?,?,?,?)
           ON CONFLICT (did) DO UPDATE SET
             last_seen_at = excluded.last_seen_at,
             follow_uri = excluded.follow_uri,
             followed_by_sonde_at = excluded.followed_by_sonde_at""",
        (did, now, uri, now),
    )
    await db.execute(
        "INSERT INTO follow_events (did, event, reason, detail, detected_at) "
        "VALUES (?,?,?,?,?)",
        (did, "followed_back", "manual", uri or "", now),
    )
    await db.commit()


async def forget_my_follow(did: str) -> None:
    db = await _db()
    now = utcnow()
    await db.execute("DELETE FROM my_follows WHERE did = ?", (did,))
    await db.execute(
        "INSERT INTO follow_events (did, event, reason, detail, detected_at) "
        "VALUES (?,?,?,?,?)",
        (did, "unfollowed_back", "manual", "", now),
    )
    await db.commit()


async def interactions_for(did: str, limit: int = 30) -> list[dict]:
    db = await _db()
    async with db.execute(
        "SELECT * FROM interactions WHERE did = ? ORDER BY occurred_at DESC LIMIT ?",
        (did, limit),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ------------------------------------------- M15b group discovery

async def discover_group_candidates() -> dict:
    """Propose groups from data already stored. Nothing is created here."""
    import collections

    from sonde import discovery

    db = await _db()
    targets = await group_target_dids(top_n=settings.posts_top_n)
    if not targets:
        return {"candidates": 0, "by_kind": {}}
    placeholders = ",".join("?" for _ in targets)

    occupations: collections.Counter = collections.Counter()
    link_kinds: collections.Counter = collections.Counter()
    documents: list[str] = []
    async with db.execute(
        f"""SELECT wikidata_occupations, link_signals, description
              FROM actors WHERE did IN ({placeholders})""",
        tuple(targets),
    ) as cur:
        for row in await cur.fetchall():
            for occupation in json.loads(row["wikidata_occupations"] or "[]"):
                occupations[occupation.lower()] += 1
            for signal in json.loads(row["link_signals"] or "[]"):
                link_kinds[signal.get("kind", "")] += 1
            if row["description"]:
                documents.append(row["description"])

    async with db.execute(
        f"SELECT text FROM posts WHERE did IN ({placeholders}) AND text IS NOT NULL",
        tuple(targets),
    ) as cur:
        documents += [r["text"] for r in await cur.fetchall()]

    covered = discovery.covered_terms()
    orgs = await organisation_summary(min_members=discovery.MIN_MEMBERS)

    candidates = (
        discovery.occupation_candidates(occupations, covered)
        + discovery.link_candidates(link_kinds, covered)
        + discovery.organisation_candidates(orgs, covered)
        + discovery.phrase_candidates(documents, covered)
    )

    now = utcnow()
    for candidate in candidates:
        # Existing decisions are preserved: a rejected candidate stays rejected
        # rather than reappearing every time discovery runs.
        await db.execute(
            """INSERT INTO group_candidates
                 (kind, term, label, member_count, why, first_seen_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (kind, term) DO UPDATE SET
                 member_count = excluded.member_count, why = excluded.why""",
            (candidate["kind"], candidate["term"], candidate["label"],
             candidate["count"], candidate["why"], now),
        )
    await db.commit()

    by_kind: dict[str, int] = {}
    for candidate in candidates:
        by_kind[candidate["kind"]] = by_kind.get(candidate["kind"], 0) + 1
    return {"candidates": len(candidates), "by_kind": by_kind}


async def discover_latent_groups() -> dict:
    """Propose communities found in the follow graph. Creates nothing.

    Unlike the rule-based candidates, these are not derived from a term anyone
    wrote down — the graph says these people belong together and the label, if
    any, is worked out afterwards. That is the point: it can only find groups
    nobody thought to name.
    """
    from collections import Counter

    from sonde import clustering

    db = await _db()
    async with db.execute(
        """SELECT e.source_did, e.did FROM affinity_edges e
             JOIN follower_state fs ON fs.did = e.did
            WHERE fs.is_current = 1 AND fs.ignored_at IS NULL"""
    ) as cur:
        sources_by_did: dict[str, set[str]] = {}
        for row in await cur.fetchall():
            sources_by_did.setdefault(row["did"], set()).add(row["source_did"])

    placeable = sum(1 for s in sources_by_did.values()
                    if len(s) >= clustering.MIN_SOURCES)
    clusters = clustering.cluster(sources_by_did)
    if not clusters:
        return {"proposed": 0, "clusters": 0, "placeable": placeable,
                "reached": len(sources_by_did)}

    # Evidence for naming, all already on disk — no calls.
    members_all = {did for c in clusters for did in c}
    placeholders = ",".join("?" for _ in members_all)
    evidence: dict[str, dict[str, list[str]]] = {
        "affiliation": {}, "occupation": {}, "link": {}, "text": {},
    }
    async with db.execute(
        f"""SELECT did, description, display_name, wikidata_occupations,
                   link_signals
              FROM actors WHERE did IN ({placeholders})""",
        tuple(members_all),
    ) as cur:
        for row in await cur.fetchall():
            did = row["did"]
            # Bio only. Display names contributed surnames as vocabulary —
            # a cluster came back labelled "White · Queer · Science", where
            # "white" was somebody's name. A person's name describes no group.
            evidence["text"][did] = sorted(clustering._tokens(row["description"] or ""))
            try:
                evidence["occupation"][did] = [
                    o.lower() for o in json.loads(row["wikidata_occupations"] or "[]")]
            except (ValueError, TypeError):
                evidence["occupation"][did] = []
            try:
                evidence["link"][did] = [
                    s.get("kind") for s in json.loads(row["link_signals"] or "[]")
                    if s.get("kind") and s.get("kind") != "site"]
            except (ValueError, TypeError):
                evidence["link"][did] = []
    async with db.execute(
        f"""SELECT did, org_name FROM affiliations
             WHERE did IN ({placeholders}) AND kind != 'former'
               AND COALESCE(confirmed, 1) = 1""",
        tuple(members_all),
    ) as cur:
        for row in await cur.fetchall():
            evidence["affiliation"].setdefault(row["did"], []).append(
                row["org_name"].lower())

    # The baseline every cluster is measured against is the placeable
    # population, not the whole follower list: a term common among people the
    # graph can see is not distinctive just because it is rare overall.
    corpora = {
        tier: Counter(t for did in members_all for t in set(values.get(did) or []))
        for tier, values in evidence.items()
    }
    total = max(len(members_all), 1)

    # Which existing group each cluster most resembles. Not a filter: a cluster
    # that matches a hand-built group is *evidence the method works* — the game
    # industry came back this way — and one that matches nothing is the
    # interesting case. Either way the reviewer needs to be told which it is.
    async with db.execute(
        """SELECT g.slug, g.name, m.did FROM group_members m
             JOIN groups g ON g.id = m.group_id
            WHERE COALESCE(m.confirmed, 1) = 1"""
    ) as cur:
        existing: dict[str, set[str]] = {}
        names: dict[str, str] = {}
        for row in await cur.fetchall():
            existing.setdefault(row["slug"], set()).add(row["did"])
            names[row["slug"]] = row["name"]

    def overlap_note(members: list[str]) -> str:
        best, share = None, 0.0
        for slug, dids in existing.items():
            hit = len(dids.intersection(members)) / len(members)
            if hit > share:
                best, share = slug, hit
        if best is None or share < 0.3:
            return " Matches no existing circle."
        return (f" {share:.0%} of them are already in {names[best]!r} — "
                f"{'largely a duplicate' if share > 0.7 else 'overlapping but not the same'}.")

    now = utcnow()
    proposed = 0
    by_tier: dict[str, int] = {}
    seen_terms: set[str] = set()
    for members in clusters:
        named = clustering.name_cluster(members, evidence, corpora, total)
        named["why"] += overlap_note(members)
        # A cluster has no natural key, so it is identified by its members.
        term = f"cluster:{hashlib.sha1(','.join(members).encode()).hexdigest()[:16]}"
        if term in seen_terms:
            continue
        seen_terms.add(term)
        label = named["label"] or f"Unnamed community of {len(members)}"
        await db.execute(
            """INSERT INTO group_candidates
                 (kind, term, label, member_count, why, first_seen_at,
                  members, tier)
               VALUES ('cluster',?,?,?,?,?,?,?)
               ON CONFLICT (kind, term) DO UPDATE SET
                 label = excluded.label, member_count = excluded.member_count,
                 why = excluded.why, members = excluded.members,
                 tier = excluded.tier
               WHERE group_candidates.decided IS NULL""",
            (term, label, len(members), named["why"], now,
             json.dumps(members), named["tier"]),
        )
        proposed += 1
        by_tier[named["tier"]] = by_tier.get(named["tier"], 0) + 1
    await db.commit()
    return {"proposed": proposed, "clusters": len(clusters),
            "placeable": placeable, "reached": len(sources_by_did),
            "by_tier": by_tier}


async def group_candidates(decided: bool | None = None,
                           limit: int = 200) -> list[dict]:
    db = await _db()
    clause = "decided IS NULL" if decided is None else "decided = ?"
    params: list = [] if decided is None else [int(decided)]
    async with db.execute(
        f"""SELECT * FROM group_candidates WHERE {clause}
             ORDER BY member_count DESC, label LIMIT ?""",
        (*params, limit),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def decide_candidate(candidate_id: int, accept: bool,
                           dids: list[str] | None = None) -> str | None:
    """Accepting turns a candidate into a real group and populates it.

    `dids` is the subset chosen on the preview page. Passing None keeps the
    original behaviour — take everything the candidate matches — which is what
    the one-click accept on the list page still does.
    """
    db = await _db()
    async with db.execute(
        "SELECT * FROM group_candidates WHERE id = ?", (candidate_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None

    await db.execute(
        "UPDATE group_candidates SET decided = ?, decided_at = ? WHERE id = ?",
        (1 if accept else 0, utcnow(), candidate_id),
    )
    if not accept:
        await db.commit()
        return None

    slug = slugify(row["label"])
    await db.execute(
        "INSERT INTO groups (slug, name, description, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT (slug) DO NOTHING",
        (slug, row["label"], f"Discovered from {row['kind']}: {row['why']}", utcnow()),
    )
    await db.commit()

    if dids is not None:
        # A hand-picked subset. Everyone here was chosen by a person looking at
        # the list, so they are `manual` and confirmed — the same standing as a
        # tag applied anywhere else, and equally safe from the next classify run.
        for did in dids:
            await tag_actor(slug, did)
    elif row["kind"] == "cluster":
        # A cluster is not derived from a term, so there is no rule to re-run:
        # the membership stored when it was proposed *is* the group.
        await _apply_cluster_group(slug, row["members"])
    else:
        await _apply_discovered_group(slug, row["kind"], row["term"])
    return slug


CANDIDATE_SORTABLE = {
    "influence": "a.influence_score",
    "followers": "a.followers_count",
    "handle": "a.handle",
    "name": "a.display_name",
}


async def candidate_members(candidate_id: int, *, order: str = "influence",
                            direction: str = "desc") -> dict | None:
    """Who a candidate would contain if accepted, without accepting it.

    The two kinds arrive at their members differently and the caller should not
    have to care. A cluster IS its stored member list — there is no rule to
    re-run, because it came from the shape of the follow graph. A rule-based
    candidate has no stored members at all and is recomputed here.

    Writes nothing. That is the whole point of a preview, and there is a test
    asserting group_members is untouched after this runs.
    """
    db = await _db()
    async with db.execute(
        "SELECT * FROM group_candidates WHERE id = ?", (candidate_id,)
    ) as cur:
        candidate = await cur.fetchone()
    if candidate is None:
        return None
    candidate = dict(candidate)

    if candidate["kind"] == "cluster":
        try:
            dids = json.loads(candidate["members"] or "[]")
        except (ValueError, TypeError):
            dids = []
        evidence = {d: "clusters with this circle in the follow graph" for d in dids}
    else:
        # Measured at ~1.8s against the live database, nearly all of it inside
        # group_target_dids -> sweep_candidate_dids, which reads every current
        # follower's bio and runs the M18 regex set over it in Python. That cost
        # is pre-existing and invisible inside classify_groups; this is the first
        # thing to put it behind a click. Clusters skip it entirely and render
        # instantly. Not cached, because the preview has to agree exactly with
        # what accepting would do, and a stale target set would break that.
        matched = await _discovered_matches(candidate["kind"], candidate["term"])
        evidence = dict(matched)
        dids = list(evidence)

    members: list[dict] = []
    if dids:
        column = CANDIDATE_SORTABLE.get(order, CANDIDATE_SORTABLE["influence"])
        arrow = "ASC" if direction == "asc" else "DESC"
        placeholders = ",".join("?" for _ in dids)
        async with db.execute(
            f"""SELECT a.did, a.handle, a.display_name, a.avatar_url,
                       a.description,
                       a.followers_count, a.influence_score, a.verified_status,
                       (mf.did IS NOT NULL) AS is_mutual,
                       (fs.did IS NOT NULL AND fs.is_current = 1) AS is_current
                  FROM actors a
                  LEFT JOIN my_follows mf USING (did)
                  LEFT JOIN follower_state fs USING (did)
                 WHERE a.did IN ({placeholders})
                 ORDER BY {column} {arrow} NULLS LAST, a.handle ASC""",
            tuple(dids),
        ) as cur:
            members = [dict(r) for r in await cur.fetchall()]

    tags = await tags_for_many([m["did"] for m in members])
    for member in members:
        member["evidence"] = evidence.get(member["did"], "")
        member["tags"] = tags.get(member["did"], [])

    # A cluster's members were captured when it was found, so some may have
    # departed since. Showing them marked beats dropping them silently: a
    # candidate that is half-departed is information about whether to accept it.
    candidate["departed"] = sum(1 for m in members if not m["is_current"])
    candidate["members_found"] = len(members)
    # DIDs we hold no actor row for at all — they cannot be rendered, and a
    # count that ignored them would not add up.
    candidate["unresolved"] = len(dids) - len(members)
    return {"candidate": candidate, "members": members,
            "order": order, "direction": direction}


async def _apply_cluster_group(slug: str, members_json: str | None) -> int:
    from sonde.groups import PROPAGATION

    db = await _db()
    async with db.execute("SELECT id FROM groups WHERE slug = ? AND archived_at IS NULL", (slug,)) as cur:
        group = await cur.fetchone()
    if group is None:
        return 0
    try:
        members = json.loads(members_json or "[]")
    except (ValueError, TypeError):
        return 0

    now = utcnow()
    for did in members:
        await db.execute(
            """INSERT INTO group_members
                 (group_id, did, tier, confidence, evidence, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (group_id, did) DO NOTHING""",
            (group["id"], did, "cluster", PROPAGATION,
             "clusters with this circle in the follow graph", now),
        )
    await db.commit()
    return len(members)


async def _apply_discovered_group(slug: str, kind: str, term: str) -> int:
    """Populate an accepted group using the rule that proposed it."""
    db = await _db()
    async with db.execute("SELECT id FROM groups WHERE slug = ? AND archived_at IS NULL", (slug,)) as cur:
        group = await cur.fetchone()
    if group is None:
        return 0
    group_id = group["id"]

    matched = await _discovered_matches(kind, term)
    now = utcnow()
    await db.executemany(
        """INSERT INTO group_members
             (group_id, did, tier, confidence, evidence, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT (group_id, did) DO NOTHING""",
        [(group_id, did, "discovered", 0.8, evidence, now) for did, evidence in matched],
    )
    await db.commit()
    return len(matched)


async def _discovered_matches(kind: str, term: str) -> list[tuple[str, str]]:
    """Who a rule-based candidate would contain, and why — writing nothing.

    Split out of `_apply_discovered_group` so the preview and the accept run the
    same matcher. They used to be able to disagree silently: what discovery
    counted when it proposed a candidate and what this rebuilt when the operator
    accepted it were separate code, and nothing compared them.
    """
    db = await _db()
    targets = await group_target_dids(top_n=settings.posts_top_n)
    if not targets:
        return []
    placeholders = ",".join("?" for _ in targets)

    matched: list[tuple[str, str]] = []
    if kind == "occupation":
        async with db.execute(
            f"""SELECT did, wikidata_occupations FROM actors
                 WHERE did IN ({placeholders}) AND wikidata_occupations IS NOT NULL""",
            tuple(targets),
        ) as cur:
            for row in await cur.fetchall():
                if any(term.lower() == o.lower()
                       for o in json.loads(row["wikidata_occupations"] or "[]")):
                    matched.append((row["did"], f"Wikidata occupation: {term}"))
    elif kind == "link":
        async with db.execute(
            f"SELECT did, link_signals FROM actors WHERE did IN ({placeholders}) "
            f"AND link_signals IS NOT NULL",
            tuple(targets),
        ) as cur:
            for row in await cur.fetchall():
                if any(s.get("kind") == term
                       for s in json.loads(row["link_signals"] or "[]")):
                    matched.append((row["did"], f"self-declared {term} link"))
    elif kind == "organisation":
        async with db.execute(
            "SELECT did FROM affiliations WHERE org_name = ? AND kind != 'former' "
            "AND COALESCE(confirmed, 1) = 1", (term,),
        ) as cur:
            matched = [(r["did"], f"currently affiliated with {term}")
                       for r in await cur.fetchall()]
    elif kind == "phrase":
        # Same tokenisation and the same two sources the proposer used. A
        # literal LIKE over bios alone disagreed with it on both counts — see
        # discovery.bigrams for what that cost.
        from sonde import discovery

        texts: dict[str, list[str]] = {}
        async with db.execute(
            f"SELECT did, description FROM actors WHERE did IN ({placeholders}) "
            f"AND description IS NOT NULL AND description != ''",
            tuple(targets),
        ) as cur:
            for row in await cur.fetchall():
                texts.setdefault(row["did"], []).append(row["description"])
        async with db.execute(
            f"SELECT did, text FROM posts WHERE did IN ({placeholders}) "
            f"AND text IS NOT NULL",
            tuple(targets),
        ) as cur:
            for row in await cur.fetchall():
                texts.setdefault(row["did"], []).append(row["text"])

        matched = [
            (did, f"bio or recent posts mention {term!r}")
            for did, documents in texts.items()
            if any(term in discovery.bigrams(document) for document in documents)
        ]

    return matched


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
              JOIN follower_state fs ON fs.did = a.did
             WHERE {' AND '.join(where)} AND COALESCE(a.confirmed, 1) = 1
               AND fs.is_current = 1 AND fs.ignored_at IS NULL
             GROUP BY o.id
            HAVING members + former >= ?
             ORDER BY {column} {arrow} NULLS LAST, o.name ASC""",
        (*params, min_members),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def organisation_members(name: str) -> dict:
    """Everyone affiliated with one organisation, current and former apart.

    Matched `COLLATE NOCASE`. The index links with the stored capitalisation, so
    exact matching works from a click — but `?name=eventbrite` typed by hand
    returned an empty page against a stored "Eventbrite", with nothing to say
    why. A name is an identifier here, not data, so case should not decide it.

    Restricted to current, non-hidden followers, which the summary counts also
    do. Without it a departed follower keeps padding an organisation's roster.
    """
    db = await _db()
    async with db.execute(
        """SELECT act.did, act.handle, act.display_name, act.avatar_url,
                  act.followers_count, act.influence_score, act.verified_status,
                  a.kind, a.role, a.method, a.confidence, a.note, a.source_url,
                  (mf.did IS NOT NULL) AS is_mutual
             FROM affiliations a
             JOIN actors act USING (did)
             JOIN follower_state fs USING (did)
             LEFT JOIN my_follows mf ON mf.did = act.did
            WHERE a.org_name = ? COLLATE NOCASE
              AND COALESCE(a.confirmed, 1) = 1
              AND fs.is_current = 1 AND fs.ignored_at IS NULL
            ORDER BY (a.kind = 'former'), act.influence_score DESC NULLS LAST""",
        (name,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    async with db.execute(
        "SELECT * FROM organisations WHERE name = ? COLLATE NOCASE", (name,)
    ) as cur:
        org = await cur.fetchone()
    return {
        # `found` lets the page say "no such organisation" rather than render an
        # empty, identical-looking shell — which is how this bug presented.
        "found": org is not None or bool(rows),
        "requested": name,
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


# A tag name is a label, not prose. 64 is generous for the chip it renders as.
GROUP_NAME_MAX = 64


def slugify(name: str) -> str:
    """A stable URL key from a display name.

    Extracted from `decide_candidate`, which had this inline, so hand-made and
    discovered tags cannot drift into two different slug conventions.
    """
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:48]


async def create_group(name: str) -> dict:
    """Make a tag, and say what actually happened.

    The caller needs the distinction: an archived slug must offer restore rather
    than silently doing nothing, which is what a bare
    `INSERT ... ON CONFLICT DO NOTHING` would do.
    """
    name = (name or "").strip()
    slug = slugify(name)
    if not name or not slug:
        return {"status": "invalid", "slug": None, "name": name}

    db = await _db()
    async with db.execute(
        "SELECT slug, archived_at FROM groups WHERE slug = ?", (slug,)
    ) as cur:
        existing = await cur.fetchone()
    if existing is not None:
        return {"status": "archived" if existing["archived_at"] else "exists",
                "slug": slug, "name": name}

    await db.execute(
        "INSERT INTO groups (slug, name, created_at) VALUES (?,?,?)",
        (slug, name[:GROUP_NAME_MAX], utcnow()),
    )
    await db.commit()
    return {"status": "created", "slug": slug, "name": name}


async def rename_group(slug: str, name: str) -> bool:
    """Display name only — never the slug. See the module note on create_group."""
    name = (name or "").strip()
    if not name:
        return False
    db = await _db()
    cur = await db.execute(
        "UPDATE groups SET name = ? WHERE slug = ?", (name[:GROUP_NAME_MAX], slug))
    await db.commit()
    return bool(cur.rowcount)


async def archive_group(slug: str, archived: bool = True) -> bool:
    """Delete is archive: memberships are preserved and it can come back."""
    db = await _db()
    cur = await db.execute(
        "UPDATE groups SET archived_at = ? WHERE slug = ?",
        (utcnow() if archived else None, slug))
    await db.commit()
    return bool(cur.rowcount)


async def merge_groups(source_slug: str, target_slug: str) -> dict:
    """Fold one group into another, then archive the source.

    Three rules, in descending order of how much trouble breaking them causes:

    A hand decision on the target outranks the merge. If somebody was
    deliberately untagged from the target, absorbing a group they belong to must
    not quietly put them back — that is the same guarantee the whole tagging
    model rests on, and a merge is not a licence to overrule it.

    The target's own evidence wins where both groups hold a row for the same
    person. Their membership of the target was already decided on its own terms;
    the source's reasoning does not improve it and would overwrite why they are
    there.

    The source is archived, never deleted. Its URL keeps resolving, its
    memberships stay on disk, and `merged_into` records where its people went,
    so a merge you regret is legible rather than a hole.
    """
    if source_slug == target_slug:
        return {"status": "same-group", "moved": 0}

    db = await _db()
    async with db.execute(
        "SELECT slug, id, archived_at FROM groups WHERE slug IN (?,?)",
        (source_slug, target_slug),
    ) as cur:
        found = {r["slug"]: dict(r) for r in await cur.fetchall()}
    source, target = found.get(source_slug), found.get(target_slug)
    if source is None or target is None:
        return {"status": "unknown-group", "moved": 0}
    if target["archived_at"]:
        return {"status": "target-archived", "moved": 0}

    async with db.execute(
        """SELECT did, tier, confidence, evidence, source_url
             FROM group_members
            WHERE group_id = ? AND COALESCE(confirmed, 1) = 1""",
        (source["id"],),
    ) as cur:
        members = [dict(r) for r in await cur.fetchall()]

    now = utcnow()
    moved = 0
    for member in members:
        # DO NOTHING covers both cases the docstring names at once: an existing
        # row on the target is left exactly as it is, whether it says the person
        # is a member or says a human removed them.
        result = await db.execute(
            """INSERT INTO group_members
                 (group_id, did, tier, confidence, evidence, source_url, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (group_id, did) DO NOTHING""",
            (target["id"], member["did"], member["tier"], member["confidence"],
             member["evidence"], member["source_url"], now),
        )
        moved += result.rowcount or 0

    await db.execute(
        "UPDATE groups SET archived_at = ?, merged_into = ? WHERE slug = ?",
        (now, target_slug, source_slug),
    )
    await db.commit()
    return {"status": "merged", "moved": moved,
            "considered": len(members), "target": target_slug}


async def group_overlaps(min_shared: int = 1, limit: int = 40) -> list[dict]:
    """Pairs of live groups sharing members, most contained first.

    Presented as a fact rather than a recommendation, deliberately. High overlap
    is often correct — 81% of novelists are also writers, and a novelist IS a
    writer — so this cannot say which pairs are duplicates. What it can do is
    put the numbers next to each other: game-dev sits 100% inside game-industry
    and is a real duplicate, while bafta-games-game shares only 21% with it
    despite the name, and merging on the name would have destroyed a group the
    rules never found.
    """
    db = await _db()
    async with db.execute(
        """WITH live AS (
               SELECT g.id, g.slug, g.name, m.did
                 FROM groups g
                 JOIN group_members m ON m.group_id = g.id
                WHERE COALESCE(m.confirmed, 1) = 1 AND g.archived_at IS NULL),
           sizes AS (SELECT id, COUNT(*) AS n FROM live GROUP BY id)
           SELECT a.slug AS a_slug, a.name AS a_name, sa.n AS a_n,
                  b.slug AS b_slug, b.name AS b_name, sb.n AS b_n,
                  COUNT(*) AS shared
             FROM live a
             JOIN live b ON a.did = b.did AND a.id < b.id
             JOIN sizes sa ON sa.id = a.id
             JOIN sizes sb ON sb.id = b.id
            GROUP BY a.id, b.id
           HAVING shared >= ?
            ORDER BY (1.0 * shared / MIN(sa.n, sb.n)) DESC, shared DESC
            LIMIT ?""",
        (min_shared, limit),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    for row in rows:
        smaller = min(row["a_n"], row["b_n"])
        row["containment"] = round(100 * row["shared"] / smaller) if smaller else 0
        # Which way round a merge would naturally go: the smaller into the
        # larger, since that is the one that loses its name.
        if row["a_n"] <= row["b_n"]:
            row["source"], row["target"] = row["a_slug"], row["b_slug"]
        else:
            row["source"], row["target"] = row["b_slug"], row["a_slug"]
    return rows


async def archived_groups() -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT g.slug, g.name, g.archived_at, g.merged_into,
                  (SELECT name FROM groups t WHERE t.slug = g.merged_into)
                    AS merged_into_name,
                  COUNT(m.did) AS members
             FROM groups g
             LEFT JOIN group_members m ON m.group_id = g.id
                   AND COALESCE(m.confirmed, 1) = 1
            WHERE g.archived_at IS NOT NULL
            GROUP BY g.id ORDER BY g.archived_at DESC"""
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def group_names() -> list[dict]:
    """Live tags, for the type-to-create datalist."""
    db = await _db()
    async with db.execute(
        "SELECT slug, name FROM groups WHERE archived_at IS NULL ORDER BY name"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def classify_groups() -> dict:
    """Assign the enrichment set to groups from data already stored."""
    from sonde.groups import classify

    await seed_groups()
    db = await _db()
    async with db.execute("SELECT id, slug FROM groups WHERE archived_at IS NULL") as cur:
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
    # Skip archived groups — their members should not be touched by jobs.
    await db.execute("""DELETE FROM group_members WHERE confirmed IS NULL AND tier != 'manual'
        AND group_id IN (SELECT id FROM groups WHERE archived_at IS NULL)""")

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
            WHERE g.archived_at IS NULL
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
        f"""SELECT a.did, a.handle, a.display_name, a.avatar_url,
                   a.description,
                   a.followers_count, a.influence_score, a.verified_status,
                   m.tier, m.confidence, m.evidence, m.confirmed,
                   (mf.did IS NOT NULL) AS is_mutual
              FROM group_members m
              JOIN groups g ON g.id = m.group_id
              JOIN actors a USING (did)
              LEFT JOIN my_follows mf ON mf.did = a.did
             WHERE g.slug = ? AND g.archived_at IS NULL
               AND COALESCE(m.confirmed, 1) = 1
             ORDER BY {column} {arrow} NULLS LAST, a.handle ASC LIMIT ?""",
        (slug, limit),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    # Every circle each member is in, so a row can show and edit them without
    # a trip to the profile. One query for the table, not one per row.
    tags = await tags_for_many([r["did"] for r in rows])
    for row in rows:
        row["tags"] = tags.get(row["did"], [])
    return rows


async def tags_for_many(dids: Sequence[str]) -> dict[str, list[dict]]:
    """Circles for a batch of people, in one query.

    `groups_for` per row is an N+1: fine for one profile, 200 queries for a
    member table and 92 for a candidate preview, which is where it was already
    being used that way.
    """
    if not dids:
        return {}
    db = await _db()
    placeholders = ",".join("?" for _ in dids)
    async with db.execute(
        f"""SELECT m.did, g.slug, g.name, m.tier, m.evidence, m.confidence
              FROM group_members m JOIN groups g ON g.id = m.group_id
             WHERE m.did IN ({placeholders}) AND g.archived_at IS NULL
               AND COALESCE(m.confirmed, 1) = 1
             ORDER BY m.confidence DESC, g.name""",
        tuple(dids),
    ) as cur:
        out: dict[str, list[dict]] = {}
        for row in await cur.fetchall():
            out.setdefault(row["did"], []).append(dict(row))
    return out


async def groups_for(did: str) -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT g.slug, g.name, m.tier, m.confidence, m.evidence, m.confirmed
             FROM group_members m JOIN groups g ON g.id = m.group_id
            WHERE m.did = ? AND g.archived_at IS NULL
              AND COALESCE(m.confirmed, 1) = 1
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


async def tag_actor(slug: str, did: str) -> bool:
    """Hand-tag one person. False if the tag is missing or archived.

    Three cases, and the middle one is the subtle one. Tagging someone the
    machine already proposed *confirms* the existing row rather than replacing
    it: agreeing with a rule is not the same as overruling it, and overwriting
    tier and evidence would discard the reason they are in the group at all.
    """
    from sonde.groups import MANUAL

    db = await _db()
    async with db.execute(
        "SELECT id FROM groups WHERE slug = ? AND archived_at IS NULL", (slug,)
    ) as cur:
        group = await cur.fetchone()
    if group is None:
        return False

    now = utcnow()
    await db.execute(
        """INSERT INTO group_members
             (group_id, did, tier, confidence, evidence, confirmed,
              created_at, decided_at)
           VALUES (?,?,?,?,?,1,?,?)
           ON CONFLICT (group_id, did) DO UPDATE SET
             confirmed = 1, decided_at = excluded.decided_at""",
        (group["id"], did, "manual", MANUAL, "tagged by hand", now, now),
    )
    await db.commit()
    return True


async def untag_actor(slug: str, did: str) -> bool:
    """`confirmed = 0`, whatever the row's origin. False if there was no row.

    Existing rows only. This is undo, not a pre-emptive block: removing a tag
    from people who never had it must not write tombstones, or a rule that
    legitimately matches one of them tomorrow would be silently suppressed
    forever.
    """
    db = await _db()
    cur = await db.execute(
        """UPDATE group_members SET confirmed = 0, decided_at = ?
            WHERE did = ? AND group_id = (
                  SELECT id FROM groups
                   WHERE slug = ? AND archived_at IS NULL)""",
        (utcnow(), did, slug),
    )
    await db.commit()
    return bool(cur.rowcount)


async def tag_actors(slug: str, dids: list[str], *, add: bool) -> int:
    """Batch. Returns the number of rows actually changed, which is what the
    page reports back — 'tagged 30' when 100 were selected is information."""
    changed = 0
    for did in dids:
        if await (tag_actor(slug, did) if add else untag_actor(slug, did)):
            changed += 1
    return changed


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


async def store_affinity_edges(edges: list[tuple[str, str]]) -> int:
    db = await _db()
    await db.execute("DELETE FROM affinity_edges")
    await db.executemany(
        "INSERT INTO affinity_edges (source_did, did) VALUES (?,?) "
        "ON CONFLICT DO NOTHING", edges,
    )
    await db.commit()
    return len(edges)


async def propagate_groups(min_seeds: int = 3, min_lift: float = 1.6,
                           min_shared: int = 3, max_groups_per_person: int = 2,
                           min_overlap: float = 0.25) -> dict:
    """T5 — guilt by association over the follow graph.

    Rules cannot find "civic tech" or "privacy activist": those are communities,
    not job titles, and they are legible in who follows whom long before they
    appear in a bio.

    The naive version — sources that follow many of a group's members — does not
    work here, and the first real run showed why: every index source comes from
    one person's follow graph, so the accounts following journalists also follow
    developers and academics. It proposed 329 memberships with 25 people in five
    or more groups and one in all seven, which is not classification, it is
    "this account is well connected".

    So a source only characterises a group if it follows that group
    *disproportionately* — lift against the baseline rate across all
    candidates. And a person is proposed for at most a couple of groups, best
    overlap first, because being proposed for seven means nothing.
    """
    from sonde.groups import PROPAGATION

    db = await _db()
    async with db.execute("SELECT COUNT(*) FROM affinity_edges") as cur:
        if not (await cur.fetchone())[0]:
            return {"skipped": "no follow-graph edges — rebuild the affinity index",
                    "proposed": 0}

    async with db.execute("SELECT did, source_did FROM affinity_edges") as cur:
        sources_of: dict[str, set[str]] = {}
        for row in await cur.fetchall():
            sources_of.setdefault(row["did"], set()).add(row["source_did"])

    async with db.execute(
        """SELECT g.id, g.slug, m.did FROM group_members m
             JOIN groups g ON g.id = m.group_id
            WHERE COALESCE(m.confirmed, 1) = 1 AND m.tier != 'propagation'
              AND g.archived_at IS NULL"""
    ) as cur:
        members: dict[str, tuple[int, set[str]]] = {}
        for row in await cur.fetchall():
            entry = members.setdefault(row["slug"], (row["id"], set()))
            entry[1].add(row["did"])

    candidates = [d for d in await group_target_dids(top_n=settings.posts_top_n)
                  if sources_of.get(d)]
    if not candidates:
        return {"proposed": 0, "skipped": "no candidates in the follow graph"}

    # Baseline: how often each source follows anyone at all. A source following
    # half the network characterises nothing.
    baseline: dict[str, int] = {}
    for did in candidates:
        for source in sources_of[did]:
            baseline[source] = baseline.get(source, 0) + 1
    total = len(candidates)

    # Best proposals per person, resolved after scoring every group.
    proposals: dict[str, list[tuple[float, int, str, str]]] = {}

    for slug, (group_id, seed_dids) in members.items():
        seeds = [sources_of[d] for d in seed_dids if sources_of.get(d)]
        if len(seeds) < min_seeds:
            continue
        seed_counts: dict[str, int] = {}
        for source_set in seeds:
            for source in source_set:
                seed_counts[source] = seed_counts.get(source, 0) + 1

        signature = set()
        for source, count in seed_counts.items():
            in_group = count / len(seeds)
            overall = baseline.get(source, 0) / total
            if overall and in_group / overall >= min_lift and in_group >= min_overlap:
                signature.add(source)
        if len(signature) < min_shared:
            continue

        for did in candidates:
            if did in seed_dids:
                continue
            shared = len(sources_of[did] & signature)
            if shared < min_shared:
                continue
            overlap = shared / len(signature)
            if overlap < min_overlap:
                continue
            proposals.setdefault(did, []).append((overlap, group_id, slug, (
                f"followed by {shared} of the {len(signature)} accounts that "
                f"distinctively follow this group")))

    now = utcnow()
    proposed = 0
    by_group: dict[str, int] = {}
    for did, options in proposals.items():
        options.sort(reverse=True)
        for overlap, group_id, slug, evidence in options[:max_groups_per_person]:
            await db.execute(
                """INSERT INTO group_members
                     (group_id, did, tier, confidence, evidence, created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT (group_id, did) DO NOTHING""",
                (group_id, did, "propagation", round(PROPAGATION * overlap, 3),
                 evidence, now),
            )
            proposed += 1
            by_group[slug] = by_group.get(slug, 0) + 1

    await db.commit()
    return {"proposed": proposed, "considered": len(proposals),
            "by_group": dict(sorted(by_group.items(), key=lambda kv: -kv[1]))}


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


# Someone arriving, by either route. `followed_back` is sonde's own write and
# `handle_changed` is a rename, so neither is an arrival.
ARRIVALS = ("followed", "returned")


async def recent_changes(limit: int = 100,
                         event: str | tuple[str, ...] | None = None) -> list[dict]:
    """Recent follow events, newest first.

    `event` takes one kind or several — the dashboard wants arrivals only while
    /changes wants whatever the operator picked from its filter.
    """
    db = await _db()
    sql = (
        "SELECT e.*, a.handle, a.display_name, a.avatar_url, "
        "a.followers_count, a.verified_status "
        "FROM follow_events e LEFT JOIN actors a USING (did) "
    )
    params: list = []
    if event:
        kinds = (event,) if isinstance(event, str) else tuple(event)
        sql += f"WHERE e.event IN ({','.join('?' for _ in kinds)}) "
        params.extend(kinds)
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


async def backup_attempts() -> int:
    """How many times the backup job has run, successfully or not."""
    return await _scalar("SELECT COUNT(*) FROM sync_runs WHERE kind = 'backup'") or 0


async def _backup_notice() -> dict | None:
    """Warn when the nightly snapshot is failing or has stalled.

    This exists because it happened. The bind mount at /backup keeps the host's
    ownership, the container runs as uid 10001, and every VACUUM INTO since
    deployment failed with "unable to open database". Each failure was dutifully
    written to sync_runs and read by nobody, while /settings said "No snapshot
    has been taken yet" — which reads as *not yet* rather than *never, and never
    will*. follow_events is the one table that cannot be re-fetched from
    Bluesky, so a silent backup failure is the most expensive kind there is.

    The signature carries the day, so dismissing tonight's failure does not
    silence tomorrow's. A persistent data-loss risk must not be dismissible once
    and forgotten.
    """
    import hashlib

    db = await _db()
    async with db.execute(
        "SELECT status, started_at, error FROM sync_runs WHERE kind = 'backup' "
        "ORDER BY started_at DESC LIMIT 1"
    ) as cur:
        latest = await cur.fetchone()
    if latest is None:
        # Never attempted. A fresh install is not a fault; /settings says so.
        return None

    last_ok = await get_meta("last_backup_at")
    problem = detail = None

    if latest["status"] == "failed":
        problem = "The nightly database snapshot is failing."
        detail = (latest["error"] or "no error recorded").strip()
        if not last_ok:
            problem = "The nightly database snapshot has never once succeeded."
    elif last_ok:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last_ok)).total_seconds() / 3600
        except ValueError:
            age = None
        if age is not None and age > 48:
            problem = f"No database snapshot for {age / 24:.0f} days."
            detail = "The backup job is scheduled nightly, so it has stalled."

    if problem is None:
        return None

    day = (latest["started_at"] or "")[:10]
    return {
        "kind": "backup_failing",
        "signature": hashlib.sha256(f"{day}|{detail}".encode()).hexdigest()[:16],
        "summary": problem,
        "detail": (
            f"{detail} The event history is the only data here that cannot be "
            f"re-fetched from Bluesky, and it is what these snapshots exist to "
            f"protect. Run 'Backup now' on /settings once the cause is fixed."
        ),
        "subjects": [],
    }


async def active_notices(kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Warnings not currently dismissed.

    The signature is the set of subjects, so dismissing "2 accounts" does not
    also silence a later "5 accounts" — the warning returns when what it is
    about actually changes.

    `kinds` filters by notice kind: the dashboard wants the operational ones,
    /verified wants the ones about verification.
    """
    import hashlib

    notices = []
    backup = await _backup_notice()
    if backup:
        notices.append(backup)

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

    if kinds is not None:
        notices = [n for n in notices if n["kind"] in kinds]

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
        # Arrivals only. Departures still exist and are still recorded; they
        # live on /changes, which is where you go to look for them.
        "recent_changes": await recent_changes(12, event=ARRIVALS),
        "top": await ranked_followers(limit=10),
    }
