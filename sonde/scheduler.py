"""APScheduler wiring — head sweep often and cheap, full sweep slowly."""

from __future__ import annotations

import logging

from datetime import datetime, timedelta, timezone

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


# Interval jobs and how long they may go unrun before startup catches them up.
# The scheduler's own timers are in-memory, so a restart resets every interval:
# deploy more often than a job's interval and it would otherwise never fire.
# `cron` jobs (nightly, affinity) are absolute and need no catch-up.
CATCH_UP: dict[str, tuple[str, int]] = {
    "full": ("full", 6 * 3600),
    "hydrate": ("hydrate", 3600),
    "follows": ("follows", 24 * 3600),
}


async def _catch_up(scheduler: AsyncIOScheduler) -> list[str]:
    """Run anything that is overdue, staggered so they don't all fire at once."""
    ages = await store.last_run_ages()
    overdue = []
    for job_id, (kind, interval) in CATCH_UP.items():
        age = ages.get(kind)
        if age is None or age >= interval:
            overdue.append(job_id)

    # 116 + 40 + 46 calls arriving together would queue behind the rate limiter
    # and starve the head sweep. Space them out.
    for position, job_id in enumerate(overdue):
        scheduler.add_job(
            scheduler.get_job(job_id).func,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=30 + position * 90),
            id=f"catchup-{job_id}",
            replace_existing=True,
        )
    return overdue


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

    # The status endpoint needs next_run_time, so the scheduler has to be
    # reachable from the request handlers.
    app.state.scheduler = scheduler

    @app.on_event("startup")
    async def _startup() -> None:
        await store.connect()
        # A restart mid-job leaves rows stuck at `running` forever.
        orphaned = await store.reconcile_orphaned_runs()
        if orphaned:
            log.info("marked %d interrupted run(s) left by a restart", orphaned)
        scheduler.start()
        log.info(
            "scheduler started — head every %dm, full every %dh",
            settings.head_sweep_minutes, settings.full_sweep_hours,
        )
        # An empty database is useless; seed it immediately rather than waiting
        # up to FULL_SWEEP_HOURS for the first run.
        if not await store.known_dids():
            scheduler.add_job(run_full, id="bootstrap", replace_existing=True)
        else:
            # Otherwise the app looks dead for HEAD_SWEEP_MINUTES after every
            # deploy. A head sweep is 1-2 calls.
            scheduler.add_job(run_head, id="startup-head", replace_existing=True)
            caught = await _catch_up(scheduler)
            if caught:
                log.info(
                    "overdue after restart, catching up: %s", ", ".join(caught)
                )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        scheduler.shutdown(wait=False)
        await store.close()

    return scheduler
