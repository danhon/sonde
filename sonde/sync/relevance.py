"""Tier 3b — exact relevance via `getKnownFollowers`.

Answers directly what the sampled index approximates: how many accounts I follow
also follow this person. Requires auth, and costs one or more calls per actor,
so it runs over the top slice rather than all 10,042.

Two things shape the implementation:

**The endpoint returns no total.** It returns a paginated array of profiles, so
an exact count means walking pages. Since the ceiling that counts as maximum
affinity is 250, paging stops once that is passed — "at least 250" and "310"
score identically, so paying for the difference is waste. That caps each actor
at three calls.

**It is a cross-check, not a replacement.** The sampled index still covers all
10,042 followers; this refines the top of the ranking and tells us whether the
sample is any good. Where the two disagree badly, the sample size is wrong —
which is a fact worth surfacing rather than hiding.
"""

from __future__ import annotations

import logging

from sonde.api.auth import authenticator
from sonde.api.client import BlueskyClient
from sonde.config import settings
from sonde.db import store
from sonde.jobs import registry
from sonde.scoring import AFFINITY_EXACT_FULL

log = logging.getLogger("sonde.relevance")

PAGE_SIZE = 100
# Beyond the saturation ceiling the score is identical, so stop counting.
MAX_PAGES = -(-AFFINITY_EXACT_FULL // PAGE_SIZE) + 1


async def known_followers(client: BlueskyClient, did: str) -> tuple[int, list[str]]:
    """Who you and this actor both know: the count, and who they are.

    The count is capped at the point where more stops changing the score, so
    the DIDs are capped with it — this is the same walk, not a second one.

    The profiles were always being fetched and thrown away; only the length of
    the array was kept. Returning them costs nothing and is the difference
    between "you share 31 people" and being able to see which.
    """
    total = 0
    dids: list[str] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        params: dict[str, object] = {"actor": did, "limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        data = await client.xrpc("app.bsky.graph.getKnownFollowers", params, authed=True)
        followers = data.get("followers") or []
        total += len(followers)
        dids.extend(f["did"] for f in followers if f.get("did"))
        cursor = data.get("cursor")
        if not cursor or total >= AFFINITY_EXACT_FULL:
            break
    return total, dids


async def known_follower_count(client: BlueskyClient, did: str) -> int | None:
    """Just the count. Kept for callers that only score."""
    total, _ = await known_followers(client, did)
    return total


async def enrich(client: BlueskyClient | None = None, *, limit: int | None = None) -> dict:
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("relevance")
    limit = limit if limit is not None else settings.relevance_top_n

    # Without a session this endpoint is a 401 for every actor. Fail the run
    # loudly rather than burning calls to learn that N times.
    if not await authenticator.token():
        await store.finish_run(
            run_id, status="ok", completed=1, api_calls=0,
            error="no Bluesky session; exact relevance skipped",
        )
        if owns_client:
            await client.aclose()
        log.info("exact relevance skipped — needs an app password")
        return {"status": "ok", "kind": "relevance", "enriched": 0,
                "skipped": "not authenticated", "api_calls": 0}

    enriched = failed = 0
    try:
        targets = await store.relevance_targets(limit=limit)
        if not targets:
            await store.finish_run(run_id, status="ok", completed=1, api_calls=0)
            return {"status": "ok", "kind": "relevance", "enriched": 0, "api_calls": 0}

        log.info("fetching exact known-follower counts for %d actor(s)", len(targets))
        for index, did in enumerate(targets, 1):
            registry.progress("relevance", index, len(targets), "accounts",
                              f"{enriched:,} counted")
            try:
                count, known = await known_followers(client, did)
            except Exception:
                failed += 1
                log.debug("known followers unavailable for %s", did, exc_info=True)
                continue
            await store.record_exact_affinity(did, count)
            # The same page walk already had these. Storing them is what turns
            # a number on a profile into a list you can read.
            await store.save_known_followers(did, known)
            enriched += 1
            if index % 25 == 0:
                await store.record_progress(run_id, actors=enriched, calls=client.calls)

        await store.rescore_all()
        await store.commit()
        agreement = await store.affinity_agreement()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc),
                               api_calls=client.calls)
        log.exception("relevance enrichment failed")
        raise
    finally:
        if owns_client:
            await client.aclose()

    await store.finish_run(run_id, status="ok", completed=1, actors_seen=enriched,
                           api_calls=client.calls)
    return {"status": "ok", "kind": "relevance", "enriched": enriched,
            "failed": failed, "agreement": agreement, "api_calls": client.calls}
