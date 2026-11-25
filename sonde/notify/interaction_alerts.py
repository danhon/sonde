"""Email every notable like or repost of the operator's posts (sonde#11).

The arrival alert answers "someone notable just followed you". This answers
"someone notable just paid attention to a post", within about 15 minutes: the
scheduler runs it after each head sweep, and each run first does the light
notifications check (`sonde.sync.notifications`) and then emails whatever
notable likes and reposts still owe an email.

It keeps the arrival alert's rules (see `alerts.py` and CLAUDE.md, "Alerting
never reports the past, and claims before it sends"):

**It never reports the past.** Its own watermark, the highest interaction id,
is stamped on the first run with the switch on and SMTP configured, and nothing
at or below it is ever emailed. A lookback window bounds it from the other
side: after an outage, old likes are left to the digest.

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
from sonde.notify.alerts import why_notable
from sonde.sync.notifications import check_notifications

log = logging.getLogger("sonde.interaction_alerts")

# A floor, not a cursor: set once, on the first enabled run, then left alone.
WATERMARK_KEY = "interaction_alert_watermark"

PACIFIC = ZoneInfo("America/Los_Angeles")
QUIET_FROM = time(23, 0)
QUIET_UNTIL = time(7, 0)

# getPosts accepts at most 25 URIs a call.
POSTS_BATCH = 25


def in_quiet_hours(now: datetime) -> bool:
    local = now.astimezone(PACIFIC).time()
    return local >= QUIET_FROM or local < QUIET_UNTIL


async def _watermark() -> int:
    """The interaction id alerting started from, stamped on first enabled use."""
    raw = await store.get_meta(WATERMARK_KEY)
    if raw:
        try:
            return int(raw)
        except ValueError:
            log.warning("unparseable interaction alert watermark %r; restamping", raw)
    latest = await store.max_interaction_id()
    await store.set_meta(WATERMARK_KEY, str(latest))
    log.info("like and repost alerts start after interaction id %d", latest)
    return latest


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


def describe(row: dict) -> str:
    handle = row.get("handle") or row["did"]
    who = f"{row['display_name']} (@{handle})" if row.get("display_name") else f"@{handle}"
    verb = "liked" if row["kind"] == "like" else "reposted"
    why = ", ".join(why_notable(row)) or "notable"
    return f"  {who} · {why} · {verb} · {_when(row['occurred_at'])}"


def render_text(rows: list[dict], previews: dict[str, str | None]) -> str:
    by_post: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_post[row.get("subject") or ""].append(row)

    out = [f"{subject(rows).removeprefix('sonde: ').capitalize()} on your posts.", ""]
    for uri, events in by_post.items():
        first = (previews.get(uri) or "").strip().splitlines()
        out.append(f"“{first[0][:160]}”" if first else "A post of yours")
        out.append(f"  {post_link(uri)}")
        out += [describe(r) for r in events]
        out.append("")
    out.append(
        "Notable means verified, a trusted verifier, or more than "
        f"{settings.large_account_followers:,} followers."
    )
    return "\n".join(out)


def build_message(rows: list[dict], previews: dict[str, str | None]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject(rows)
    message["From"] = settings.smtp_username
    message["To"] = settings.alert_to
    message.set_content(render_text(rows, previews))
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


async def _previews(client: BlueskyClient, uris: list[str]) -> dict[str, str | None]:
    """Each post's text, from the cache or getPosts. Best effort: a failed
    lookup costs the first line, never the email."""
    cached = await store.post_previews(uris)
    missing = [u for u in uris if u and u not in cached]
    fetched: dict[str, str | None] = {}
    try:
        for i in range(0, len(missing), POSTS_BATCH):
            batch = missing[i:i + POSTS_BATCH]
            data = await client.xrpc("app.bsky.feed.getPosts", {"uris": batch})
            for post in data.get("posts") or []:
                if post.get("uri") in batch:
                    fetched[post["uri"]] = (post.get("record") or {}).get("text")
    except Exception as exc:  # noqa: BLE001
        log.warning("getPosts failed; alerting without post text: %s", exc)
    await store.save_post_previews(fetched)
    return {**cached, **fetched}


# ------------------------------------------------------------------ the run

async def run_interaction_alerts(client: BlueskyClient | None = None, *,
                                 now: datetime | None = None) -> dict:
    """Read notifications, then email every notable like or repost owed one."""
    owns_client = client is None
    client = client or BlueskyClient()
    now = now or datetime.now(timezone.utc)
    try:
        # Always read: the digest's record of notable likes needs the rows
        # and the follower counts even while the email switch is off.
        check = await check_notifications(client)
        result = {"status": "ok", "kind": "like-alerts", "sent": 0,
                  "check": check}

        if not settings.interaction_alerts_configured:
            # No watermark here on purpose: switching on later stays quiet.
            result["skipped"] = ("disabled" if not settings.enable_interaction_alerts
                                 else "SMTP not configured")
            return result

        after = await _watermark()
        if in_quiet_hours(now):
            result["skipped"] = "quiet hours"
            return result
        return {**result, **await _alert(client, now, after)}
    finally:
        if owns_client:
            await client.aclose()


async def _alert(client: BlueskyClient, now: datetime, after: int) -> dict:
    run_id = await store.start_run("like-alerts")
    try:
        since = now - timedelta(hours=settings.interaction_alert_lookback_hours)
        rows = await store.interaction_alert_candidates(
            since_iso=since.astimezone(timezone.utc).isoformat(timespec="seconds"),
            after_id=after)
        if not rows:
            await store.finish_run(run_id, status="ok", completed=1)
            return {"skipped": "no notable likes or reposts"}

        retries = sum(1 for r in rows if r["attempts"])
        previews = await _previews(
            client, list(dict.fromkeys(r["subject"] for r in rows if r["subject"])))
        message = build_message(rows, previews)
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
