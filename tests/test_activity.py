"""M7.5 — showing what the app is doing.

Every test here is a regression for something found in production on
2026-07-27, when the report was "there is no progress indicator so I can't tell
if it's working properly or if it's running anything in the background."
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from sonde.db import store
from sonde.jobs import JobRegistry
from sonde.web.app import create_app


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "activity.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


@pytest.fixture
def client(tmp_path):
    store.set_db_path(str(tmp_path / "activity.db"))
    with TestClient(create_app()) as c:
        yield c


# --------------------------------------------- orphaned runs (bug 1)

async def test_a_restart_mid_job_does_not_leave_a_job_running_forever():
    """Nothing else resolves these, so they persisted indefinitely."""
    run_id = await store.start_run("full")
    assert (await store.recent_runs())[0]["status"] == "running"

    closed = await store.reconcile_orphaned_runs()

    assert closed == 1
    row = (await store.recent_runs())[0]
    assert row["status"] == "interrupted"
    assert row["ended_at"] is not None
    assert "restart" in (row["error"] or "")


async def test_interrupted_is_distinct_from_failed():
    """They did not fail. Conflating the two hides real failures."""
    await store.start_run("full")
    await store.reconcile_orphaned_runs()
    assert (await store.recent_runs())[0]["status"] == "interrupted"


async def test_reconciliation_leaves_finished_runs_alone():
    ok_id = await store.start_run("head")
    await store.finish_run(ok_id, status="ok", completed=1)
    await store.start_run("full")

    assert await store.reconcile_orphaned_runs() == 1
    statuses = {r["kind"]: r["status"] for r in await store.recent_runs()}
    assert statuses["head"] == "ok"
    assert statuses["full"] == "interrupted"


async def test_an_interrupted_run_is_not_reported_as_the_last_sync():
    """healthz must not claim a job that never finished as the last good sync."""
    good = await store.start_run("full")
    await store.finish_run(good, status="ok", completed=1, actors_seen=10_042)
    await store.start_run("hydrate")
    await store.reconcile_orphaned_runs()

    summary = await store.last_sync_summary()
    assert summary["kind"] == "full"
    assert summary["status"] == "ok"


# ------------------------------------------- partial progress (bug 5)

async def test_an_interrupted_run_records_how_far_it_reached():
    run_id = await store.start_run("full")
    await store.record_progress(run_id, pages=47, actors=4_100, calls=47)
    await store.reconcile_orphaned_runs()

    row = (await store.recent_runs())[0]
    assert row["pages_fetched"] == 47
    assert row["actors_seen"] == 4_100
    assert row["status"] == "interrupted"


async def test_record_progress_ignores_an_empty_update():
    run_id = await store.start_run("full")
    await store.record_progress(run_id)  # must not raise or wipe columns
    assert (await store.recent_runs())[0]["pages_fetched"] == 0


async def test_last_sync_age_is_reported():
    run_id = await store.start_run("full")
    await store.finish_run(run_id, status="ok", completed=1)
    age = await store.last_sync_age_seconds()
    assert age is not None and age < 5


async def test_last_sync_age_is_none_before_any_run():
    assert await store.last_sync_age_seconds() is None


# ------------------------------------------------ live progress (registry)

async def test_progress_is_reported_while_a_job_runs():
    registry = JobRegistry()
    started, release = asyncio.Event(), asyncio.Event()

    async def job() -> dict:
        registry.progress("full", 47, 115, "pages", "4,100 followers seen")
        started.set()
        await release.wait()
        return {"status": "ok"}

    task = registry.spawn("full", job)
    await started.wait()

    snapshot = registry.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["kind"] == "full"
    assert snapshot[0]["current"] == 47
    assert snapshot[0]["total"] == 115
    assert snapshot[0]["pct"] == pytest.approx(40.9, abs=0.1)
    assert snapshot[0]["unit"] == "pages"
    assert snapshot[0]["elapsed"] >= 0

    release.set()
    await task
    assert registry.snapshot() == []


async def test_progress_on_an_untracked_kind_is_a_noop():
    JobRegistry().progress("nothing", 1, 2, "pages")  # must not raise


async def test_pct_is_none_without_a_known_total():
    registry = JobRegistry()
    started, release = asyncio.Event(), asyncio.Event()

    async def job() -> dict:
        registry.progress("rescore", 5)
        started.set()
        await release.wait()
        return {}

    task = registry.spawn("rescore", job)
    await started.wait()
    assert registry.snapshot()[0]["pct"] is None
    release.set()
    await task


async def test_spawn_keeps_a_reference_so_the_task_cannot_vanish():
    """Bare create_task lets the loop drop a task nobody awaits, exception and all."""
    registry = JobRegistry()
    done = asyncio.Event()

    async def job() -> dict:
        await asyncio.sleep(0)
        done.set()
        return {"status": "ok"}

    registry.spawn("head", job)
    del job  # only the registry's reference remains
    await asyncio.wait_for(done.wait(), timeout=2)


async def test_a_failing_background_job_does_not_crash_the_app():
    registry = JobRegistry()

    async def job() -> dict:
        raise RuntimeError("boom")

    task = registry.spawn("full", job)
    await asyncio.gather(task, return_exceptions=True)
    assert registry.running() == []


# ----------------------------------------------------- /api/status

def test_status_endpoint_reports_idle(client):
    body = client.get("/api/status").json()
    assert body["running"] == []
    assert body["scheduler"] is False
    assert "last_sync_age_seconds" in body
    assert body["scheduled"] == []


def test_status_endpoint_leaks_no_follower_data(client):
    """It is polled constantly; keep it to job state."""
    blob = repr(client.get("/api/status").json())
    assert "did:" not in blob
    assert "bsky.social" not in blob


def test_every_page_carries_the_activity_strip(client):
    """The complaint was not being able to tell from anywhere that it works."""
    for path in ("/", "/followers", "/influential", "/verified", "/changes", "/settings"):
        html = client.get(path).text
        assert 'id="activity"' in html, path
        assert "/api/status" in html, path


def test_the_strip_handles_an_expired_session(client):
    """The poll gets an Authelia redirect, not JSON; it must say so."""
    html = client.get("/").text
    assert 'redirect: "error"' in html
    assert "Session expired" in html


def test_polling_adapts_to_whether_anything_is_running(client):
    html = client.get("/").text
    assert "BUSY = 3000" in html and "IDLE = 15000" in html


# ------------------------------------------------- status rendering

async def test_a_running_job_does_not_render_as_a_failure(client):
    """The dashboard styled status as ok-or-red, so in-flight looked like a crash."""
    await store.start_run("full")
    html = client.get("/").text
    row = html[html.index("running") - 400: html.index("running") + 20]
    assert "text-red-700" not in row
    assert "text-slate-500" in row


async def test_an_interrupted_job_does_not_render_as_a_failure(client):
    await store.start_run("full")
    await store.reconcile_orphaned_runs()
    html = client.get("/settings").text
    row = html[html.index("interrupted") - 400: html.index("interrupted") + 20]
    assert "text-red-700" not in row


async def test_a_genuine_failure_still_renders_red(client):
    run_id = await store.start_run("full")
    await store.finish_run(run_id, status="failed", error="boom")
    html = client.get("/").text
    row = html[html.index(">failed") - 400: html.index(">failed") + 20]
    assert "text-red-700" in row


# ------------------------------------- scheduler state across restarts

async def test_last_run_ages_reports_per_kind():
    """Interval timers reset on restart, so 'when did this last succeed'
    has to come from the database instead."""
    full = await store.start_run("full")
    await store.finish_run(full, status="ok", completed=1)
    hyd = await store.start_run("hydrate")
    await store.finish_run(hyd, status="ok", completed=1)

    ages = await store.last_run_ages()
    assert set(ages) == {"full", "hydrate"}
    assert all(0 <= v < 5 for v in ages.values())


async def test_last_run_ages_ignores_unsuccessful_runs():
    """An interrupted or failed run must not count as 'recently done',
    or catch-up would skip a job that never actually completed."""
    interrupted = await store.start_run("full")
    await store.reconcile_orphaned_runs()
    failed = await store.start_run("hydrate")
    await store.finish_run(failed, status="failed", error="boom")

    assert await store.last_run_ages() == {}


async def test_catch_up_runs_jobs_that_are_overdue(monkeypatch):
    """The real failure: deploy every 4h and the 6h full sweep never fires,
    so departures are never detected."""
    from sonde import scheduler as sched

    calls = []

    class FakeJob:
        def __init__(self, jid):
            self.id, self.func = jid, lambda: None

    class FakeScheduler:
        def get_job(self, jid):
            return FakeJob(jid)

        def add_job(self, func, trigger, **kw):
            calls.append(kw["id"])

    # Nothing has ever run: everything is overdue.
    overdue = await sched._catch_up(FakeScheduler())

    assert set(overdue) == {"full", "hydrate", "follows"}
    assert calls == ["catchup-full", "catchup-hydrate", "catchup-follows"]


async def test_catch_up_skips_jobs_that_ran_recently():
    from sonde import scheduler as sched

    for kind in ("full", "hydrate", "follows"):
        run_id = await store.start_run(kind)
        await store.finish_run(run_id, status="ok", completed=1)

    class FakeScheduler:
        def get_job(self, jid):
            raise AssertionError("should not schedule a job that just ran")

        def add_job(self, *a, **kw):
            raise AssertionError("should not schedule a job that just ran")

    assert await sched._catch_up(FakeScheduler()) == []


async def test_catch_up_staggers_so_jobs_do_not_all_fire_at_once():
    """116 + 40 + 46 calls together would queue behind the rate limiter
    and starve the head sweep."""
    from sonde import scheduler as sched

    run_dates = []

    class FakeScheduler:
        def get_job(self, jid):
            return type("J", (), {"id": jid, "func": lambda: None})()

        def add_job(self, func, trigger, **kw):
            run_dates.append(kw["run_date"])

    await sched._catch_up(FakeScheduler())

    gaps = [
        (run_dates[i + 1] - run_dates[i]).total_seconds()
        for i in range(len(run_dates) - 1)
    ]
    assert all(g >= 60 for g in gaps), f"jobs fire too close together: {gaps}"
