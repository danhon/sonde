"""Email every notable like or repost: one run of the alert job against a
temporary database, a fake Bluesky, a captured outbox and an injected clock.

The run reads notifications (the light check) and then emails whatever notable
likes and reposts still owe an email. Like the arrival alerts, most of what is
tested here is a way alerting could be worse than none: emailing the past,
emailing twice, emailing at night, or naming people on the lock screen.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from sonde.api.client import BlueskyClient
from sonde.config import settings
from sonde.db import store
from sonde.notify import interaction_alerts as ia
from tests.fakes import actor, profiles_response, routed, verified

PACIFIC = ZoneInfo("America/Los_Angeles")
ME = "did:plc:me"
POST_A = f"at://{ME}/app.bsky.feed.post/aaa"
POST_B = f"at://{ME}/app.bsky.feed.post/bbb"
TEXTS = {POST_A: "First line of post A\nsecond line", POST_B: "Post B says hello"}

# Today in Pacific time. Near the real clock on purpose: the check refreshes
# follower counts only for likes within 48 hours of the real "now".
_TODAY = datetime.now(PACIFIC).date()


def pacific(hour: int, minute: int = 0, *, days: int = 0) -> datetime:
    day = _TODAY + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=PACIFIC)


def stamp(when: datetime) -> str:
    """As Bluesky writes `indexedAt`."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def configure(monkeypatch, **values) -> None:
    for name, value in values.items():
        monkeypatch.setitem(settings.__dict__, name, value)


@pytest.fixture(autouse=True)
async def _db(db):
    yield


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    configure(
        monkeypatch,
        enable_interaction_alerts=True,
        interaction_alert_lookback_hours=48,
        smtp_host="smtp.example.com",
        smtp_username="sonde@example.com",
        notify_to="me@example.com",
        verified_alert_to="",
        large_account_followers=10_000,
    )


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    async def fake_send(message):
        sent.append(message)
        return True

    async def no_avatars(rows):
        return {}

    monkeypatch.setattr(ia, "send", fake_send)
    # No test here may reach the network for a picture.
    monkeypatch.setattr(ia, "avatars", no_avatars)
    return sent


def body(message) -> str:
    """The plain-text part: the email is multipart now, with an HTML twin."""
    return message.get_body(("plain",)).get_content()


