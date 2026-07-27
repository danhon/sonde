"""Recent posts per follower.

`getAuthorFeed` is one call per actor and there is no bulk equivalent, so
fetching three posts for all 10,042 followers on every run would be 40,168
calls a day on an IP shared with two other apps. Affordable against the quota,
not against good manners.

Tiered instead: the top 500 by influence, every verified follower, and recent
arrivals refresh on each full sweep; everyone else rolls round once a day. That
is ~12,400 calls/day — 1.4% of the budget — and every follower still gets three
recent posts refreshed daily.

Posts do double duty. They retire the lifetime-average liveness proxy (which
flattered accounts that died years ago), and their text is the strongest free
signal for grouping.
"""

from __future__ import annotations

import logging

from sonde.api.client import BlueskyClient
from sonde.config import settings
from sonde.db import store
from sonde.jobs import registry

log = logging.getLogger("sonde.posts")

KEEP_PER_FOLLOWER = 3


def _parse_feed(payload: dict) -> list[dict]:
    out: list[dict] = []
    for item in payload.get("feed", []) or []:
        post = item.get("post") or {}
        record = post.get("record") or {}
        uri = post.get("uri")
        if not uri:
            continue
        out.append({
            "uri": uri,
            "text": record.get("text"),
            "indexed_at": post.get("indexedAt"),
            "like_count": post.get("likeCount"),
            "repost_count": post.get("repostCount"),
            "reply_count": post.get("replyCount"),
            # `reason` on the feed item means this appeared because they
            # reposted it, not because they wrote it.
            "is_repost": "reason" in item,
        })
    return out[:KEEP_PER_FOLLOWER]


async def fetch_posts(
    client: BlueskyClient | None = None, *, limit: int | None = None,
    ttl_hours: int | None = None,
) -> dict:
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("posts")
    limit = limit if limit is not None else settings.posts_per_run
    fetched = empty = failed = 0

    try:
        targets = await store.post_targets(ttl_hours=ttl_hours if ttl_hours is not None
                                           else settings.posts_ttl_hours)
        targets = targets[:limit]
        if not targets:
            await store.finish_run(run_id, status="ok", completed=1, api_calls=0)
            return {"status": "ok", "kind": "posts", "fetched": 0, "api_calls": 0}

        log.info("fetching posts for %d follower(s)", len(targets))
        for index, did in enumerate(targets, 1):
            registry.progress("posts", index, len(targets), "accounts",
                              f"{fetched:,} fetched")
            try:
                data = await client.xrpc(
                    "app.bsky.feed.getAuthorFeed",
                    {"actor": did, "limit": KEEP_PER_FOLLOWER,
                     "filter": "posts_no_replies"},
                )
            except Exception:
                # A suspended or blocking account must not stop the run.
                failed += 1
                log.debug("posts unavailable for %s", did, exc_info=True)
                # Stamp it anyway so it doesn't jam the front of the queue.
                await store.replace_posts(did, [])
                continue

            posts = _parse_feed(data)
            await store.replace_posts(did, posts)
            if posts:
                fetched += 1
            else:
                empty += 1
            if index % 50 == 0:
                await store.record_progress(run_id, actors=fetched, calls=client.calls)

        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc),
                               api_calls=client.calls)
        log.exception("post fetch failed")
        raise
    finally:
        if owns_client:
            await client.aclose()

    await store.finish_run(run_id, status="ok", completed=1, actors_seen=fetched,
                           api_calls=client.calls)
    return {"status": "ok", "kind": "posts", "fetched": fetched,
            "empty": empty, "failed": failed, "api_calls": client.calls}
