"""Follower sweeps.

Arrivals and departures cost wildly different amounts, so they don't share a
schedule. The follower list is newest-follow-first (verified: page 115 held no
account created after Aug 2023, while page 1 held one created two days prior),
so a new follower is always at the head — 1–2 calls. A departure is an
*absence*, provable only by walking all 115 pages.

Integrity rules, in the order they matter:

1. The cursor is the only end-of-list signal (enforced in api.graph).
2. Hydration maps by DID, never by position (enforced in api.graph).
3. Only a COMPLETE FULL sweep may compute departures. Head sweeps never can —
   they deliberately don't see most of the list.
4. A follower is lost only after N consecutive complete full sweeps miss them.
   `missed_sweeps` resets to 0 on any sighting, head sweep included.
5. A sweep that would mark more than MASS_DEPARTURE_PCT of followers lost halts
   and records `needs_review` instead of writing events. The banner carries an
   override, so a genuine mass departure can't wedge the app forever.
"""

from __future__ import annotations

import logging

from sonde.api.client import BlueskyClient
from sonde.api.graph import get_profile, iter_followers
from sonde.config import settings
from sonde.db import store

log = logging.getLogger("sonde.sync")


async def _record_arrivals(
    actors: list[dict], start_rank: int, *, backfilled: bool
) -> tuple[int, int]:
    """Upsert a page of actors. Returns (arrivals, returns)."""
    arrivals = returns = 0
    for offset, profile in enumerate(actors):
        did = profile.get("did")
        if not did:
            continue
        rank = start_rank + offset

        previous_handle = await store.get_handle(did)
        await store.upsert_actor(profile)

        new_handle = profile.get("handle")
        if previous_handle and new_handle and previous_handle != new_handle:
            # A rename is one person with continuous history, not a departure
            # plus an arrival. Every table keys on DID for exactly this reason.
            await store.add_event(
                did, "handle_changed", detail=f"{previous_handle} → {new_handle}"
            )

        was_absent = await store.mark_seen(did, rank, backfilled=backfilled)
        if was_absent and not backfilled:
            # Backfill is not arrival. On day one there is nothing to compare
            # against, so counting the import would open the timeline with a
            # cliff that never happened.
            if previous_handle is None:
                arrivals += 1
                await store.add_event(did, "followed")
            else:
                returns += 1
                await store.add_event(did, "returned")
    return arrivals, returns


async def head_sweep(client: BlueskyClient | None = None) -> dict:
    """Cheap check for new followers.

    Pages from the top until a page yields no unknown DIDs, then stops. Usually
    one page; extends by itself if hundreds arrive at once. Never computes
    departures — it doesn't see enough of the list to have an opinion.
    """
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("head")
    pages = arrivals = returns = seen = 0
    status = "ok"
    capped = False
    try:
        known = await store.known_dids()
        if not known:
            # Nothing known means every page looks new, so there is no early
            # exit to find. That is the full sweep's job; don't burn 115 calls
            # here every 15 minutes.
            log.info("head sweep skipped — no baseline yet, waiting for a full sweep")
            await store.finish_run(run_id, status="ok", completed=1, api_calls=0)
            return {
                "status": "ok", "kind": "head", "pages": 0, "seen": 0,
                "arrivals": 0, "returns": 0, "skipped": "no baseline",
                "api_calls": 0,
            }

        async for page, actors in iter_followers(
            client, settings.actor, limit=settings.head_sweep_max_pages
        ):
            pages, seen = page, seen + len(actors)
            unknown = [a for a in actors if a.get("did") not in known]
            a, r = await _record_arrivals(actors, (page - 1) * settings.page_size, backfilled=False)
            arrivals += a
            returns += r
            if not unknown:
                break  # caught up with what we already knew
            known.update(a["did"] for a in actors if a.get("did"))
        else:
            capped = pages >= settings.head_sweep_max_pages
            if capped:
                log.warning(
                    "head sweep hit its %d-page cap with arrivals still appearing; "
                    "the full sweep will reconcile", settings.head_sweep_max_pages,
                )
        await _commit()
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        status = "failed"
        await store.finish_run(run_id, status=status, error=str(exc), api_calls=client.calls)
        log.exception("head sweep failed")
        raise
    finally:
        if owns_client:
            await client.aclose()

    await store.finish_run(
        run_id, status=status, completed=1, pages_fetched=pages, actors_seen=seen,
        new_followers=arrivals, api_calls=client.calls,
    )
    return {
        "status": status, "kind": "head", "pages": pages, "seen": seen,
        "arrivals": arrivals, "returns": returns, "capped": capped,
        "api_calls": client.calls,
    }


