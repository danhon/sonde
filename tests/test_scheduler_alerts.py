"""At most one alert email every 30 minutes.

The gate sits in `_alert_after_sweep`, in front of both alert jobs, and keys on
when an email actually left. Not on when a sweep tried: a run that finds nothing
to say sends nothing and must not hold the next real alert back.

The two ways this could be wrong are both tested here: an email too soon after
the last one, and a real alert delayed behind a send that never happened.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sonde import scheduler
from sonde.config import settings
from sonde.db import store
from tests.fakes import verified


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "gate.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


@pytest.fixture
def calls(monkeypatch):
    """Record which alert jobs the sweep would have run."""
    seen = []

    async def arrivals():
        seen.append("arrivals")

    async def likes():
        seen.append("likes")

    monkeypatch.setattr(scheduler, "run_alerts", arrivals)
    monkeypatch.setattr(scheduler, "run_interaction_alerts", likes)
    return seen


async def sent_arrival(minutes_ago: int) -> None:
    """One arrival that really was emailed, then aged."""
    await store.upsert_actor(verified("did:plc:v"))
    await store.mark_seen("did:plc:v", 0)
    await store.add_event("did:plc:v", "followed")
    await store.commit()
    rows = await store.alert_candidates(
        since_iso="2000-01-01T00:00:00+00:00", after_event_id=0
    )
    await store.claim_alerts(rows)
    await store.mark_alerts_sent([r["event_id"] for r in rows])
    db = await store._db()
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    await db.execute("UPDATE verified_alerts SET sent_at = ?", (when,))
    await store.commit()


async def test_the_first_email_goes(calls):
    await scheduler._alert_after_sweep()
    assert calls == ["arrivals", "likes"]


async def test_a_second_email_inside_thirty_minutes_is_skipped(calls):
    await sent_arrival(minutes_ago=5)
    await scheduler._alert_after_sweep()
    assert calls == []


async def test_an_email_after_thirty_minutes_goes(calls):
    await sent_arrival(minutes_ago=31)
    await scheduler._alert_after_sweep()
    assert calls == ["arrivals", "likes"]


async def test_the_interval_is_a_setting(calls, monkeypatch):
    monkeypatch.setitem(settings.__dict__, "alert_interval_minutes", 60)
    await sent_arrival(minutes_ago=45)
    await scheduler._alert_after_sweep()
    assert calls == []


async def test_a_failed_send_does_not_hold_the_next_one(calls):
    await sent_arrival(minutes_ago=2)
    db = await store._db()
    await db.execute("UPDATE verified_alerts SET sent_at = NULL, error = 'boom'")
    await store.commit()

    await scheduler._alert_after_sweep()
    assert calls == ["arrivals", "likes"]


async def test_a_like_email_holds_an_arrival_email_back(calls):
    """One gate for both kinds, so two emails never land a minute apart."""
    db = await store._db()
    await db.execute(
        "INSERT INTO interaction_alerts (interaction_id, did, claimed_at, sent_at)"
        " VALUES (?,?,?,?)",
        (1, "did:plc:v", datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()),
    )
    await store.commit()

    await scheduler._alert_after_sweep()
    assert calls == []
