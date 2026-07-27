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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sonde", description="Bluesky follower tracker")
    parser.add_argument("--once", action="store_true", help="run one full sweep and exit")
    parser.add_argument("--head", action="store_true", help="run one head sweep and exit")
    parser.add_argument("--hydrate", action="store_true", help="hydrate stale profiles and exit")
    parser.add_argument("--rescore", action="store_true", help="recompute all scores and exit")
    parser.add_argument("--limit", type=int, default=None, help="cap actors hydrated")
    parser.add_argument("--schedule", action="store_true", help="web UI plus scheduled sweeps")
    args = parser.parse_args(argv)

    _configure_logging()

    if args.once or args.head:
        return asyncio.run(_run_once("head" if args.head else "full"))
    if args.hydrate:
        return asyncio.run(_run_hydrate(args.limit))
    if args.rescore:
        return asyncio.run(_run_rescore())

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
