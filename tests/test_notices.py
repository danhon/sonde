"""Dismissible warnings.

Reported from production: /verified warned about 2 followers whose verification
records fail validation, with no way to acknowledge it. Dismissing has to hide
the warning without hiding the fact — so dismissals are an append-only log, and
a warning returns on its own if what it is about changes.
"""

import pytest
from fastapi.testclient import TestClient

from sonde.db import store
from sonde.web.app import create_app
from tests.fakes import actor


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "notices.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


async def add_invalid(did: str, rank: int = 0) -> None:
    payload = actor(did)
    payload["verification"] = {
        "verifications": [], "verifiedStatus": "invalid",
        "trustedVerifierStatus": "none",
    }
    await store.upsert_actor(payload)
    await store.mark_seen(did, rank)


async def test_invalid_verifications_raise_a_notice():
    await add_invalid("did:plc:bad1", 0)
    await add_invalid("did:plc:bad2", 1)

    notices = await store.active_notices()
    assert len(notices) == 1
    assert notices[0]["kind"] == "invalid_verification"
    assert "2 follower" in notices[0]["summary"]
    assert {s["did"] for s in notices[0]["subjects"]} == {"did:plc:bad1", "did:plc:bad2"}


async def test_no_notice_when_there_is_nothing_to_warn_about():
    await store.upsert_actor(actor("did:plc:fine"))
    await store.mark_seen("did:plc:fine", 0)
    assert await store.active_notices() == []


async def test_dismissing_hides_the_notice():
    await add_invalid("did:plc:bad1", 0)
    notice = (await store.active_notices())[0]

    assert await store.dismiss_notice(notice["kind"], notice["signature"]) is True
    assert await store.active_notices() == []


async def test_a_dismissal_is_logged_with_what_it_covered():
    """Dismissing hides the warning; the log keeps the fact."""
    await add_invalid("did:plc:bad1", 0)
    notice = (await store.active_notices())[0]
    await store.dismiss_notice(notice["kind"], notice["signature"])

    log = await store.dismissal_log()
    assert len(log) == 1
    assert log[0]["kind"] == "invalid_verification"
    assert "1 follower" in log[0]["summary"]
    assert [s["handle"] for s in log[0]["subjects"]] == ["bad1.bsky.social"]
    assert log[0]["dismissed_at"]


async def test_the_warning_returns_when_the_accounts_change():
    """Dismissing '2 accounts' must not silence a later '5'."""
    await add_invalid("did:plc:bad1", 0)
    notice = (await store.active_notices())[0]
    await store.dismiss_notice(notice["kind"], notice["signature"])
    assert await store.active_notices() == []

    await add_invalid("did:plc:bad3", 2)

    fresh = await store.active_notices()
    assert len(fresh) == 1, "a new subject is a new warning"
    assert fresh[0]["signature"] != notice["signature"]


async def test_the_same_warning_stays_dismissed_across_rescans():
    await add_invalid("did:plc:bad1", 0)
    notice = (await store.active_notices())[0]
    await store.dismiss_notice(notice["kind"], notice["signature"])

    for _ in range(3):
        assert await store.active_notices() == []


async def test_dismissing_something_that_is_not_active_does_nothing():
    assert await store.dismiss_notice("invalid_verification", "deadbeef") is False
    assert await store.dismissal_log() == []


async def test_a_dismissal_can_be_undone():
    await add_invalid("did:plc:bad1", 0)
    notice = (await store.active_notices())[0]
    await store.dismiss_notice(notice["kind"], notice["signature"])

    await store.restore_notice((await store.dismissal_log())[0]["id"])

    assert len(await store.active_notices()) == 1
    assert await store.dismissal_log() == []


async def test_hidden_followers_do_not_raise_a_notice():
    await add_invalid("did:plc:bad1", 0)
    await store.set_ignored("did:plc:bad1", True)
    assert await store.active_notices() == []


# ------------------------------------------------------------- routes

async def test_the_banner_is_dismissible_from_verified(client):
    await add_invalid("did:plc:bad1", 0)
    html = client.get("/verified").text
    assert "fails validation" in html
    assert "/dismiss" in html

    notice = (await store.active_notices())[0]
    client.post(
        f"/notices/{notice['kind']}/{notice['signature']}/dismiss?back=/verified",
        follow_redirects=False,
    )

    assert "fails validation" not in client.get("/verified").text


async def test_the_log_is_visible_on_settings(client):
    await add_invalid("did:plc:bad1", 0)
    notice = (await store.active_notices())[0]
    client.post(f"/notices/{notice['kind']}/{notice['signature']}/dismiss",
                follow_redirects=False)

    html = client.get("/settings").text
    assert "Dismissed warnings" in html
    assert "bad1.bsky.social" in html
    assert "un-dismiss" in html


async def test_dismiss_redirects_somewhere_safe(client):
    """`back` comes from the query string; it must not become an open redirect."""
    await add_invalid("did:plc:bad1", 0)
    notice = (await store.active_notices())[0]
    r = client.post(
        f"/notices/{notice['kind']}/{notice['signature']}/dismiss?back=https://evil.example",
        follow_redirects=False,
    )
    assert r.headers["location"] == "/verified"
