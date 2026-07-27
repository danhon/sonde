"""Entry point — web only, a single sweep, or web plus the scheduler."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from sonde.config import settings

log = logging.getLogger("sonde")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run_once(kind: str) -> int:
    from sonde.db import store
    from sonde.sync import runner

    await store.connect()
    try:
        result = await runner.full_sweep() if kind == "full" else await runner.head_sweep()
        log.info("sweep finished: %s", result)
        return 0 if result.get("status") == "ok" else 1
    finally:
        await store.close()


async def _run_hydrate(limit: int | None) -> int:
    from sonde.db import store
    from sonde.sync import profiles

    await store.connect()
    try:
        result = await profiles.hydrate(limit=limit)
        log.info("hydration finished: %s", result)
        return 0 if result["status"] == "ok" else 1
    finally:
        await store.close()


async def _run_rescore() -> int:
    from sonde.db import store
    from sonde.sync import profiles

    await store.connect()
    try:
        log.info("rescore finished: %s", await profiles.rescore())
        return 0
    finally:
        await store.close()


async def _run_follows() -> int:
    from sonde.db import store
    from sonde.sync import mutuals

    await store.connect()
    try:
        log.info("follows sync finished: %s", await mutuals.sync_follows())
        return 0
    finally:
        await store.close()


async def _run_institutions() -> int:
    """6a — zero API calls; it re-reads what the sweep already stored."""
    from sonde.db import store
    from sonde.sync import profiles

    await store.connect()
    try:
        seeded = await store.seed_institutions()
        found = await store.discover_institutions_from_issuers()
        result = await store.apply_institution_matches()
        await profiles.rescore()
        log.info("seeded %d, discovered %s, matched %s", seeded, found, result)
        return 0
    finally:
        await store.close()


async def _run_simple(kind: str, limit: int | None) -> int:
    from sonde.db import store
    from sonde.sync import affinity

    await store.connect()
    try:
        if kind == "rosters":
            result = await affinity.fetch_rosters()
        else:
            result = await affinity.build_index(cap=limit)
        log.info("%s finished: %s", kind, result)
        return 0 if result["status"] == "ok" else 1
    finally:
        await store.close()


async def _run_backup() -> int:
    from sonde.db import store
    from sonde.sync import backup

    await store.connect()
    try:
        await store.record_daily_snapshot()
        result = await backup.snapshot()
        log.info("backup finished: %s", result)
        return 0
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sonde", description="Bluesky follower tracker")
    parser.add_argument("--once", action="store_true", help="run one full sweep and exit")
    parser.add_argument("--head", action="store_true", help="run one head sweep and exit")
    parser.add_argument("--hydrate", action="store_true", help="hydrate stale profiles and exit")
    parser.add_argument("--rescore", action="store_true", help="recompute all scores and exit")
    parser.add_argument("--limit", type=int, default=None, help="cap actors hydrated")
    parser.add_argument("--follows", action="store_true", help="sync my follow list and exit")
    parser.add_argument("--institutions", action="store_true",
                        help="seed, discover and match institutions (no API calls)")
    parser.add_argument("--rosters", action="store_true", help="fetch institution rosters")
    parser.add_argument("--affinity", action="store_true", help="build the affinity index")
    parser.add_argument("--backup", action="store_true", help="write a snapshot and exit")
    parser.add_argument("--schedule", action="store_true", help="web UI plus scheduled sweeps")
    args = parser.parse_args(argv)

    _configure_logging()

    if args.once or args.head:
        return asyncio.run(_run_once("head" if args.head else "full"))
    if args.hydrate:
        return asyncio.run(_run_hydrate(args.limit))
    if args.rescore:
        return asyncio.run(_run_rescore())
    if args.follows:
        return asyncio.run(_run_follows())
    if args.institutions:
        return asyncio.run(_run_institutions())
    if args.rosters:
        return asyncio.run(_run_simple("rosters", args.limit))
    if args.affinity:
        return asyncio.run(_run_simple("affinity", args.limit))
    if args.backup:
        return asyncio.run(_run_backup())

    from sonde.web.app import app

    if args.schedule:
        from sonde.scheduler import attach_scheduler

        attach_scheduler(app)

    log.info(
        "starting web on %s:%s (actor=%s, schedule=%s)",
        settings.web_host, settings.web_port, settings.actor, args.schedule,
    )
    uvicorn.run(app, host=settings.web_host, port=settings.web_port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
