"""Capturing interactions, inbound and outbound.

Reconstructing this by walking my 23,602 posts and asking who liked, reposted
and quoted each would be ~94,000 calls and about nine hours.
`listNotifications` returns every inbound interaction — author, reason, which
post, when — at 100 per call, so 50,000 notifications is 500 calls and three
minutes. Roughly 190x cheaper for strictly more information.

Outbound is nearly free: my own author feed already carries my replies (the
reply parent names who I replied to), my reposts and my quote posts.

**Interactions are stored append-only as they are observed.** The notification
API's retention window is finite and undocumented, so sonde accumulates its own
history and the score improves the longer it runs — the same reasoning that
makes `follow_events` the one irreplaceable table. Incremental runs walk back
only to the newest interaction already stored.
"""

from __future__ import annotations

import logging

from sonde.api.auth import authenticator
from sonde.api.client import BlueskyClient
from sonde.config import settings
from sonde.db import store
from sonde.jobs import registry

log = logging.getLogger("sonde.interactions")

PAGE = 100
# Reasons that say something about a relationship. `follow` is already tracked
# by the sweep, and starterpack/verified notifications are not interactions.
INTERESTING = {"like", "repost", "reply", "quote", "mention"}


def _thread_of(record: dict) -> str | None:
    reply = record.get("reply") or {}
    root = reply.get("root") or {}
    return root.get("uri") or reply.get("parent", {}).get("uri")


async def ingest_notifications(client: BlueskyClient, *, max_pages: int,
                               stop_at: str | None) -> tuple[list[dict], int]:
    """Walk notifications newest-first, stopping at what we already have."""
    rows: list[dict] = []
    cursor: str | None = None
    pages = 0
    while pages < max_pages:
        params: dict[str, object] = {"limit": PAGE}
        if cursor:
            params["cursor"] = cursor
        data = await client.xrpc(
            "app.bsky.notification.listNotifications", params, authed=True
        )
        pages += 1
        notifications = data.get("notifications") or []
        if not notifications:
            break

        for note in notifications:
            when = note.get("indexedAt") or ""
            if stop_at and when <= stop_at:
                # Caught up with what is already stored.
                return rows, pages
            reason = note.get("reason")
            if reason not in INTERESTING:
                continue
            author = (note.get("author") or {}).get("did")
            if not author:
                continue
            record = note.get("record") or {}
            rows.append({
                "did": author, "direction": "inbound", "kind": reason,
                "uri": note.get("uri"),
                "subject": note.get("reasonSubject"),
                "thread": _thread_of(record) or note.get("reasonSubject"),
                "occurred_at": when,
            })

        cursor = data.get("cursor")
        if not cursor:
            break
    return rows, pages


async def ingest_outbound(client: BlueskyClient, *, max_pages: int,
                          stop_at: str | None) -> tuple[list[dict], int]:
    """My replies, reposts and quote posts, from my own feed."""
    rows: list[dict] = []
    cursor: str | None = None
    pages = 0
    while pages < max_pages:
        params: dict[str, object] = {
            "actor": settings.actor, "limit": PAGE,
            "filter": "posts_with_replies",
        }
        if cursor:
            params["cursor"] = cursor
        data = await client.xrpc("app.bsky.feed.getAuthorFeed", params)
        pages += 1
        feed = data.get("feed") or []
        if not feed:
            break

        for item in feed:
            post = item.get("post") or {}
            when = post.get("indexedAt") or ""
            if stop_at and when <= stop_at:
                return rows, pages
            record = post.get("record") or {}

            # A repost of someone else's post.
            reason = item.get("reason") or {}
            if reason.get("$type", "").endswith("reasonRepost"):
                other = (post.get("author") or {}).get("did")
                if other and other != post.get("author", {}).get("did", other):
                    pass
                if other:
                    rows.append({
                        "did": other, "direction": "outbound", "kind": "repost",
                        "uri": post.get("uri"), "subject": post.get("uri"),
                        "thread": post.get("uri"), "occurred_at": when,
                    })
                continue

            # A reply of mine: the parent's author is who I replied to.
            reply = item.get("reply") or {}
            parent_author = ((reply.get("parent") or {}).get("author") or {}).get("did")
            if parent_author:
                rows.append({
                    "did": parent_author, "direction": "outbound", "kind": "reply",
                    "uri": post.get("uri"),
                    "subject": (reply.get("parent") or {}).get("uri"),
                    "thread": _thread_of(record)
                              or (reply.get("root") or {}).get("uri"),
                    "occurred_at": when,
                })

            # A quote post of mine.
            embed = record.get("embed") or {}
            quoted = (embed.get("record") or {}).get("uri") or \
                     ((embed.get("record") or {}).get("record") or {}).get("uri")
            if quoted and quoted.startswith("at://"):
                rows.append({
                    "did": quoted.split("/")[2], "direction": "outbound",
                    "kind": "quote", "uri": post.get("uri"), "subject": quoted,
                    "thread": quoted, "occurred_at": when,
                })

        cursor = data.get("cursor")
        if not cursor:
            break
    return rows, pages


async def sync(client: BlueskyClient | None = None, *,
               max_pages: int | None = None, full: bool = False) -> dict:
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("interactions")
    max_pages = max_pages if max_pages is not None else settings.interaction_max_pages

    if not await authenticator.token():
        await store.finish_run(
            run_id, status="ok", completed=1, api_calls=0,
            error="no Bluesky session; notifications need auth",
        )
        if owns_client:
            await client.aclose()
        return {"status": "ok", "kind": "interactions", "stored": 0,
                "skipped": "not authenticated", "api_calls": 0}

    stored = 0
    try:
        # Incremental by default: walk back only to what we already hold.
        stop_at = None if full else await store.latest_interaction_at()
        registry.progress("interactions", 0, 2, "sources", "notifications")
        inbound, in_pages = await ingest_notifications(
            client, max_pages=max_pages, stop_at=stop_at)

        registry.progress("interactions", 1, 2, "sources", "my own feed")
        outbound, out_pages = await ingest_outbound(
            client, max_pages=max_pages, stop_at=stop_at)

        stored = await store.record_interactions(inbound + outbound)
        await store.score_relationships()
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc),
                               api_calls=client.calls)
        log.exception("interaction sync failed")
        raise
    finally:
        if owns_client:
            await client.aclose()

    await store.finish_run(run_id, status="ok", completed=1, actors_seen=stored,
                           pages_fetched=in_pages + out_pages, api_calls=client.calls)
    return {"status": "ok", "kind": "interactions", "stored": stored,
            "inbound": len(inbound), "outbound": len(outbound),
            "pages": in_pages + out_pages, "api_calls": client.calls}
