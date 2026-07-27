"""APScheduler wiring — head sweep often and cheap, full sweep slowly."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from sonde.config import settings
from sonde.db import store
from sonde.jobs import registry
from sonde.sync import runner

log = logging.getLogger("sonde.scheduler")


async def run_head() -> dict:
    return await registry.run("head", runner.head_sweep)


async def run_full() -> dict:
    return await registry.run("full", runner.full_sweep)


async def run_hydrate() -> dict:
    from sonde.sync import profiles

    return await registry.run("hydrate", lambda: profiles.hydrate(limit=1000))


async def run_follows() -> dict:
    from sonde.sync import mutuals

    return await registry.run("follows", mutuals.sync_follows)


async def run_affinity() -> dict:
    """Monthly: rosters, the affinity index, then institution rematching."""
    from sonde.sync import affinity, profiles

    async def job() -> dict:
        await store.seed_institutions()
        await store.discover_institutions_from_issuers()
        rosters = await affinity.fetch_rosters()
        index = await affinity.build_index()
        matches = await store.apply_institution_matches()
        await profiles.rescore()
        return {"rosters": rosters, "index": index, "institutions": matches}

    return await registry.run("affinity", job)


async def run_nightly() -> dict:
    """Daily rollup then snapshot, in that order so the backup includes it."""
    from sonde.sync import backup

    async def job() -> dict:
        snap = await store.record_daily_snapshot()
        result = await backup.snapshot()
        return {**result, "snapshot": snap}

    return await registry.run("nightly", job)


def attach_scheduler(app: FastAPI) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        run_head, "interval", minutes=settings.head_sweep_minutes,
        id="head", max_instances=1, coalesce=True, misfire_grace_time=300,
    )
    scheduler.add_job(
        run_full, "interval", hours=settings.full_sweep_hours,
        id="full", max_instances=1, coalesce=True, misfire_grace_time=1800,
    )
    # Hydration is TTL-driven, so running hourly just tops up whatever has
    # aged out rather than doing bulk work each time.
    scheduler.add_job(
        run_hydrate, "interval", hours=1,
        id="hydrate", max_instances=1, coalesce=True, misfire_grace_time=1800,
    )
    # My own follow list: mutual detection, and the affinity index's source pool.
    scheduler.add_job(
        run_follows, "interval", hours=24,
        id="follows", max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    # Affinity index + rosters. Expensive (~4,300 calls) but monthly, and it is
    # the signal that answers "influential in MY network" rather than in general.
    scheduler.add_job(
        run_affinity, "cron", day=2, hour=4, minute=5,
        id="affinity", max_instances=1, coalesce=True, misfire_grace_time=21600,
    )
    # Rollup + snapshot. follow_events cannot be re-fetched from Bluesky and
    # Docker volumes on ubuntuplex are not backed up, so this is the only thing
    # standing between a volume loss and losing the point of the app.
    scheduler.add_job(
        run_nightly, "cron", hour=3, minute=17,
        id="nightly", max_instances=1, coalesce=True, misfire_grace_time=7200,
    )

    @app.on_event("startup")
    async def _startup() -> None:
        await store.connect()
        scheduler.start()
        log.info(
            "scheduler started — head every %dm, full every %dh",
            settings.head_sweep_minutes, settings.full_sweep_hours,
        )
        # An empty database is useless; seed it immediately rather than waiting
        # up to FULL_SWEEP_HOURS for the first run.
        if not await store.known_dids():
            scheduler.add_job(run_full, id="bootstrap", replace_existing=True)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        scheduler.shutdown(wait=False)
        await store.close()

    return scheduler
