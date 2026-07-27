"""Single-flight locks and live progress for the UI.

The scheduler and the manual trigger can collide, and a head sweep can fire
mid-full-sweep. A second request for a running kind attaches to the running job
rather than queuing a duplicate.

Progress lives here because it is inherently in-memory and per-process: it is
the answer to "is it working right now". The durable record is `sync_runs`,
which jobs update on a throttle so an interrupted run still shows how far it
reached.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("sonde.jobs")


@dataclass
class Job:
    kind: str
    task: asyncio.Task
    started_at: float
    current: int = 0
    total: int | None = None
    unit: str = ""
    detail: str = ""

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.started_at, 1)

    @property
    def pct(self) -> float | None:
        if not self.total:
            return None
        return round(min(self.current / self.total, 1.0) * 100, 1)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "current": self.current,
            "total": self.total,
            "unit": self.unit,
            "pct": self.pct,
            "detail": self.detail,
            "elapsed": self.elapsed,
        }


@dataclass
class JobRegistry:
    _running: dict[str, Job] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Tasks are kept here as well as on the Job so a fire-and-forget trigger
    # cannot be garbage-collected mid-execution.
    _background: set[asyncio.Task] = field(default_factory=set)

    async def run(self, kind: str, fn: Callable[[], Awaitable[dict]]) -> dict:
        """Run `fn` under a single-flight lock keyed on `kind`."""
        async with self._lock:
            existing = self._running.get(kind)
            if existing is not None and not existing.task.done():
                log.info("%s already running; attaching to it", kind)
                task = existing.task
            else:
                task = asyncio.create_task(fn())
                self._running[kind] = Job(kind=kind, task=task, started_at=time.monotonic())
        try:
            return await asyncio.shield(task)
        finally:
            async with self._lock:
                job = self._running.get(kind)
                if job is not None and job.task is task and task.done():
                    self._running.pop(kind, None)

    def spawn(self, kind: str, fn: Callable[[], Awaitable[dict]]) -> asyncio.Task:
        """Fire-and-forget a job, keeping a strong reference to the task.

        `asyncio.create_task` alone is a footgun: the event loop holds only a
        weak reference, so a task nobody awaits can vanish mid-flight and take
        its exception with it.
        """
        task = asyncio.create_task(self.run(kind, fn))
        self._background.add(task)

        def _done(t: asyncio.Task) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("background job %s failed", kind, exc_info=t.exception())

        task.add_done_callback(_done)
        return task

    def progress(
        self, kind: str, current: int, total: int | None = None,
        unit: str = "", detail: str = "",
    ) -> None:
        """Report how far a running job has got. Safe to call when not tracked."""
        job = self._running.get(kind)
        if job is None:
            return
        job.current = current
        if total is not None:
            job.total = total
        if unit:
            job.unit = unit
        if detail:
            job.detail = detail

    def running(self) -> list[str]:
        return [k for k, j in self._running.items() if not j.task.done()]

    def snapshot(self) -> list[dict]:
        return [j.as_dict() for j in self._running.values() if not j.task.done()]

    def is_running(self, kind: str) -> bool:
        job = self._running.get(kind)
        return job is not None and not job.task.done()


registry = JobRegistry()