async def full_sweep(client: BlueskyClient | None = None) -> dict:
    """Walk the entire follower list; the only sweep allowed to mark departures."""
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("full")

    first_ever = not await store.known_dids()
    if first_ever:
        # Day one has nothing to compare against. Everyone imported is marked
        # backfilled and kept out of arrival stats, or the timeline opens with a
        # 10,041-person cliff that never happened.
        log.info("first sweep — importing the existing follower list as backfill")

    pages = arrivals = returns = seen = 0
    present: set[str] = set()
    status = "ok"
    completed = False

    try:
        async for page, actors in iter_followers(client, settings.actor):
            pages, seen = page, seen + len(actors)
            present.update(a["did"] for a in actors if a.get("did"))
            a, r = await _record_arrivals(
                actors, (page - 1) * settings.page_size, backfilled=first_ever
            )
            arrivals += a
            returns += r
        completed = True

        # Trend the reported-vs-tracked gap; 1 extra call.
        try:
            me = await get_profile(client, settings.actor)
            if me.get("followersCount") is not None:
                await store.set_meta("followers_reported", str(me["followersCount"]))
        except Exception:
            log.warning("could not refresh reported follower count", exc_info=True)

        lost, status = await _apply_departures(present)
        await _commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(
            run_id, status="failed", completed=int(completed), error=str(exc),
            pages_fetched=pages, actors_seen=seen, api_calls=client.calls,
        )
        log.exception("full sweep failed")
        raise
    finally:
        if owns_client:
            await client.aclose()

    await store.finish_run(
        run_id, status=status, completed=1, pages_fetched=pages, actors_seen=seen,
        new_followers=arrivals, lost_followers=len(lost), api_calls=client.calls,
    )
    return {
        "status": status, "kind": "full", "pages": pages, "seen": seen,
        "arrivals": arrivals, "returns": returns, "departures": len(lost),
        "backfilled": first_ever, "api_calls": client.calls,
    }


async def _apply_departures(present: set[str]) -> tuple[list[str], str]:
    """Rules 3–5. Only ever called after a complete full sweep."""
    tracked = await store.known_dids()
    absent = sorted(tracked - present)
    confirmed = await store.bump_missed(absent)

    if not confirmed:
        await store.set_meta("needs_review_count", "")
        return [], "ok"

    total = max(len(tracked), 1)
    pct = len(confirmed) / total * 100
    if pct > settings.mass_departure_pct:
        # Halt rather than write. This is recoverable: /settings offers an
        # "accept this sweep" override so a real mass departure — a moderation
        # action, a bot purge — can't trip the breaker forever.
        log.error(
            "halting: %d departures is %.1f%% of %d followers, above the %.1f%% threshold",
            len(confirmed), pct, total, settings.mass_departure_pct,
        )
        await store.set_meta("needs_review_count", str(len(confirmed)))
        return [], "needs_review"

    await store.mark_departed(confirmed, reason="unknown")
    await store.set_meta("needs_review_count", "")
    return confirmed, "ok"


async def accept_pending_departures() -> int:
    """Operator override for a halted sweep (rule 5). Returns how many were applied."""
    confirmed = await store.pending_departures()
    if confirmed:
        await store.mark_departed(confirmed, reason="unknown")
    await store.set_meta("needs_review_count", "")
    run_id = await store.start_run("full")
    await store.finish_run(
        run_id, status="overridden", completed=1, lost_followers=len(confirmed)
    )
    await _commit()
    return len(confirmed)


async def _commit() -> None:
    db = await store._db()  # noqa: SLF001 - internal by design, single writer
    await db.commit()