class FakeBluesky:
    """Notifications newest-first in one page, profiles, and posts."""

    def __init__(self):
        self.notes: list[dict] = []
        self.known: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.fail_notifications = False
        self._n = 0
        profiles = profiles_response(self.known)

        def list_notifications(request):
            if self.fail_notifications:
                return httpx.Response(400, json={"error": "InvalidRequest"})
            return httpx.Response(200, json={"notifications": list(self.notes)})

        def get_posts(request):
            uris = request.url.params.get_list("uris")
            return httpx.Response(200, json={"posts": [
                {"uri": u, "author": {"did": ME}, "record": {"text": TEXTS[u]}}
                for u in uris if u in TEXTS]})

        inner = routed({
            "app.bsky.notification.listNotifications": list_notifications,
            "app.bsky.actor.getProfiles": profiles,
            "app.bsky.feed.getPosts": get_posts,
        })

        def record(request):
            self.requests.append(request)
            return inner.handle_request(request)

        self.client = BlueskyClient("https://fake", transport=httpx.MockTransport(record),
                                    per_second=1000, max_retries=0)

    def add(self, author: dict, reason: str, when: datetime, post: str = POST_A,
            followers: int | None = None) -> None:
        """A new like or repost, newest first, as listNotifications serves it."""
        self._n += 1
        profile = dict(author)
        if followers is not None:
            profile["followersCount"] = followers
        self.known[author["did"]] = profile
        self.notes.insert(0, {
            "uri": f"at://{author['did']}/app.bsky.feed.{reason}/{self._n}",
            "cid": f"cid{self._n}", "author": author, "reason": reason,
            "reasonSubject": post, "record": {"subject": {"uri": post}},
            "isRead": False, "indexedAt": stamp(when),
        })

    def calls(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/" + method)]


@pytest.fixture
def bsky():
    return FakeBluesky()


async def started() -> None:
    """Pretend the alerts have been on for a while, with no backlog."""
    await store.set_meta(ia.WATERMARK_KEY, "0")


async def run(bsky, now: datetime) -> dict:
    return await ia.run_interaction_alerts(bsky.client, now=now)


# ------------------------------------------------------ what earns an email

async def test_a_verified_like_is_emailed(bsky, outbox):
    await started()
    bsky.add(verified("did:plc:v", displayName="Vera"), "like", pacific(9, 50))

    result = await run(bsky, pacific(10))

    assert result["sent"] == 1
    assert len(outbox) == 1
    text = body(outbox[0])
    assert "Vera" in text and "@v.bsky.social" in text
    assert "verified" in text and "liked" in text


async def test_a_verified_repost_is_emailed(bsky, outbox):
    await started()
    bsky.add(verified("did:plc:v"), "repost", pacific(9, 50))

    await run(bsky, pacific(10))

    assert "reposted" in body(outbox[0])
    assert outbox[0]["Subject"] == "sonde: 1 notable repost"


async def test_more_than_ten_thousand_followers_counts(bsky, outbox):
    await started()
    bsky.add(actor("did:plc:big", displayName="Big"), "like", pacific(9, 50),
             followers=10_001)

    await run(bsky, pacific(10))

    assert len(outbox) == 1
    assert "about 10k followers" in body(outbox[0])


async def test_exactly_ten_thousand_followers_does_not(bsky, outbox):
    await started()
    bsky.add(actor("did:plc:edge"), "like", pacific(9, 50), followers=10_000)

    result = await run(bsky, pacific(10))

    assert result["sent"] == 0 and outbox == []


async def test_an_ordinary_account_does_not(bsky, outbox):
    await started()
    bsky.add(actor("did:plc:someone"), "like", pacific(9, 50), followers=300)

    await run(bsky, pacific(10))

    assert outbox == []


async def test_a_trusted_verifier_counts(bsky, outbox):
    await started()
    tv = actor("did:plc:tv", verification={
        "verifications": [], "verifiedStatus": "none",
        "trustedVerifierStatus": "valid"})
    bsky.add(tv, "like", pacific(9, 50))

    await run(bsky, pacific(10))

    assert "a trusted verifier" in body(outbox[0])


async def test_a_stranger_counts_and_is_not_made_a_follower(bsky, outbox):
    """Nobody here follows the operator: there is no follower_state row."""
    await started()
    bsky.add(verified("did:plc:stranger"), "like", pacific(9, 50))

    await run(bsky, pacific(10))

    assert len(outbox) == 1
    assert "did:plc:stranger" not in await store.known_dids()


async def test_a_hidden_account_does_not(bsky, outbox):
    await started()
    spam = verified("did:plc:spam")
    await store.upsert_actor(spam)
    await store.mark_seen("did:plc:spam", 0)
    await store.commit()
    await store.set_ignored("did:plc:spam", True, reason="moderation")
    bsky.add(spam, "like", pacific(9, 50), followers=50_000)

    await run(bsky, pacific(10))

    assert outbox == []


async def test_a_repeat_like_from_the_same_person_is_emailed_again(bsky, outbox):
    await started()
    fan = verified("did:plc:v")
    bsky.add(fan, "like", pacific(9, 50), post=POST_A)
    await run(bsky, pacific(10))

    bsky.add(fan, "like", pacific(10, 5), post=POST_B)
    await run(bsky, pacific(10, 15))

    assert len(outbox) == 2
    assert "Post B says hello" in body(outbox[1])
    assert "First line of post A" not in body(outbox[1])


# ------------------------------------------------------------ what it says

async def test_a_burst_is_one_email_grouped_by_post(bsky, outbox):
    await started()
    bsky.add(verified("did:plc:v1", displayName="One"), "like", pacific(9, 40), post=POST_A)
    bsky.add(verified("did:plc:v2", displayName="Two"), "like", pacific(9, 41), post=POST_B)
    bsky.add(verified("did:plc:v3", displayName="Three"), "repost", pacific(9, 42), post=POST_A)
    bsky.add(actor("did:plc:big", displayName="Big"), "like", pacific(9, 43),
             post=POST_A, followers=25_400)

    result = await run(bsky, pacific(10))

    assert result["sent"] == 4 and len(outbox) == 1
    text = body(outbox[0])
    # Each post appears once, with its first line and link, and its events
    # beneath it, every one listed.
    assert text.count("First line of post A") == 1
    assert "second line" not in text
    assert text.count("Post B says hello") == 1
    assert "https://bsky.app/profile/did:plc:me/post/aaa" in text
    assert "https://bsky.app/profile/did:plc:me/post/bbb" in text
    a, b = text.index("First line of post A"), text.index("Post B says hello")
    under_a = text[a:b] if a < b else text[a:]
    for name in ("One", "Three", "Big"):
        assert name in under_a
    assert "Two" not in under_a
    assert "about 25k followers" in under_a
    assert "9:43 am" in text


async def test_every_event_is_listed_however_many(bsky, outbox):
    await started()
    for i in range(30):
        bsky.add(verified(f"did:plc:v{i}", displayName=f"Person{i:02d}"), "like",
                 pacific(9, i))

    await run(bsky, pacific(10))

    text = body(outbox[0])
    assert all(f"Person{i:02d}" in text for i in range(30))
    assert " more." not in text


async def test_the_subject_carries_counts_and_no_names(bsky, outbox):
    await started()
    bsky.add(verified("did:plc:v1", "ana.example.com", displayName="Ana"), "like", pacific(9, 40))
    bsky.add(verified("did:plc:v2", "bo.example.com", displayName="Bo"), "like", pacific(9, 41))
    bsky.add(verified("did:plc:v3", "cy.example.com", displayName="Cy"), "like", pacific(9, 42))
    bsky.add(verified("did:plc:v4", "di.example.com", displayName="Di"), "repost", pacific(9, 43))

    await run(bsky, pacific(10))

    subj = outbox[0]["Subject"]
    assert subj == "sonde: 3 notable likes, 1 repost"
    for name in ("Ana", "Bo", "Cy", "Di", "example.com"):
        assert name not in subj


async def test_it_goes_to_the_arrival_alert_address(monkeypatch, bsky, outbox):
    configure(monkeypatch, verified_alert_to="phone@example.com")
    await started()
    bsky.add(verified("did:plc:v"), "like", pacific(9, 50))

    await run(bsky, pacific(10))

    assert outbox[0]["To"] == "phone@example.com"
    assert outbox[0].get_content_type() == "multipart/alternative"


async def test_post_text_is_fetched_once_and_cached(bsky, outbox):
    await started()
    bsky.add(verified("did:plc:v1"), "like", pacific(9, 50))
    await run(bsky, pacific(10))
    bsky.add(verified("did:plc:v2"), "like", pacific(10, 5))
    await run(bsky, pacific(10, 15))

    assert len(outbox) == 2
    assert "First line of post A" in body(outbox[1])
    assert len(bsky.calls("app.bsky.feed.getPosts")) == 1


# ------------------------------------------------- it never reports the past

async def test_the_first_enabled_run_alerts_nothing_earlier(bsky, outbox):
    bsky.add(verified("did:plc:old"), "like", pacific(9, 30))
    result = await run(bsky, pacific(9, 45))
    assert result["sent"] == 0 and outbox == []

    bsky.add(verified("did:plc:new"), "like", pacific(9, 50))
    await run(bsky, pacific(10))

    assert len(outbox) == 1
    assert "@new." in body(outbox[0]) and "@old." not in body(outbox[0])


async def test_a_like_from_before_the_first_run_backfilled_later_is_not_emailed(
        bsky, outbox):
    """The 6-hourly sync stores older likes after the watermark was stamped:
    higher ids, earlier times. They are the past, however late they arrive."""
    await run(bsky, pacific(10))            # first enabled run: the watermark

    old = verified("did:plc:old")
    await store.upsert_actor(old)
    await store.record_interactions([{
        "did": "did:plc:old", "direction": "inbound", "kind": "like",
        "uri": "at://did:plc:old/app.bsky.feed.like/backfilled", "subject": POST_A,
        "thread": POST_A, "occurred_at": stamp(pacific(9))}])
    await store.commit()
    bsky.add(verified("did:plc:new"), "like", pacific(10, 5))

    await run(bsky, pacific(10, 15))

    assert len(outbox) == 1
    assert "@new." in body(outbox[0]) and "@old." not in body(outbox[0])


async def test_a_disabled_run_stamps_no_watermark(monkeypatch, bsky, outbox):
    configure(monkeypatch, enable_interaction_alerts=False)
    bsky.add(verified("did:plc:v"), "like", pacific(9, 50))

    result = await run(bsky, pacific(10))

    assert result["skipped"] == "disabled"
    assert await store.get_meta(ia.WATERMARK_KEY) is None
    assert await store.get_meta(ia.WATERMARK_AT_KEY) is None
    assert outbox == []


async def test_no_smtp_stamps_no_watermark(monkeypatch, bsky, outbox):
    configure(monkeypatch, smtp_host="")
    bsky.add(verified("did:plc:v"), "like", pacific(9, 50))

    await run(bsky, pacific(10))

    assert await store.get_meta(ia.WATERMARK_KEY) is None


async def test_the_switch_is_off_by_default(monkeypatch):
    from sonde.config import Settings

    monkeypatch.delenv("ENABLE_INTERACTION_ALERTS", raising=False)
    assert Settings().enable_interaction_alerts is False


async def test_older_than_the_lookback_is_not_emailed(bsky, outbox):
    await started()
    bsky.add(verified("did:plc:v"), "like", pacific(9, 50))

    # The same like, first seen by a run 49 hours later (an outage).
    result = await run(bsky, pacific(9, 50) + timedelta(hours=49))

    assert result["sent"] == 0 and outbox == []


# ------------------------------------------------------------- never twice

async def test_the_same_like_is_never_emailed_twice(bsky, outbox):
    await started()
    bsky.add(verified("did:plc:v"), "like", pacific(9, 50))

    await run(bsky, pacific(10))
    await run(bsky, pacific(10, 15))

    assert len(outbox) == 1


async def test_a_failed_send_is_retried_then_given_up_on(monkeypatch, bsky, outbox):
    await started()
    bsky.add(verified("did:plc:v"), "like", pacific(9, 50))
    good_send = ia.send

    async def boom(message):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ia, "send", boom)
    result = await run(bsky, pacific(10))
    assert result["status"] == "failed" and result["pending"] == 1

    monkeypatch.setattr(ia, "send", good_send)
    assert (await run(bsky, pacific(10, 15)))["retried"] == 1
    assert len(outbox) == 1

    # A permanently broken mailer: attempts run out, then it stops.
    bsky.add(verified("did:plc:w"), "like", pacific(10, 20))
    monkeypatch.setattr(ia, "send", boom)
    for i in range(store.ALERT_MAX_ATTEMPTS):
        await run(bsky, pacific(10, 30 + i))
    monkeypatch.setattr(ia, "send", good_send)
    assert (await run(bsky, pacific(11)))["sent"] == 0
    assert len(outbox) == 1
    given_up = await store.abandoned_interaction_alerts()
    assert [g["did"] for g in given_up] == ["did:plc:w"]


