"""Departure detection — the rules that stop the history from lying.

Absence from the follower list is how an unfollow is detected, which makes a
half-finished sweep dangerous: it would read as thousands of people leaving at
once. These tests drive synthetic sweep sequences through the real store.
"""

import httpx
import pytest

from sonde.api.client import BlueskyClient
from sonde.config import settings
from sonde.db import store
from sonde.sync import runner
from tests.fakes import actor, follower_pages, routed


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "sync.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


def sweeper(pages: list[list[dict]]) -> BlueskyClient:
    return BlueskyClient(
        "https://fake",
        transport=routed(
            {
                "app.bsky.graph.getFollowers": follower_pages(pages),
                # getProfile for the reported-count refresh
                "app.bsky.actor.getProfile": lambda r: httpx.Response(
                    200, json={"did": "did:plc:me", "handle": "danhon.com", "followersCount": 999}
                ),
            }
        ),
        per_second=1000,
    )


def cohort(n: int, prefix: str = "f") -> list[dict]:
    return [actor(f"did:plc:{prefix}{i}") for i in range(n)]


async def run_full(pages: list[list[dict]]) -> dict:
    client = sweeper(pages)
    try:
        return await runner.full_sweep(client)
    finally:
        await client.aclose()


async def run_head(pages: list[list[dict]]) -> dict:
    client = sweeper(pages)
    try:
        return await runner.head_sweep(client)
    finally:
        await client.aclose()


# ------------------------------------------------------------- backfill

async def test_first_sweep_is_backfill_not_a_wave_of_arrivals():
    """Day one has nothing to compare against — a 10,041-person cliff never happened."""
    result = await run_full([cohort(50)])

    assert result["backfilled"] is True
    assert result["arrivals"] == 0, "backfilled followers must not count as arrivals"
    assert len((await store.known_dids())) == 50
    events = await store.recent_runs()
    assert events[0]["new_followers"] == 0


async def test_arrivals_after_backfill_are_real_arrivals():
    await run_full([cohort(10)])
    result = await run_full([[actor("did:plc:new1"), *cohort(10)]])

    assert result["backfilled"] is False
    assert result["arrivals"] == 1
    evs = await store.events_for("did:plc:new1")
    assert [e["event"] for e in evs] == ["followed"]


# ------------------------------------------------- the two-sweep rule

# NB: cohorts are 100 here so that a single departure is 1% — under the 2%
# mass-departure rail. With a cohort of 10, one loss is 10% and correctly trips
# the breaker instead, which is a different rule (tested below).

async def test_one_missed_sweep_does_not_mark_a_departure():
    await run_full([cohort(100)])
    result = await run_full([cohort(99)])  # f99 absent, first time

    assert result["departures"] == 0
    assert "did:plc:f99" in await store.known_dids()


async def test_two_consecutive_misses_mark_a_departure():
    await run_full([cohort(100)])
    await run_full([cohort(99)])
    result = await run_full([cohort(99)])

    assert result["departures"] == 1
    assert "did:plc:f99" not in await store.known_dids()
    evs = await store.events_for("did:plc:f99")
    assert evs[0]["event"] == "departed"


async def test_a_sighting_resets_the_miss_counter():
    """A page skipped mid-sweep must not accumulate toward departure."""
    await run_full([cohort(100)])
    await run_full([cohort(99)])       # missed once
    await run_full([cohort(100)])      # seen again — counter resets
    result = await run_full([cohort(99)])  # missed once more, not twice

    assert result["departures"] == 0
    assert "did:plc:f99" in await store.known_dids()


async def test_a_returning_follower_is_recorded_as_returned():
    await run_full([cohort(100)])
    await run_full([cohort(99)])
    await run_full([cohort(99)])       # f99 departs
    result = await run_full([cohort(100)])  # and comes back

    assert result["returns"] == 1
    assert "did:plc:f99" in await store.known_dids()
    kinds = [e["event"] for e in await store.events_for("did:plc:f99")]
    assert kinds[0] == "returned"


# --------------------------------------------- the mass-departure halt

async def test_a_mass_departure_halts_instead_of_writing_events():
    await run_full([cohort(100)])
    await run_full([cohort(50)])           # 50% absent, first miss
    result = await run_full([cohort(50)])  # would confirm 50 departures

    assert result["status"] == "needs_review"
    assert result["departures"] == 0
    assert len(await store.known_dids()) == 100, "no one may be marked lost"
    assert await store.get_meta("needs_review_count") == "50"


async def test_a_small_departure_is_under_the_threshold():
    """1 of 100 is 1%, below the 2% rail — normal processing."""
    await run_full([cohort(100)])
    await run_full([cohort(99)])
    result = await run_full([cohort(99)])

    assert result["status"] == "ok"
    assert result["departures"] == 1


