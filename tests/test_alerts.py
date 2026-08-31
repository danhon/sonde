"""Instant email when a verified account follows.

Most of these are about the ways an alerting feature is worse than no alerting
feature: emailing about the past on deploy, emailing twice, emailing once per
person, or failing silently.
"""

import pytest

from sonde.config import settings
from sonde.db import store
from sonde.notify import alerts
from tests.fakes import actor, verified


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "alerts.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


def configure(monkeypatch, **values) -> None:
    """Settings is a frozen dataclass, so its fields are patched through the
    instance dict — plain setattr raises, and patching the class is silently
    shadowed by the values the dataclass already wrote per instance."""
    for name, value in values.items():
        monkeypatch.setitem(settings.__dict__, name, value)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """SMTP present and alerting on, unless a test says otherwise."""
    configure(
        monkeypatch,
        enable_verified_alerts=True,
        smtp_host="smtp.example.com",
        smtp_username="sonde@example.com",
        notify_to="me@example.com",
        verified_alert_to="",
        verified_alert_lookback_hours=48,
        verified_alert_max_listed=10,
    )


@pytest.fixture
def outbox(monkeypatch):
    """Capture sends instead of making them."""
    sent = []

    async def fake_send(rows):
        sent.append(list(rows))
        return True

    monkeypatch.setattr(alerts, "send", fake_send)
    return sent


async def arrival(did: str, *, is_verified=True, event="followed", **extra) -> None:
    profile = verified(did, **extra) if is_verified else actor(did, **extra)
    await store.upsert_actor(profile)
    await store.mark_seen(did, 0)
    await store.add_event(did, event)
    await store.commit()


async def started() -> None:
    """Pretend alerting has been on for a while, with no backlog."""
    await store.set_meta(alerts.WATERMARK_KEY, "0")


# ------------------------------------------------------ what earns an email

async def test_a_verified_arrival_is_emailed(outbox):
    await started()
    await arrival("did:plc:v")

    result = await alerts.run_alerts()
    assert result["sent"] == 1
    assert [r["did"] for r in outbox[0]] == ["did:plc:v"]


async def test_an_unverified_arrival_is_not(outbox):
    await started()
    await arrival("did:plc:plain", is_verified=False)

    assert (await alerts.run_alerts())["sent"] == 0
    assert outbox == []


async def test_a_trusted_verifier_counts_even_without_a_verification_record(outbox):
    """Being followed by an outlet that verifies others is exactly the news."""
    await started()
    await store.upsert_actor(actor(
        "did:plc:nyt",
        verification={"verifiedStatus": "none", "trustedVerifierStatus": "valid",
                      "verifications": []},
    ))
    await store.mark_seen("did:plc:nyt", 0)
    await store.add_event("did:plc:nyt", "followed")
    await store.commit()

    assert (await alerts.run_alerts())["sent"] == 1


async def test_a_verified_account_coming_back_is_news_again(outbox):
    """The claim table keys on the event, not the person, for this reason."""
    await started()
    await arrival("did:plc:v")
    await alerts.run_alerts()

    await store.add_event("did:plc:v", "returned")
    await store.commit()
    assert (await alerts.run_alerts())["sent"] == 1
    assert "returned" in alerts.render_text(outbox[1])


async def test_departures_are_never_alerted(outbox):
    await started()
    await arrival("did:plc:v", event="departed")
    assert (await alerts.run_alerts())["sent"] == 0


async def test_a_hidden_follower_does_not_reach_the_inbox(outbox):
    """Something the moderation lists removed from every table stays removed."""
    await started()
    await arrival("did:plc:spam")
    await store.set_ignored("did:plc:spam", True, reason="moderation")
    await store.commit()

    assert (await alerts.run_alerts())["sent"] == 0


# ------------------------------------------------- never report the past

async def test_the_first_enabled_run_alerts_nothing_that_came_before_it(outbox):
    """Deploying this must not email about every verified follower of the week."""
    await arrival("did:plc:earlier")

    assert (await alerts.run_alerts())["sent"] == 0
    assert outbox == []
    assert await store.get_meta(alerts.WATERMARK_KEY) is not None


async def test_arrivals_after_the_watermark_do_get_through(outbox):
    await arrival("did:plc:earlier")
    await alerts.run_alerts()          # stamps the watermark

    await arrival("did:plc:later")
    assert (await alerts.run_alerts())["sent"] == 1
    assert [r["did"] for r in outbox[0]] == ["did:plc:later"]


async def test_a_disabled_run_stamps_no_watermark(monkeypatch, outbox):
    """Switching alerting on months later must be quiet, not retroactive."""
    configure(monkeypatch, enable_verified_alerts=False)
    await arrival("did:plc:v")

    assert (await alerts.run_alerts())["skipped"] == "disabled"
    assert await store.get_meta(alerts.WATERMARK_KEY) is None


