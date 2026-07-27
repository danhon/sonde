"""Curated moderation lists as a first-pass filter.

skywatch.blue publishes 17 public modlists. Followers appearing on an enabled
list are hidden from listings — never deleted, still swept, still counted.

Two things this deliberately gets right, because a moderation filter that
cannot be argued with is worse than none:

**Your decisions outrank the automation, in both directions.** Un-hide someone
the list flagged and they stay un-hidden; hide someone yourself and no list
refresh restores them. `ignore_locked` records that a human decided, and the
automated pass skips those rows entirely.

**Every hide names its list.** The detail page shows which list flagged an
account, so "why is this person hidden" always has an answer.

Worth knowing about the lists themselves: they are not homogeneous. Some are
abuse ("Spam", "Follow Farming", "Platform Abuse & Manipulation"); others are
political or affiliational ("MAGA", "Elon Musk", "Hammer & Sickle"). Applying
all of them is a defensible default and it is also a content decision, so each
list can be switched off individually on /settings.
"""

from __future__ import annotations

import logging

from sonde.api.client import BlueskyClient
from sonde.config import settings
from sonde.db import store
from sonde.jobs import registry

log = logging.getLogger("sonde.moderation")

# Cap per list so one enormous list cannot dominate a run.
MAX_PAGES_PER_LIST = 200


async def _list_members(client: BlueskyClient, uri: str) -> tuple[list[str], bool]:
    members: list[str] = []
    cursor: str | None = None
    pages = 0
    while pages < MAX_PAGES_PER_LIST:
        params: dict[str, object] = {"list": uri, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = await client.xrpc("app.bsky.graph.getList", params)
        pages += 1
        for item in data.get("items", []) or []:
            did = (item.get("subject") or {}).get("did")
            if did:
                members.append(did)
        cursor = data.get("cursor")
        if not cursor:
            return members, False
    return members, True  # truncated


async def sync_lists(client: BlueskyClient | None = None,
                     curators: list[str] | None = None) -> dict:
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("moderation")
    curators = curators or settings.moderation_curators
    synced = truncated = 0
    total_members = 0

    try:
        for curator in curators:
            try:
                catalogue = await client.xrpc(
                    "app.bsky.graph.getLists", {"actor": curator, "limit": 100}
                )
            except Exception:
                log.warning("could not read lists for %s", curator, exc_info=True)
                continue

            lists = catalogue.get("lists", []) or []
            for index, meta in enumerate(lists, 1):
                registry.progress("moderation", index, len(lists), "lists",
                                  meta.get("name", ""))
                try:
                    members, was_truncated = await _list_members(client, meta["uri"])
                except Exception:
                    log.warning("could not read list %s", meta.get("name"), exc_info=True)
                    continue
                if was_truncated:
                    truncated += 1
                    log.warning("list %r hit the %d-page cap; membership is partial",
                                meta.get("name"), MAX_PAGES_PER_LIST)
                total_members += await store.save_moderation_list(
                    {
                        "uri": meta["uri"],
                        "curator": curator,
                        "name": meta.get("name", "untitled"),
                        "purpose": (meta.get("purpose") or "").split("#")[-1],
                        "description": meta.get("description"),
                    },
                    members,
                )
                synced += 1

        result = await store.apply_moderation_hides()
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc),
                               api_calls=client.calls)
        log.exception("moderation sync failed")
        raise
    finally:
        if owns_client:
            await client.aclose()

    await store.finish_run(run_id, status="ok", completed=1,
                           actors_seen=total_members, api_calls=client.calls)
    log.info("moderation: %d list(s), %d hidden, %d restored, %d left alone (yours)",
             synced, result["hidden"], result["restored"], result["locked_skipped"])
    return {"status": "ok", "kind": "moderation", "lists": synced,
            "truncated_lists": truncated, "members": total_members,
            **result, "api_calls": client.calls}
