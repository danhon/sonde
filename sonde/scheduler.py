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
