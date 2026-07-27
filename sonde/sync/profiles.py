"""Tier 1 — profile hydration.

`followersCount` exists only on profileViewDetailed, so the follower sweep can't
supply it. This pass fills it in, 25 actors per call, newest followers first and
everyone else on a TTL.

The critical rule: getProfiles returns HTTP 200 with unresolvable actors
SILENTLY OMITTED. Results are keyed by DID (see api.graph.get_profiles); the
omitted set is recorded as `unservable_since`, which is what distinguishes an
account that vanished from one that unfollowed.
"""

from __future__ import annotations

import logging

from sonde.api.client import BlueskyClient
from sonde.api.graph import batched, get_profiles
from sonde.config import settings
from sonde.db import store

log = logging.getLogger("sonde.hydrate")


async def hydrate(
    client: BlueskyClient | None = None, *, limit: int | None = None
) -> dict:
    """Hydrate stale profiles. `limit` caps how many actors are refreshed."""
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("hydrate")

    hydrated = unservable = 0
    status = "ok"
    try:
        dids = await store.stale_actor_dids(limit=limit)
        if not dids:
            await store.finish_run(run_id, status="ok", completed=1, api_calls=0)
            return {"status": "ok", "kind": "hydrate", "hydrated": 0,
                    "unservable": 0, "api_calls": 0}

        log.info("hydrating %d actors in %d batches",
                 len(dids), -(-len(dids) // settings.profiles_batch))

        for batch in batched(dids, settings.profiles_batch):
            by_did, missing = await get_profiles(client, batch)
            for did, profile in by_did.items():
                await store.apply_detailed_profile(did, profile)
            hydrated += len(by_did)
            if missing:
                # Requested but not returned: deactivated, deleted, suspended or
                # blocking. That absence is the cleanest "gone" marker available.
                await store.mark_unservable(missing)
                unservable += len(missing)

        # Hydration can switch a whole component on for the first time (the
        # first follower counts make reach and selectivity live), so the corpus
        # has to be rescored against the new active set rather than left with a
        # mix of denominators.
        if hydrated:
            await store.rescore_all()
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc),
                               profiles_hydrated=hydrated, api_calls=client.calls)
        log.exception("hydration failed")
        raise
    finally:
        if owns_client:
            await client.aclose()

    await store.finish_run(run_id, status=status, completed=1,
                           profiles_hydrated=hydrated, api_calls=client.calls)
    return {"status": status, "kind": "hydrate", "hydrated": hydrated,
            "unservable": unservable, "api_calls": client.calls}


async def rescore() -> dict:
    """Recompute every score. Runs after hydration and on a weights change."""
    run_id = await store.start_run("rescore")
    try:
        count = await store.rescore_all()
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc))
        raise
    await store.finish_run(run_id, status="ok", completed=1, actors_seen=count)
    return {"status": "ok", "kind": "rescore", "scored": count}