# ------------------------------------------------------------- quiet hours

async def test_nothing_is_sent_at_night_and_the_night_arrives_at_seven(bsky, outbox):
    await started()
    bsky.add(verified("did:plc:late", displayName="Late"), "like",
             pacific(23, 20, days=-1))

    result = await run(bsky, pacific(23, 30, days=-1))
    assert result["sent"] == 0 and outbox == []

    bsky.add(verified("did:plc:early", displayName="Early"), "repost", pacific(5, 0))
    await run(bsky, pacific(6, 45))
    assert outbox == []

    result = await run(bsky, pacific(7, 0))
    assert result["sent"] == 2 and len(outbox) == 1
    assert "Late" in body(outbox[0]) and "Early" in body(outbox[0])


# ------------------------------------------------------- reading Bluesky

async def test_a_failed_notifications_read_does_not_move_the_marker(bsky, outbox):
    from sonde.sync import notifications

    await started()
    bsky.add(verified("did:plc:v"), "like", pacific(9, 50))
    bsky.fail_notifications = True

    result = await run(bsky, pacific(10))

    assert result["check"]["status"] == "failed"
    assert result["status"] == "failed"
    assert await store.get_meta(notifications.MARKER_KEY) is None
    assert outbox == []

    bsky.fail_notifications = False
    await run(bsky, pacific(10, 15))
    assert len(outbox) == 1


