"""Tier 2 — my own follow list.

Cheap (46 calls) and it does double duty: mutual detection, and the candidate
pool the affinity index draws its sources from.
"""

from __future__ import annotations

import logging

from sonde.api.client import BlueskyClient
from sonde.api.graph import iter_follows
from sonde.config import settings
from sonde.db import store

log = logging.getLogger("sonde.mutuals")


async def sync_follows(client: BlueskyClient | None = None) -> dict:
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("follows")
    pages = seen = 0
    try:
        dids: list[str] = []
        async for page, actors in iter_follows(client, settings.actor):
            pages = page
            for a in actors:
                if a.get("did"):
                    dids.append(a["did"])
                    # Store the profile too: these are affinity-index candidates
                    # and we need their followsCount to pick sources later.
                    await store.upsert_actor(a)
            seen += len(actors)
        await store.replace_my_follows(dids)
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc), api_calls=client.calls)
        log.exception("follows sync failed")
        raise
    finally:
        if owns_client:
            await client.aclose()

    mutuals = await store.mutual_count()
    await store.finish_run(
        run_id, status="ok", completed=1, pages_fetched=pages,
        actors_seen=seen, api_calls=client.calls,
    )
    return {"status": "ok", "kind": "follows", "follows": seen,
            "mutuals": mutuals, "api_calls": client.calls}
