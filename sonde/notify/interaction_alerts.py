"""Email every notable like or repost of the operator's posts (sonde#11).

The arrival alert answers "someone notable just followed you". This answers
"someone notable just paid attention to a post", within about 15 minutes: the
scheduler runs it after each head sweep, and each run first does the light
notifications check (`sonde.sync.notifications`) and then emails whatever
notable likes and reposts still owe an email.

It keeps the arrival alert's rules (see `alerts.py` and CLAUDE.md, "Alerting
never reports the past, and claims before it sends"):

**It never reports the past.** Its own watermark, the highest interaction id
and the moment it was taken, is stamped on the first run with the switch on and
SMTP configured. Nothing at or below that id is ever emailed, and nothing that
happened before that moment either: the 6-hourly sync backfills older likes
later, with higher ids. A lookback window bounds it from the other side: after
an outage, old likes are left to the digest.

**It claims before it sends.** The `interaction_alerts` row goes in first, so
a crash mid-send costs a missed email rather than a duplicate. A failed send
is retried on later runs and given up on after `store.ALERT_MAX_ATTEMPTS`; the
digest reports what was given up on.

And three of its own:

**Every event is listed.** No "and N more": the operator asked for every like,
including repeat ones from the same person (claims key on the interaction).

**Nothing between 23:00 and 07:00 Pacific.** A run in quiet hours still reads
notifications but claims and sends nothing, so the first run at or after 07:00
sends the night in one email.

**The subject carries counts, never names**, because it shows on a lock screen.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sonde.api.client import BlueskyClient
from sonde.config import settings
from sonde.db import store
from sonde.notify import email_html
from sonde.notify.alerts import why_notable
from sonde.notify.avatars import avatars
from sonde.sync.notifications import check_notifications, fetch_previews

log = logging.getLogger("sonde.interaction_alerts")

# A floor, not a cursor: set once, on the first enabled run, then left alone.
WATERMARK_KEY = "interaction_alert_watermark"
# When it was stamped. Ids alone are not time: a like the sync backfills after
# the stamp gets a higher id than one the check stored before it.
WATERMARK_AT_KEY = "interaction_alert_watermark_at"

PACIFIC = ZoneInfo("America/Los_Angeles")
QUIET_FROM = time(23, 0)
QUIET_UNTIL = time(7, 0)


def in_quiet_hours(now: datetime) -> bool:
    local = now.astimezone(PACIFIC).time()
    return local >= QUIET_FROM or local < QUIET_UNTIL


async def _watermark(now: datetime) -> tuple[int, datetime | None]:
    """The interaction id and moment alerting started from, stamped together
    on first enabled use. The moment is None only for a watermark stamped
    before it was recorded."""
    raw = await store.get_meta(WATERMARK_KEY)
    if raw:
        try:
            after = int(raw)
        except ValueError:
            log.warning("unparseable interaction alert watermark %r; restamping", raw)
        else:
            stamped = await store.get_meta(WATERMARK_AT_KEY)
            try:
                return after, datetime.fromisoformat(stamped) if stamped else None
            except ValueError:
                log.warning("unparseable watermark time %r; ignoring it", stamped)
                return after, None
    latest = await store.max_interaction_id()
    stamped_at = now.astimezone(timezone.utc)
    await store.set_meta(WATERMARK_AT_KEY, stamped_at.isoformat(timespec="seconds"))
    await store.set_meta(WATERMARK_KEY, str(latest))
    log.info("like and repost alerts start after interaction id %d, %s",
             latest, stamped_at.isoformat(timespec="seconds"))
    return latest, stamped_at


# ------------------------------------------------------------- the email

def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def subject(rows: list[dict]) -> str:
    """Counts only: the subject shows on a lock screen."""
    likes = sum(1 for r in rows if r["kind"] == "like")
    reposts = sum(1 for r in rows if r["kind"] == "repost")
    parts = []
    if likes:
        parts.append(_plural(likes, "like"))
    if reposts:
        parts.append(_plural(reposts, "repost"))
    # "3 notable likes, 1 repost"
    parts[0] = parts[0].replace(" ", " notable ", 1)
    return "sonde: " + ", ".join(parts)


def post_link(uri: str) -> str:
    """at://did/app.bsky.feed.post/rkey → its bsky.app page."""
    parts = (uri or "").removeprefix("at://").split("/")
    if len(parts) == 3 and parts[1] == "app.bsky.feed.post":
        return f"https://bsky.app/profile/{parts[0]}/post/{parts[2]}"
    return uri or ""


def _when(occurred_at: str) -> str:
    try:
        moment = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError:
        return occurred_at
    local = moment.astimezone(PACIFIC)
    hour = local.hour % 12 or 12
    return f"{local:%a} {hour}:{local:%M} {'am' if local.hour < 12 else 'pm'} PT"