async def test_update_seen_is_never_requested(bsky, outbox):
    await started()
    bsky.add(verified("did:plc:v"), "like", pacific(9, 50))

    await run(bsky, pacific(10))
    await run(bsky, pacific(23, 30))

    assert bsky.requests
    assert not [r for r in bsky.requests if "updateSeen" in r.url.path]
    assert all(r.method == "GET" for r in bsky.requests)


async def test_each_line_carries_the_accounts_bio(bsky, outbox):
    """Who someone is matters as much as how many follow them (Dan, 2026-11-24)."""
    await started()
    bsky.add(verified("did:plc:nobio", handle="bo.example.com", displayName="Bo"),
             "like", pacific(9, 40))
    bsky.add(verified("did:plc:bio", handle="ana.example.com", displayName="Ana",
                      description="Writes about trains.\nSecond line, left out."),
             "like", pacific(9, 41))
    bsky.add(verified("did:plc:long", handle="cy.example.com", displayName="Cy",
                      description="x" * 200),
             "like", pacific(9, 42))

    await run(bsky, pacific(10))

    lines = body(outbox[0]).splitlines()
    at = {name: next(i for i, line in enumerate(lines) if f"{name} (@" in line)
          for name in ("Ana", "Bo", "Cy")}
    # No bio: the next line is the next person, not a stray indented line.
    assert at["Ana"] == at["Bo"] + 1
    # A bio: its first line only, quoted so it can't pass for sonde's own text.
    assert lines[at["Ana"] + 1] == "    “Writes about trains.”"
    assert "Second line" not in "\n".join(lines)
    # A long bio says it was cut.
    assert lines[at["Cy"] + 1] == f"    “{'x' * 160}…”"


