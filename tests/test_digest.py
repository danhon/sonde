"""Daily email digest."""

import pytest

from sonde.config import settings
from sonde.db import store
from sonde.notify import digest as dg
from tests.fakes import actor, verified


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "digest.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


async def follower(did: str, rank: int = 0, *, score=None, followers=None,
                   is_verified=False) -> None:
    await store.upsert_actor(verified(did) if is_verified else actor(did))
    await store.mark_seen(did, rank)
    db = await store._db()
    await db.execute(
        "UPDATE actors SET influence_score = ?, followers_count = ? WHERE did = ?",
        (score, followers, did),
    )


async def healthy() -> None:
    """A recent successful sweep, so staleness doesn't dominate every test."""
    run_id = await store.start_run("full")
    await store.finish_run(run_id, status="ok", completed=1)


# ------------------------------------------------------ what goes in

async def test_arrivals_are_ranked_by_influence_not_arrival_order():
    await healthy()
    await follower("did:plc:small", 0, score=10, followers=50)
    await follower("did:plc:big", 1, score=90, followers=500_000)
    await store.add_event("did:plc:small", "followed")
    await store.add_event("did:plc:big", "followed")
    await store.commit()

    d = await dg.gather()
    assert [a["did"] for a in d.arrivals] == ["did:plc:big", "did:plc:small"]


async def test_departures_and_returns_are_separated():
    await healthy()
    await follower("did:plc:gone", 0)
    await follower("did:plc:back", 1)
    await store.add_event("did:plc:gone", "departed", reason="gone")
    await store.add_event("did:plc:back", "returned")
    await store.commit()

    d = await dg.gather()
    assert [x["did"] for x in d.departures] == ["did:plc:gone"]
    assert [x["did"] for x in d.returns] == ["did:plc:back"]


async def test_events_outside_the_window_are_excluded():
    await healthy()
    await follower("did:plc:old", 0)
    db = await store._db()
    await db.execute(
        "INSERT INTO follow_events (did, event, detected_at) VALUES (?,?,?)",
        ("did:plc:old", "followed", "2020-01-01T00:00:00+00:00"),
    )
    await store.commit()

    assert (await dg.gather()).arrivals == []


# ---------------------------------------------- when it sends

async def test_a_quiet_day_sends_nothing():
    """A daily 'nothing happened' email trains you to ignore the one that matters."""
    await healthy()
    d = await dg.gather()
    assert d.has_news is False
    assert d.worth_sending is False


async def test_a_broken_day_always_sends_even_with_no_news():
    """Silence is ambiguous — did nothing happen, or did the app die?"""
    await store.set_meta("needs_review_count", "213")
    d = await dg.gather()

    assert d.has_news is False
    assert d.worth_sending is True
    assert any("held for review" in p for p in d.problems)


async def test_a_stale_sweep_is_reported():
    """Never having swept, and not having swept lately, are both worth an email."""
    d = await dg.gather()
    assert any("ever completed successfully" in p for p in d.problems)


async def test_an_overdue_sweep_is_reported():
    from datetime import datetime, timedelta, timezone

    run_id = await store.start_run("full")
    await store.finish_run(run_id, status="ok", completed=1)
    db = await store._db()
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    await db.execute("UPDATE sync_runs SET ended_at = ? WHERE id = ?", (long_ago, run_id))
    await store.commit()

    d = await dg.gather()
    assert any("Last successful sweep was" in p for p in d.problems)


async def test_a_failed_run_is_reported():
    await healthy()
    run_id = await store.start_run("hydrate")
    await store.finish_run(run_id, status="failed", error="boom")
    d = await dg.gather()
    assert any("hydrate run failed" in p for p in d.problems)


async def test_auth_configured_but_failing_is_reported(monkeypatch):
    from sonde.api import auth as auth_module

    monkeypatch.setattr(
        auth_module.authenticator, "status",
        lambda: {"configured": True, "authenticated": False,
                 "handle": None, "error": "createSession returned 401"},
    )
    await healthy()
    d = await dg.gather()
    assert any("not authenticating" in p for p in d.problems)


# ------------------------------------------------------- rendering

async def test_the_subject_summarises_the_day():
    await healthy()
    await follower("did:plc:a", 0, score=50)
    await follower("did:plc:b", 1, score=40)
    await store.add_event("did:plc:a", "followed")
    await store.add_event("did:plc:b", "departed")
    await store.commit()

    subject = (await dg.gather()).subject
    assert "+1" in subject and "-1" in subject


async def test_problems_lead_the_body():
    await store.set_meta("needs_review_count", "5")
    body = dg.render_text(await dg.gather())
    assert body.index("NEEDS ATTENTION") < 40


async def test_both_follower_numbers_appear():
    """Tracked never equals reported; the email must not imply otherwise."""
    await healthy()
    await follower("did:plc:a", 0)
    await store.set_meta("followers_reported", "11451")

    body = dg.render_text(await dg.gather())
    assert "11,451 reported" in body
    assert "permanent" in body


async def test_long_lists_are_truncated_rather_than_dumped():
    await healthy()
    for i in range(60):
        did = f"did:plc:n{i}"
        await follower(did, i, score=float(i))
        await store.add_event(did, "followed")
    await store.commit()

    body = dg.render_text(await dg.gather(), limit=25)
    assert "and 35 more" in body


async def test_the_message_is_addressed_and_plain_text(monkeypatch):
    monkeypatch.setattr(settings.__class__, "smtp_username", "sonde@example.com",
                        raising=False)
    await healthy()
    message = dg.build_message(await dg.gather())
    assert message["Subject"].startswith("sonde:")
    assert message.get_content_type() == "text/plain"


# ---------------------------------------------------------- sending

async def test_nothing_is_sent_without_smtp_configured():
    await healthy()
    await follower("did:plc:a", 0)
    await store.add_event("did:plc:a", "followed")
    await store.commit()

    result = await dg.run_digest()
    assert result["sent"] is False
    assert result["arrivals"] == 1


async def test_a_quiet_day_records_why_it_skipped():
    await healthy()
    result = await dg.run_digest()
    assert result["skipped"] == "nothing to report"


async def test_forcing_a_send_bypasses_the_quiet_check():
    await healthy()
    sent = []

    async def fake_send(digest):
        sent.append(digest)
        return True

    original = dg.send
    dg.send = fake_send
    try:
        result = await dg.run_digest(force=True)
    finally:
        dg.send = original

    assert result["sent"] is True
    assert len(sent) == 1
