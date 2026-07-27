"""Daily email digest.

Sent once a day at 14:00 America/Los_Angeles. Two decisions shape what goes in
it:

**It reports, it does not editorialise.** Every figure is one already stored;
nothing is inferred for the email that isn't true in the app.

**A quiet day sends nothing, but a broken day always sends.** A daily "nothing
happened" email trains you to ignore the one that matters. But silence is
ambiguous — did nothing happen, or did the app die? — so health problems send
regardless of whether there is follower news, and the digest always states when
the last successful sweep was.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sonde.config import settings
from sonde.db import store

log = logging.getLogger("sonde.digest")

# A sweep every 6h; more than this since the last good one means something is wrong.
STALE_SYNC_HOURS = 14


@dataclass
class Digest:
    arrivals: list[dict] = field(default_factory=list)
    departures: list[dict] = field(default_factory=list)
    returns: list[dict] = field(default_factory=list)
    renames: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    last_sync_hours: float | None = None
    since: str = ""

    @property
    def has_news(self) -> bool:
        return bool(self.arrivals or self.departures or self.returns or self.renames)

    @property
    def worth_sending(self) -> bool:
        """Quiet days stay quiet; broken days always speak up."""
        return self.has_news or bool(self.problems)

    @property
    def subject(self) -> str:
        if self.problems and not self.has_news:
            return f"sonde: {len(self.problems)} thing(s) need attention"
        bits = []
        if self.arrivals:
            bits.append(f"+{len(self.arrivals)}")
        if self.departures:
            bits.append(f"-{len(self.departures)}")
        summary = " ".join(bits) or "no change"
        warn = f" · {len(self.problems)} warning(s)" if self.problems else ""
        return f"sonde: {summary} · {self.counts.get('tracked', 0):,} followers{warn}"


async def gather(hours: int = 24) -> Digest:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_iso = since.isoformat(timespec="seconds")

    digest = Digest(since=since_iso)
    digest.counts = await store.counts()

    events = await store.events_since(since_iso)
    by_kind: dict[str, list[dict]] = {}
    for event in events:
        by_kind.setdefault(event["event"], []).append(event)

    # Arrivals worth reading first: rank by influence, not arrival order.
    digest.arrivals = sorted(
        by_kind.get("followed", []),
        key=lambda e: (e.get("influence_score") or 0), reverse=True,
    )
    digest.departures = sorted(
        by_kind.get("departed", []),
        key=lambda e: (e.get("followers_count") or 0), reverse=True,
    )
    digest.returns = by_kind.get("returned", [])
    digest.renames = by_kind.get("handle_changed", [])

    # Health. These are the things that fail silently.
    age = await store.last_sync_age_seconds()
    digest.last_sync_hours = round(age / 3600, 1) if age is not None else None
    if age is None:
        digest.problems.append("No sweep has ever completed successfully.")
    elif age > STALE_SYNC_HOURS * 3600:
        digest.problems.append(
            f"Last successful sweep was {digest.last_sync_hours:.0f}h ago — "
            f"expected one every {settings.full_sweep_hours}h."
        )

    needs_review = await store.get_meta("needs_review_count")
    if needs_review:
        digest.problems.append(
            f"A sweep was held for review: it would have marked {needs_review} "
            f"followers as departed, above the safety threshold. No events were "
            f"written. Accept or investigate on /settings."
        )

    for run in await store.recent_runs(12):
        if run["status"] == "failed":
            digest.problems.append(
                f"The last {run['kind']} run failed: {(run['error'] or '')[:120]}"
            )
            break

    from sonde.api.auth import authenticator

    status = authenticator.status()
    if status["configured"] and not status["authenticated"]:
        digest.problems.append(
            f"Bluesky app password is set but not authenticating"
            f"{' — ' + status['error'] if status['error'] else ''}. "
            f"Follow dates and exact relevance are being skipped."
        )
    return digest


def _line(person: dict) -> str:
    handle = person.get("handle") or person.get("did", "?")
    name = person.get("display_name") or ""
    followers = person.get("followers_count")
    score = person.get("influence_score")
    bits = [f"@{handle}"]
    if name:
        bits.append(name)
    if followers is not None:
        bits.append(f"{followers:,} followers")
    if score is not None:
        bits.append(f"score {score:.0f}")
    if person.get("verified_status") == "valid":
        bits.append("verified")
    return " · ".join(bits)


def render_text(digest: Digest, limit: int = 25) -> str:
    out: list[str] = []
    counts = digest.counts

    if digest.problems:
        out.append("NEEDS ATTENTION")
        out += [f"  - {p}" for p in digest.problems]
        out.append("")

    reported = counts.get("reported")
    tracked = counts.get("tracked", 0)
    out.append(
        f"{tracked:,} followers tracked"
        + (f" ({reported:,} reported by Bluesky — the gap is permanent)" if reported else "")
    )
    out.append(
        f"{counts.get('verified', 0):,} verified · "
        f"{counts.get('mutuals', 0):,} mutuals · "
        f"{counts.get('ignored', 0):,} hidden"
    )
    if digest.last_sync_hours is not None:
        out.append(f"Last successful sweep {digest.last_sync_hours:.1f}h ago.")
    out.append("")

    def section(title: str, rows: list[dict]) -> None:
        if not rows:
            return
        out.append(f"{title} ({len(rows)})")
        for person in rows[:limit]:
            out.append(f"  {_line(person)}")
        if len(rows) > limit:
            out.append(f"  … and {len(rows) - limit} more")
        out.append("")

    section("NEW FOLLOWERS", digest.arrivals)
    section("DEPARTED", digest.departures)
    section("RETURNED", digest.returns)

    if digest.renames:
        out.append(f"CHANGED HANDLE ({len(digest.renames)})")
        for person in digest.renames[:limit]:
            out.append(f"  {person.get('detail') or _line(person)}")
        out.append("")

    if not digest.has_news:
        out.append("No follower changes in the last 24 hours.")
        out.append("")

    out.append(
        "Departures are labelled 'gone' rather than 'unfollowed' when the cause "
        "cannot be told apart — deactivation, deletion, suspension and blocks all "
        "look identical from outside."
    )
    out.append("")
    out.append(f"https://{settings.service_host}/")
    return "\n".join(out)


def build_message(digest: Digest) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = digest.subject
    message["From"] = settings.smtp_username
    message["To"] = settings.notify_to
    message.set_content(render_text(digest))
    return message


async def send(digest: Digest) -> bool:
    """Returns True if an email actually went out."""
    import aiosmtplib

    if not settings.smtp_configured:
        log.info("digest not sent — SMTP is not configured")
        return False
    await aiosmtplib.send(
        build_message(digest),
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        use_tls=settings.smtp_port == 465,
        start_tls=settings.smtp_port != 465,
    )
    return True


async def run_digest(force: bool = False) -> dict:
    run_id = await store.start_run("digest")
    try:
        digest = await gather()
        should = force or digest.worth_sending
        sent = await send(digest) if should else False
        if should and not sent:
            log.warning("digest had content but SMTP is not configured")
        await store.finish_run(
            run_id, status="ok", completed=1,
            actors_seen=len(digest.arrivals) + len(digest.departures),
        )
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc))
        log.exception("digest failed")
        raise
    return {
        "status": "ok", "kind": "digest", "sent": sent,
        "arrivals": len(digest.arrivals), "departures": len(digest.departures),
        "problems": len(digest.problems),
        "skipped": None if should else "nothing to report",
    }
