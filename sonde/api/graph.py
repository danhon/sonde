"""Graph and actor queries.

Two API behaviours drive everything here, both measured against the live API on
2026-07-26 rather than assumed:

1. Pages come back SHORT. `getFollowers` at limit=100 returned a mean of 87.3
   actors, and only 5 of 115 pages were full, because the AppView drops
   deactivated / suspended / blocked accounts *after* selecting 100 follow
   records. Stopping when a page is under-full ends the sweep on page 1 of 115.
   The cursor is the only end-of-list signal.

2. `getProfiles` SILENTLY OMITS actors it cannot resolve — HTTP 200, shorter
   array, no error. Results must be mapped back by DID; zipping request against
   response assigns follower counts to the wrong people with no error anywhere.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from sonde.api.client import BlueskyClient
from sonde.config import settings

log = logging.getLogger("sonde.graph")


async def iter_followers(
    client: BlueskyClient, actor: str, *, limit: int | None = None
) -> AsyncIterator[tuple[int, list[dict]]]:
    """Yield (page_number, actors) newest-follow-first.

    Terminates only when the API stops returning a cursor. `limit` caps the
    number of *pages*, which is how the head sweep stays cheap.
    """
    cursor: str | None = None
    page = 0
    while True:
        params: dict[str, object] = {"actor": actor, "limit": settings.page_size}
        if cursor:
            params["cursor"] = cursor
        # authed=True adds viewer state, which carries the AT-URI of each
        # follower's follow of us — the rkey decodes to an exact follow date.
        # Without a session this silently returns the same public payload.
        data = await client.xrpc("app.bsky.graph.getFollowers", params, authed=True)
        page += 1
        yield page, data.get("followers", [])

        cursor = data.get("cursor")
        if not cursor:
            return  # the ONLY valid end-of-list condition
        if limit is not None and page >= limit:
            return


async def iter_follows(
    client: BlueskyClient, actor: str
) -> AsyncIterator[tuple[int, list[dict]]]:
    cursor: str | None = None
    page = 0
    while True:
        params: dict[str, object] = {"actor": actor, "limit": settings.page_size}
        if cursor:
            params["cursor"] = cursor
        data = await client.xrpc("app.bsky.graph.getFollows", params)
        page += 1
        yield page, data.get("follows", [])
        cursor = data.get("cursor")
        if not cursor:
            return


async def get_profile(client: BlueskyClient, actor: str) -> dict:
    return await client.xrpc("app.bsky.actor.getProfile", {"actor": actor})


async def get_profiles(
    client: BlueskyClient, dids: list[str]
) -> tuple[dict[str, dict], list[str]]:
    """Hydrate up to 25 actors. Returns (by_did, missing_dids).

    Mapping is by DID, never by position — see module docstring. The missing
    set is meaningful: those accounts are unservable, which is what separates
    "gone" from "unfollowed".
    """
    if not dids:
        return {}, []
    if len(dids) > settings.profiles_batch:
        raise ValueError(
            f"getProfiles accepts at most {settings.profiles_batch} actors, got {len(dids)}"
        )
    data = await client.xrpc("app.bsky.actor.getProfiles", {"actors": dids})
    by_did = {p["did"]: p for p in data.get("profiles", []) if p.get("did")}
    missing = [d for d in dids if d not in by_did]
    if missing:
        log.debug("getProfiles omitted %d of %d actors", len(missing), len(dids))
    return by_did, missing


def batched(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
