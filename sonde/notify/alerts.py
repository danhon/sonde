"""Email the moment a verified account follows you.

The daily digest answers "what happened yesterday". This answers "someone worth
knowing about just arrived", which is a different question with a different
useful latency — a head sweep runs every 15 minutes, so that is how fast this
is. Detection is free: `verification` rides along in the same getFollowers
payload the sweep already fetched, so nothing here costs an API call.

Four rules, each of them a way this feature could have been annoying:

**It never reports the past.** The first run with alerting actually enabled
stamps a watermark at the current end of the event log and alerts nothing
before it. Without that, deploying the feature — or switching it on months
later — would open with an email about every verified follower who happened to
arrive that week. A lookback window bounds it from the other side, so an outage
cannot produce a backlog dump either.

**It claims before it sends.** The `verified_alerts` row goes in first, so a
crash mid-send costs a missed alert rather than a duplicate one. Missing is the
cheaper failure because it is backstopped: the same arrival appears in the daily
digest, and an alert that exhausts its attempts is reported there as a problem.

**One email per run.** Twelve verified arrivals in one sweep is one email
listing ten of them, not twelve emails.

**Hidden followers stay hidden.** Something the moderation lists took out of
every table has no business arriving in your inbox instead.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sonde.config import settings
from sonde.db import store

log = logging.getLogger("sonde.alerts")

# Where "before the feature existed" is remembered. Set once, then left alone:
# it is a floor, not a cursor, and the lookback window does the ongoing work.
WATERMARK_KEY = "verified_alert_watermark"


async def _watermark() -> int:
    """The event id alerting started from, stamped on first enabled use.

    Deliberately not stamped when alerting is off, so turning it on later is
    quiet rather than retroactive.
    """
    raw = await store.get_meta(WATERMARK_KEY)
    if raw:
        try:
            return int(raw)
        except ValueError:
            log.warning("unparseable alert watermark %r; restamping", raw)
    latest = await store.max_event_id()
    await store.set_meta(WATERMARK_KEY, str(latest))
    log.info("verified alerts start from event id %d", latest)
    return latest


def issuers(row: dict) -> list[str]:
    """Who vouched for them, best-effort.

    `verification` is omitted entirely for unverified accounts rather than set
    to "none", and a valid verifiedStatus does not guarantee an exposed record,
    so every layer here is optional.
    """
    records = row.get("verification_records")
    if records is None:
        records = json.loads(row.get("verifications") or "[]")
    names = [
        r.get("issuerHandle") or r.get("issuer")
        for r in records
        if isinstance(r, dict) and r.get("isValid")
    ]
    return [n for n in names if n]


def describe(row: dict) -> list[str]:
    """The lines describing one arrival."""
    handle = row.get("handle") or row.get("did", "?")
    head = [f"@{handle}"]
    if row.get("display_name"):
        head.append(row["display_name"])
    if row.get("followers_count") is not None:
        head.append(f"{row['followers_count']:,} followers")
    if row.get("influence_score") is not None:
        head.append(f"score {row['influence_score']:.0f}")

    who = issuers(row)
    if who:
        head.append("verified by " + ", ".join(sorted(set(who))))
    elif row.get("verified_status") == "valid":
        # Valid status, no record exposed. Saying "verified by nobody" would be
        # wrong; saying nothing would lose the reason the email exists.
        head.append("verified")
    if row.get("trusted_verifier_status") == "valid":
        head.append("a trusted verifier")
    if row.get("event") == "returned":
        head.append("returned — they had followed before")

    lines = [" · ".join(head)]
    bio = (row.get("description") or "").strip().splitlines()
    if bio:
        lines.append(f"  {bio[0][:160]}")
    lines.append(f"  https://{settings.service_host}/followers/{row['did']}")
    lines.append(f"  https://bsky.app/profile/{handle}")
    return lines


def subject(rows: list[dict]) -> str:
    if len(rows) == 1:
        handle = rows[0].get("handle") or rows[0].get("did", "?")
        return f"sonde: verified follower — @{handle}"
    return f"sonde: {len(rows)} verified followers"


def render_text(rows: list[dict], limit: int | None = None) -> str:
    limit = settings.verified_alert_max_listed if limit is None else limit
    out: list[str] = []
    out.append(
        "A verified account just followed you."
        if len(rows) == 1
        else f"{len(rows)} verified accounts just followed you."
    )
    out.append("")
    for row in rows[:limit]:
        out += describe(row)
        out.append("")
    if len(rows) > limit:
        out.append(f"… and {len(rows) - limit} more.")
        out.append("")
    out.append(
        "Verification is Bluesky's, not a judgement about the person — it says "
        "an issuer vouched for who they are, nothing more."
    )
    out.append("")
    out.append(f"https://{settings.service_host}/verified")
    return "\n".join(out)


def build_message(rows: list[dict]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject(rows)
    message["From"] = settings.smtp_username
    message["To"] = settings.alert_to
    message.set_content(render_text(rows))
    return message


async def send(rows: list[dict]) -> bool:
    """Returns True if an email actually went out."""
    import aiosmtplib

    await aiosmtplib.send(
        build_message(rows),
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        use_tls=settings.smtp_port == 465,
        start_tls=settings.smtp_port != 465,
    )
    return True


async def run_alerts() -> dict:
    """Send one email covering every verified arrival that still owes one."""
    if not settings.verified_alerts_configured:
        # No watermark is stamped here on purpose — see _watermark().
        reason = (
            "disabled" if not settings.enable_verified_alerts else "SMTP not configured"
        )
        log.debug("verified alerts skipped — %s", reason)
        return {"status": "ok", "kind": "alerts", "sent": 0, "skipped": reason}

    run_id = await store.start_run("alerts")
    try:
        after = await _watermark()
        since = datetime.now(timezone.utc) - timedelta(
            hours=settings.verified_alert_lookback_hours
        )
        rows = await store.alert_candidates(
            since_iso=since.isoformat(timespec="seconds"), after_event_id=after
        )
        if not rows:
            await store.finish_run(run_id, status="ok", completed=1)
            return {"status": "ok", "kind": "alerts", "sent": 0,
                    "skipped": "no verified arrivals"}

        retries = sum(1 for r in rows if r["attempts"])
        await store.claim_alerts(rows)
        event_ids = [r["event_id"] for r in rows]
        try:
            await send(rows)
        except Exception as exc:  # noqa: BLE001 — recorded, then retried later
            await store.mark_alerts_failed(event_ids, str(exc))
            await store.finish_run(run_id, status="failed", error=str(exc))
            log.warning("verified alert send failed for %d arrival(s): %s",
                        len(rows), exc)
            return {"status": "failed", "kind": "alerts", "sent": 0,
                    "pending": len(rows), "error": str(exc)}

        await store.mark_alerts_sent(event_ids)
        await store.finish_run(run_id, status="ok", completed=1, actors_seen=len(rows))
        log.info("emailed %d verified arrival(s)", len(rows))
        return {"status": "ok", "kind": "alerts", "sent": len(rows),
                "retried": retries}
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc))
        log.exception("verified alerts failed")
        raise