# --------------------------------------------- the HTML alternative (sonde#16)

def test_the_like_email_carries_an_html_alternative():
    rows = [{"interaction_id": 1, "did": "did:plc:a", "kind": "like",
             "subject": POST_A, "occurred_at": "2026-11-26T17:09:53.000Z",
             "handle": "ana.example.com", "display_name": "Ana",
             "description": None, "followers_count": 41_000,
             "verified_status": "valid", "trusted_verifier_status": "none",
             "attempts": 0, "avatar_url": "https://cdn.example/a.jpg"}]
    message = ia.build_message(rows, {POST_A: "First line"})
    assert message.get_body(("html",)) is not None


def test_the_plain_text_part_is_unchanged():
    rows = [{"interaction_id": 1, "did": "did:plc:a", "kind": "like",
             "subject": POST_A, "occurred_at": "2026-11-26T17:09:53.000Z",
             "handle": "ana.example.com", "display_name": "Ana",
             "description": None, "followers_count": 41_000,
             "verified_status": "valid", "trusted_verifier_status": "none",
             "attempts": 0}]
    previews = {POST_A: "First line"}
    message = ia.build_message(rows, previews)
    assert (message.get_body(("plain",)).get_content().rstrip("\n")
            == ia.render_text(rows, previews))


async def test_a_run_attaches_an_avatar(bsky, outbox, monkeypatch):
    async def fake(rows):
        return {"did:plc:v": b"\xff\xd8\xff\xd9"}

    monkeypatch.setattr(ia, "avatars", fake)
    await started()
    bsky.add(verified("did:plc:v", handle="v.bsky.social"), "like", pacific(9, 40))

    await run(bsky, pacific(10))

    images = [p for p in outbox[0].walk() if p.get_content_type() == "image/jpeg"]
    assert len(images) == 1
    assert images[0]["Content-Disposition"].startswith("inline")


async def test_a_gated_run_still_reads_notifications(bsky, outbox):
    """The 30-minute interval gates email, not the read the digest depends on."""
    await started()
    bsky.add(verified("did:plc:v"), "like", pacific(9, 40))

    result = await ia.run_interaction_alerts(bsky.client, now=pacific(10), send=False)

    assert result["skipped"] == "inside the alert interval"
    assert outbox == []
    rows = await store.notable_interactions_since("2000-01-01T00:00:00+00:00")
    assert [r["did"] for r in rows] == ["did:plc:v"]


async def test_a_broken_avatar_does_not_stop_the_like_email(bsky, outbox, monkeypatch):
    """One person's undecodable picture must not silence every like alert."""
    async def boom(rows):
        raise RuntimeError("Pillow fell over")

    monkeypatch.setattr(ia, "avatars", boom)
    await started()
    bsky.add(verified("did:plc:v"), "like", pacific(9, 40))

    result = await ia.run_interaction_alerts(bsky.client, now=pacific(10))

    assert result["sent"] == 1
    assert len(outbox) == 1
