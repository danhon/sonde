"""The light notifications check, meant to run after each head sweep.

The 6-hourly interactions sync walks notifications too, but six hours is too
late to tell the operator that someone notable liked a post. This reads only
what arrived since it last looked, stores it, and leaves every recent like or
repost author with current verification and a follower count, so whoever
judges notability next has what it needs.

Three rules:

- **The marker moves only after the rows are stored.** A check that fails
  partway stores nothing and leaves the marker, so the next one reads the
  same notifications again; the table's unique key absorbs the overlap.
- **It never calls `updateSeen`.** Reading must not change what the operator
  sees as unread in the Bluesky app.
- **Authors get an `actors` row, never a `follower_state` row.** A stranger
  who likes a post is someone sonde knows about, not a follower.
"""

from __future__ import annotations

import logging

from sonde.api.client import BlueskyClient
from sonde.api.graph import batched, get_profiles
from sonde.config import settings
from sonde.db import store
from sonde.sync.interactions import INBOUND_SYNCED_KEY, ingest_notifications

log = logging.getLogger("sonde.notifications")

# Where the last successful check stopped: the newest `indexedAt` it stored.
MARKER_KEY = "notifications_checked_to"

# How far back a like or repost still has its author's count refreshed. It
# matches the 48-hour alert lookback, so an author whose lookup failed is
# retried on later checks for as long as the like could still be alerted.
REFRESH_WINDOW_HOURS = 48


async def check_notifications(client: BlueskyClient) -> dict:
    """Read new notifications, store them, and refresh their authors' counts.

    Never raises for an API failure: it returns `status: "failed"` with the
    marker untouched, so a caller can carry on with what is already stored.
    """
    marker = await store.get_meta(MARKER_KEY)
    authors: dict[str, dict] = {}
    try:
        rows, pages = await ingest_notifications(
            client, max_pages=settings.notification_check_max_pages,
            stop_at=marker, authors=authors)
    except Exception as exc:  # noqa: BLE001
        log.warning("notifications check failed: %s", exc)
        return {"status": "failed", "error": str(exc), "stored": 0, "refreshed": 0}

    # A database from before this check: the sync has no marker of its own yet
    # and would fall back to the table's newest inbound row, which this check
    # is about to move. Pin where the sync really stopped first.
    if await store.get_meta(INBOUND_SYNCED_KEY) is None:
        synced = await store.latest_interaction_at("inbound")
        if synced:
            await store.set_meta(INBOUND_SYNCED_KEY, synced)

    for profile in authors.values():
        await store.upsert_actor(profile)
    stored = await store.record_interactions(rows)
    await store.commit()
    if rows:
        await store.set_meta(MARKER_KEY, max(r["occurred_at"] for r in rows))

    # After the marker on purpose: a getProfiles failure must not make the
    # next check re-read notifications. The lookup is driven from the table,
    # so a failed one is simply retried next time.
    refreshed, refresh_error = await _refresh_counts(client)
    result = {"status": "ok", "stored": stored, "pages": pages,
              "refreshed": refreshed}
    if refresh_error:
        result["refresh_error"] = refresh_error
    return result


async def _refresh_counts(client: BlueskyClient) -> tuple[int, str | None]:
    dids = await store.authors_needing_counts(
        since_hours=REFRESH_WINDOW_HOURS,
        max_age_days=settings.follower_count_max_age_days)
    refreshed = 0
    try:
        for batch in batched(dids, settings.profiles_batch):
            found, _missing = await get_profiles(client, batch)
            for did, profile in found.items():
                await store.apply_detailed_profile(did, profile)
                refreshed += 1
            await store.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("follower count refresh failed after %d: %s", refreshed, exc)
        await store.commit()
        return refreshed, str(exc)
    return refreshed, None