def describe(row: dict) -> list[str]:
    """One event: who, why notable, what, when; then the first line of their
    bio, as the arrival alert gives it, so the name means something."""
    handle = row.get("handle") or row["did"]
    who = f"{row['display_name']} (@{handle})" if row.get("display_name") else f"@{handle}"
    verb = "liked" if row["kind"] == "like" else "reposted"
    why = ", ".join(why_notable(row)) or "notable"
    lines = [f"  {who} · {why} · {verb} · {_when(row['occurred_at'])}"]
    # Quoted, so text the account wrote can't pass for a line sonde wrote.
    bio = (row.get("description") or "").strip().splitlines()
    if bio:
        first = bio[0].strip()
        lines.append(f"    “{first[:160]}{'…' if len(first) > 160 else ''}”")
    return lines


def render_text(rows: list[dict], previews: dict[str, str | None]) -> str:
    by_post: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_post[row.get("subject") or ""].append(row)

    out = [f"{subject(rows).removeprefix('sonde: ').capitalize()} on your posts.", ""]
    for uri, events in by_post.items():
        first = (previews.get(uri) or "").strip().splitlines()
        out.append(f"“{first[0][:160]}”" if first else "A post of yours")
        out.append(f"  {post_link(uri)}")
        for r in events:
            out += describe(r)
        out.append("")
    out.append(
        "Notable means verified, a trusted verifier, or more than "
        f"{settings.large_account_followers:,} followers."
    )
    return "\n".join(out)


def build_message(rows: list[dict], previews: dict[str, str | None],
                  images: dict[str, bytes] | None = None) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject(rows)
    message["From"] = settings.smtp_username
    message["To"] = settings.alert_to
    # The plain part first and unchanged: a watch and a lock screen read it.
    message.set_content(render_text(rows, previews))
    email_html.attach(
        message,
        email_html.render_interactions(rows, previews, images or {}),
        images or {},
    )
    return message


async def send(message: EmailMessage) -> bool:
    """Returns True if an email actually went out."""
    import aiosmtplib

    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        use_tls=settings.smtp_port == 465,
        start_tls=settings.smtp_port != 465,
    )
    return True


# ------------------------------------------------------------------ the run

async def run_interaction_alerts(client: BlueskyClient | None = None, *,
                                 now: datetime | None = None,
                                 send: bool = True) -> dict:
    """Read notifications, then email every notable like or repost owed one.

    `send=False` still reads: the read is what feeds the digest's notable-likes
    section, and the 30-minute alert interval gates email, not work.
    """
    owns_client = client is None
    client = client or BlueskyClient()
    now = now or datetime.now(timezone.utc)
    try:
        # Always read: the digest's record of notable likes needs the rows
        # and the follower counts even while the email switch is off.
        check = await check_notifications(client)
        # A failed read is the run's failure too, so the caller sees it; what
        # is already stored is still emailed below.
        result = {"status": check["status"], "kind": "like-alerts", "sent": 0,
                  "check": check}

        if not settings.interaction_alerts_configured:
            # No watermark here on purpose: switching on later stays quiet.
            result["skipped"] = ("disabled" if not settings.enable_interaction_alerts
                                 else "SMTP not configured")
            return result

        after, stamped_at = await _watermark(now)
        if in_quiet_hours(now):
            result["skipped"] = "quiet hours"
            return result
        if not send:
            result["skipped"] = "inside the alert interval"
            return result
        return {**result, **await _alert(client, now, after, stamped_at)}
    finally:
        if owns_client:
            await client.aclose()


async def _alert(client: BlueskyClient, now: datetime, after: int,
                 stamped_at: datetime | None) -> dict:
    run_id = await store.start_run("like-alerts")
    try:
        since = now - timedelta(hours=settings.interaction_alert_lookback_hours)
        if stamped_at is not None:
            since = max(since, stamped_at)
        rows = await store.interaction_alert_candidates(
            since_iso=since.astimezone(timezone.utc).isoformat(timespec="seconds"),
            after_id=after)
        if not rows:
            await store.finish_run(run_id, status="ok", completed=1)
            return {"skipped": "no notable likes or reposts"}

        retries = sum(1 for r in rows if r["attempts"])
        previews = await fetch_previews(
            client, list(dict.fromkeys(r["subject"] for r in rows if r["subject"])))
        message = build_message(rows, previews, await avatars(rows))
        await store.claim_interaction_alerts(rows)
        ids = [r["interaction_id"] for r in rows]
        try:
            await send(message)
        except Exception as exc:  # noqa: BLE001 — recorded, then retried later
            await store.mark_interaction_alerts_failed(ids, str(exc))
            await store.finish_run(run_id, status="failed", error=str(exc))
            log.warning("like/repost alert send failed for %d event(s): %s",
                        len(rows), exc)
            return {"status": "failed", "pending": len(rows), "error": str(exc)}

        await store.mark_interaction_alerts_sent(ids)
        await store.finish_run(run_id, status="ok", completed=1, actors_seen=len(rows))
        log.info("emailed %d notable like(s)/repost(s)", len(rows))
        return {"sent": len(rows), "retried": retries}
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc))
        log.exception("like and repost alerts failed")
        raise
