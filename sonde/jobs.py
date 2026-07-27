"""Single-flight locks and in-memory progress for the UI.

The scheduler and the manual trigger can collide, and a head sweep can fire
mid-full-sweep. A second request for a running kind attaches to the running job
rather than queuing a duplicate.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("sonde.jobs")


@dataclass
class Job:
    kind: str
    task: asyncio.Task
    started_at: float
    detail: str = ""


@dataclass
class JobRegistry:
    _running: dict[str, Job] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def run(self, kind: str, fn: Callable[[], Awaitable[dict]]) -> dict:
        """Run `fn` under a single-flight lock keyed on `kind`."""
        async with self._lock:
            existing = self._running.get(kind)
            if existing is not None and not existing.task.done():
                log.info("%s already running; attaching to it", kind)
                task = existing.task
            else:
                task = asyncio.create_task(fn())
                self._running[kind] = Job(
                    kind=kind, task=task, started_at=asyncio.get_running_loop().time()
                )
        try:
            return await asyncio.shield(task)
        finally:
            async with self._lock:
                job = self._running.get(kind)
                if job is not None and job.task is task and task.done():
                    self._running.pop(kind, None)

    def running(self) -> list[str]:
        return [k for k, j in self._running.items() if not j.task.done()]

    def is_running(self, kind: str) -> bool:
        job = self._running.get(kind)
        return job is not None and not job.task.done()


registry = JobRegistry()