async def test_the_halt_can_be_overridden():
    """A real mass departure must not wedge the app forever."""
    await run_full([cohort(100)])
    await run_full([cohort(50)])
    await run_full([cohort(50)])
    assert await store.get_meta("needs_review_count") == "50"

    applied = await runner.accept_pending_departures()

    assert applied == 50
    assert len(await store.known_dids()) == 50
    assert not await store.get_meta("needs_review_count")


# ---------------------------------------------------- head sweep rules

async def test_head_sweep_never_marks_departures():
    """It deliberately doesn't see most of the list, so it has no opinion."""
    await run_full([cohort(30)])
    result = await run_head([[actor("did:plc:brand-new")]])

    assert "departures" not in result
    assert len(await store.known_dids()) == 31, "nobody lost by a head sweep"


async def test_head_sweep_stops_at_the_first_fully_known_page():
    await run_full([cohort(10)])
    client = sweeper([cohort(10), cohort(10, "z"), cohort(10, "y")])
    try:
        result = await runner.head_sweep(client)
    finally:
        await client.aclose()

    assert result["pages"] == 1, "must stop once it recognises everything on a page"
    assert result["arrivals"] == 0


async def test_head_sweep_extends_through_a_burst_of_arrivals():
    """If 300 people follow at once, one page isn't enough — it self-extends."""
    await run_full([cohort(5)])
    burst_a = [actor(f"did:plc:new-a{i}") for i in range(100)]
    burst_b = [actor(f"did:plc:new-b{i}") for i in range(100)]
    client = sweeper([burst_a, burst_b, cohort(5)])
    try:
        result = await runner.head_sweep(client)
    finally:
        await client.aclose()

    assert result["pages"] == 3
    assert result["arrivals"] == 200


async def test_head_sweep_does_nothing_without_a_baseline():
    """Caught by the live eval: on an empty DB every page looks new.

    With no known DIDs there is no early exit to find, so the sweep walked all
    115 pages — at a 15-minute cadence that is ~11k calls a day on a shared IP.
    Seeding is the full sweep's job.
    """
    result = await run_head([cohort(100) for _ in range(5)])

    assert result["pages"] == 0
    assert result["api_calls"] == 0
    assert result["skipped"] == "no baseline"


async def test_head_sweep_is_capped_when_arrivals_never_run_out():
    """Belt and braces: even with a baseline, it must not walk the whole list."""
    await run_full([cohort(3)])
    endless = [[actor(f"did:plc:burst{p}-{i}") for i in range(100)] for p in range(40)]
    client = sweeper(endless)
    try:
        result = await runner.head_sweep(client)
    finally:
        await client.aclose()

    assert result["pages"] == settings.head_sweep_max_pages
    assert result["capped"] is True


async def test_head_sweep_resets_the_miss_counter_too():
    """Rule 4 says ANY sighting resets, head sweeps included."""
    await run_full([cohort(100)])
    await run_full([cohort(99)])              # f99 missed once
    await run_head([[actor("did:plc:f99")]])  # spotted by a head sweep
    result = await run_full([cohort(99)])     # missed again — should be the first

    assert result["departures"] == 0


# ---------------------------------------------------------- misc state

async def test_handle_change_is_not_a_departure_and_arrival():
    await run_full([[actor("did:plc:x", handle="old.bsky.social")]])
    await run_full([[actor("did:plc:x", handle="new.example.com")]])

    kinds = [e["event"] for e in await store.events_for("did:plc:x")]
    assert "handle_changed" in kinds
    assert "departed" not in kinds
    assert await store.get_handle("did:plc:x") == "new.example.com"


async def test_reported_follower_count_is_stored_for_the_gap():
    await run_full([cohort(3)])
    assert await store.get_meta("followers_reported") == "999"
    stats = await store.dashboard_stats()
    assert stats["counts"]["reported"] == 999
    assert stats["counts"]["tracked"] == 3


async def test_failed_sweep_records_status_and_reraises():
    """A sweep that dies mid-pagination must not compute departures."""
    await run_full([cohort(10)])

    def explode(request):
        return httpx.Response(500, json={"error": "InternalServerError"})

    client = BlueskyClient(
        "https://fake",
        transport=routed({"app.bsky.graph.getFollowers": explode}),
        per_second=1000,
        max_retries=0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await runner.full_sweep(client)
    await client.aclose()

    assert len(await store.known_dids()) == 10, "a crash must not lose anyone"
    assert (await store.recent_runs())[0]["status"] == "failed"