async def test_smtp_being_unconfigured_also_stamps_nothing(monkeypatch, outbox):
    configure(monkeypatch, smtp_host="")
    await arrival("did:plc:v")

    assert (await alerts.run_alerts())["skipped"] == "SMTP not configured"
    assert await store.get_meta(alerts.WATERMARK_KEY) is None


async def test_arrivals_older_than_the_lookback_are_left_to_the_digest(monkeypatch, outbox):
    """A week of downtime comes back with today's news, not the whole backlog."""
    await started()
    await arrival("did:plc:stale")
    db = await store._db()
    await db.execute("UPDATE follow_events SET detected_at = '2020-01-01T00:00:00+00:00'")
    await store.commit()

    assert (await alerts.run_alerts())["sent"] == 0


# ------------------------------------------------------------- exactly once

async def test_the_same_arrival_is_never_emailed_twice(outbox):
    await started()
    await arrival("did:plc:v")

    assert (await alerts.run_alerts())["sent"] == 1
    assert (await alerts.run_alerts())["sent"] == 0
    assert len(outbox) == 1


async def test_a_burst_is_one_email_not_one_each(outbox):
    await started()
    for i in range(12):
        await arrival(f"did:plc:v{i}")

    result = await alerts.run_alerts()
    assert result["sent"] == 12
    assert len(outbox) == 1


async def test_a_burst_names_some_and_counts_the_rest(outbox):
    await started()
    for i in range(12):
        await arrival(f"did:plc:v{i}")
    await alerts.run_alerts()

    body = alerts.render_text(outbox[0], limit=10)
    assert "and 2 more" in body
    assert alerts.subject(outbox[0]) == "sonde: 12 verified followers"


# ------------------------------------------------------------- when it fails

async def test_a_failed_send_is_retried_on_the_next_run(monkeypatch, outbox):
    await started()
    await arrival("did:plc:v")

    async def boom(rows):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(alerts, "send", boom)
    result = await alerts.run_alerts()
    assert result["status"] == "failed" and result["pending"] == 1

    monkeypatch.setattr(alerts, "send", _capture(outbox))
    assert (await alerts.run_alerts())["retried"] == 1
    assert len(outbox) == 1


def _capture(outbox):
    async def fake_send(rows):
        outbox.append(list(rows))
        return True
    return fake_send


async def test_a_permanently_broken_mailer_gives_up_rather_than_retrying_forever(
    monkeypatch, outbox
):
    await started()
    await arrival("did:plc:v")

    async def boom(rows):
        raise RuntimeError("nope")

    monkeypatch.setattr(alerts, "send", boom)
    for _ in range(store.ALERT_MAX_ATTEMPTS):
        await alerts.run_alerts()

    # Attempts exhausted: it stops trying, and stops finding it.
    monkeypatch.setattr(alerts, "send", _capture(outbox))
    assert (await alerts.run_alerts())["sent"] == 0
    assert len(await store.abandoned_alerts()) == 1


async def test_the_digest_reports_what_alerting_gave_up_on(monkeypatch, outbox):
    """A dead mailer must not look like a quiet week."""
    from sonde.notify import digest as dg

    await started()
    await arrival("did:plc:v")

    async def boom(rows):
        raise RuntimeError("nope")

    monkeypatch.setattr(alerts, "send", boom)
    for _ in range(store.ALERT_MAX_ATTEMPTS):
        await alerts.run_alerts()

    d = await dg.gather()
    assert any("abandoned" in p for p in d.problems)
    assert d.worth_sending is True


# ------------------------------------------------------------ what it says

async def test_the_email_names_the_issuer(outbox):
    await started()
    await arrival("did:plc:v", issuer="nytimes.com")
    await alerts.run_alerts()

    assert "verified by nytimes.com" in alerts.render_text(outbox[0])


async def test_the_email_links_both_to_sonde_and_to_bluesky(outbox):
    await started()
    await arrival("did:plc:v")
    await alerts.run_alerts()

    body = alerts.render_text(outbox[0])
    assert f"https://{settings.service_host}/followers/did:plc:v" in body
    assert "https://bsky.app/profile/" in body


async def test_a_single_arrival_is_named_in_the_subject(outbox):
    await started()
    await arrival("did:plc:v", handle="ana.example.com")
    await alerts.run_alerts()

    assert alerts.subject(outbox[0]) == "sonde: verified follower — @ana.example.com"


async def test_the_message_is_addressed_and_plain_text(outbox):
    await started()
    await arrival("did:plc:v")
    await alerts.run_alerts()

    message = alerts.build_message(outbox[0])
    assert message["To"] == "me@example.com"
    assert message.get_content_type() == "text/plain"


async def test_alerts_can_be_addressed_somewhere_other_than_the_digest(monkeypatch, outbox):
    configure(monkeypatch, verified_alert_to="phone@example.com")
    await started()
    await arrival("did:plc:v")
    await alerts.run_alerts()

    assert alerts.build_message(outbox[0])["To"] == "phone@example.com"
