"""Wikipedia pageviews — attention, not merely existence.

Having an article says someone was notable once. Pageviews say whether anyone
is reading it now. Measured 2026-07-26: Naomi Klein 29,920 views in 30 days,
Jamelle Bouie 6,726.

One call per matched person per week, and only the ~107 people Wikidata matched
— so about fifteen calls a day. The API is free and needs no key.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from sonde.db import store
from sonde.jobs import registry

log = logging.getLogger("sonde.wikipedia")

BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
USER_AGENT = "sonde/0.1 (https://github.com/danhon/sonde; follower analytics)"


def _window(days: int = 30) -> tuple[str, str]:
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=days)
    return start.strftime("%Y%m%d00"), end.strftime("%Y%m%d00")


async def views_for(client: httpx.AsyncClient, title: str, days: int = 30) -> int | None:
    """Total views over the window, or None if there is no article by that title."""
    start, end = _window(days)
    safe = title.replace(" ", "_")
    url = f"{BASE}/en.wikipedia/all-access/user/{safe}/daily/{start}/{end}"
    resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return sum(item.get("views", 0) for item in items)


async def refresh(limit: int = 200, transport=None) -> dict:
    run_id = await store.start_run("pageviews")
    fetched = missing = failed = calls = 0
    try:
        targets = await store.pageview_targets(limit=limit)
        if not targets:
            await store.finish_run(run_id, status="ok", completed=1, api_calls=0)
            return {"status": "ok", "kind": "pageviews", "fetched": 0, "api_calls": 0}

        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            for index, row in enumerate(targets, 1):
                registry.progress("pageviews", index, len(targets), "articles",
                                  f"{fetched:,} fetched")
                try:
                    views = await views_for(client, row["wikipedia_title"])
                    calls += 1
                except Exception:
                    failed += 1
                    log.debug("pageviews failed for %r", row["wikipedia_title"],
                              exc_info=True)
                    continue
                # Stamp either way: a missing article should not be retried
                # every cycle to learn the same thing.
                await store.record_pageviews(row["did"], views)
                if views is None:
                    missing += 1
                else:
                    fetched += 1

        await store.rescore_all()
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc), api_calls=calls)
        log.exception("pageview refresh failed")
        raise

    await store.finish_run(run_id, status="ok", completed=1,
                           actors_seen=fetched, api_calls=calls)
    return {"status": "ok", "kind": "pageviews", "fetched": fetched,
            "no_article": missing, "failed": failed, "api_calls": calls}
