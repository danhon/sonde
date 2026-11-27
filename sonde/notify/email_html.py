"""The HTML alternative both alert emails carry.

Two shapes, one family. A like or repost alert is cards: one per post of yours,
with the people who touched it underneath. An arrival alert is the same person
row without a post over it. Everything else — the avatar, the badge, the bio
line, the footer — is shared.

Email is not a browser, and the four things that bite are written down here
rather than rediscovered:

- **Tables and inline styles only.** No stylesheet, no `<style>` for layout, no
  flexbox: Gmail strips a `<head>` stylesheet and the rest is inconsistent.
- **`add_related()` goes on the HTML part, not the message.** On the message it
  raises `Cannot convert alternative to related`.
- **No `filename=` on a related part.** That sets
  `Content-Disposition: attachment` and the avatar arrives as a paperclip.
- **Stay small.** Gmail clips a message over 102 KB, so `MARKUP_BUDGET` is
  asserted by a test and avatars are 64px JPEGs.

The look is "sonde-light": sonde's palette and mono headings on white. No dark
variant, because Gmail ignores `prefers-color-scheme` and force-inverts instead;
the colours here are chosen to survive that rather than to be a dark theme.
"""

from __future__ import annotations

import functools
import hashlib
import io
from email.message import EmailMessage
from html import escape
from typing import Sequence

from PIL import Image, ImageDraw

from sonde.config import settings

# The two alert modules import this one, so these are imported inside the
# renderers rather than at module load: a top-level import would be a cycle.

# The real limit is Gmail's 102 KB clip; this is a guard so a template that
# grows is caught here rather than in someone's inbox. Sized for a 12-person
# alert (arrivals is capped there; likes are not, and a 30-event test pins the
# clip separately) — the blue check costs about 100 bytes per person.
MARKUP_BUDGET = 24_000

# One red for accents, slate for everything else, and no pure black or white so
# Gmail's inversion has something to interpolate from.
INK = "#0f172a"
MUTED = "#475569"
FAINT = "#94a3b8"
RULE = "#e2e8f0"
CARD = "#ffffff"
PAGE = "#f8fafc"
RED = "#dc2626"

MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

AVATAR = 44

# Bluesky's verified badge, drawn here rather than fetched: an email may not
# load a remote icon, and the check is one shared picture per message, not one
# per person. Two parts, not one: a 28px PNG shown at 14px, so a retina screen
# has something to draw.
CHECK_CID = "check@sonde"
CHECK_PX = 28
CHECK_SHOWN = 14
BLUESKY_BLUE = "#1d9bf0"


@functools.lru_cache(maxsize=1)
def check_png() -> bytes:
    """The blue check, as a transparent PNG.

    A circle rather than Bluesky's scalloped seal: at 14px the difference is
    not what anyone sees, and a faithful trace would be a binary asset in the
    repo for one icon.
    """
    scale = 4
    size = CHECK_PX * scale
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, size - 1, size - 1), fill=BLUESKY_BLUE)
    draw.line(
        [(size * 0.28, size * 0.52), (size * 0.44, size * 0.68),
         (size * 0.74, size * 0.34)],
        fill="white", width=int(size * 0.11), joint="curve",
    )
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def _check() -> str:
    # Kept terse on purpose: this repeats once per person, and the whole
    # message has a Gmail-clip budget.
    return (f'<img src="cid:{CHECK_CID}" width="{CHECK_SHOWN}" height="{CHECK_SHOWN}" '
            f'alt="verified" style="vertical-align:middle;margin-left:4px;">')


def cid_for(did: str) -> str:
    """A Content-ID token for one person's avatar, stable across emails."""
    return f"{hashlib.sha1(did.encode()).hexdigest()[:16]}@sonde"


def _initials(row: dict) -> str:
    name = (row.get("display_name") or "").strip()
    if name:
        words = [w for w in name.split() if w[:1].isalnum()]
        if words:
            return "".join(w[0].upper() for w in words[:2])
    handle = (row.get("handle") or "").strip()
    return handle[:1].upper() if handle else "?"


def _avatar(row: dict, images: dict[str, bytes]) -> str:
    if row.get("did") in images:
        return (
            f'<img src="cid:{cid_for(row["did"])}" width="{AVATAR}" height="{AVATAR}" '
            f'alt="" style="display:block;width:{AVATAR}px;height:{AVATAR}px;'
            f'border:0;border-radius:6px;">'
        )
    return (
        f'<div style="width:{AVATAR}px;height:{AVATAR}px;border-radius:6px;'
        f'background:#e2e8f0;color:#334155;font:600 15px/44px {SANS};'
        f'text-align:center;letter-spacing:0.02em;">{escape(_initials(row))}</div>'
    )


def _link(href: str, text: str, *, color: str = RED) -> str:
    return (f'<a href="{escape(href, quote=True)}" '
            f'style="color:{color};text-decoration:none;">{escape(text)}</a>')


def _bio(row: dict) -> str:
    first = (row.get("description") or "").strip().splitlines()
    if not first or not first[0].strip():
        return ""
    line = first[0].strip()
    clipped = line[:160] + ("…" if len(line) > 160 else "")
    return (f'<div style="font:italic 12px/1.45 {SANS};color:{MUTED};'
            f'padding-top:2px;">“{escape(clipped)}”</div>')


def _person(row: dict, images: dict[str, bytes], *, fact: str) -> str:
    """One person: picture, name, what they did and why it mattered."""
    handle = row.get("handle") or row.get("did", "")
    name = row.get("display_name") or f"@{handle}"
    name_extra = (f' <span style="font-weight:400;color:{MUTED};">@{escape(handle)}</span>'
                  if row.get("display_name") else "")
    links = " · ".join(
        part for part in (
            _link(f"https://bsky.app/profile/{handle}", "Bluesky", color=MUTED)
            if handle else "",
            _link(f"https://{settings.service_host}/followers/{row.get('did', '')}",
                  "sonde", color=MUTED) if row.get("did") else "",
        ) if part
    )
    return f"""
    <tr>
      <td width="{AVATAR + 12}" valign="top" style="padding:10px 0 10px 14px;">{_avatar(row, images)}</td>
      <td valign="top" style="padding:10px 14px 10px 0;word-break:break-word;overflow-wrap:anywhere;">
        <div style="font:600 14px/1.35 {SANS};color:{INK};">{escape(name)}{name_extra}{_check() if row.get("verified_status") == "valid" else ""}</div>
        <div style="font:12px/1.45 {SANS};color:{MUTED};padding-top:2px;">{escape(fact)}</div>
        {_bio(row)}
        <div style="font:11px/1.6 {SANS};color:{FAINT};padding-top:3px;">{links}</div>
      </td>
    </tr>"""


def _heading(text: str) -> str:
    return (f'<div style="font:600 13px/1.3 {MONO};letter-spacing:0.06em;'
            f'text-transform:uppercase;color:{INK};padding:0 0 4px;">'
            f"{escape(text)}</div>")


def _card_open(title_html: str, href: str) -> str:
    """A post of yours: what it says, and the way to it.

    `title_html` is already escaped — or our own markup for a post with no
    preview — because the caller knows which of the two it has.
    """
    view = (f'<div style="padding-top:4px;font:11px/1.4 {SANS};">'
            f'{_link(href, "view on Bluesky")}</div>') if href else ""
    return (f'<tr><td><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="background:{CARD};border:1px solid '
            f'{RULE};border-radius:8px;margin:0 0 14px;">'
            f'<tr><td colspan="2" style="padding:14px 14px 0;">'
            f'<div style="font:15px/1.4 {SANS};color:{INK};word-break:break-word;'
            f'overflow-wrap:anywhere;">{title_html}</div>'
            f"{view}</td></tr>")


def _note(text: str) -> str:
    return (f'<div style="font:11px/1.6 {SANS};color:{FAINT};'
            f'padding:10px 14px 0;">{escape(text)}</div>')


def _page(title: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:{PAGE};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{PAGE};padding:18px 12px;">
  <tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
         style="max-width:600px;background:{PAGE};">"""


def _close(href: str) -> str:
    """Footer link, then shut the two wrapper tables and the page."""
    return (f'<tr><td align="center" style="padding:6px 0 18px;">'
            f'<div style="font:11px/1.6 {SANS};color:{FAINT};">'
            f'{_link(href, href.replace("https://", ""), color=FAINT)}</div>'
            f"</td></tr></table></td></tr></table></body></html>")


def render_arrivals(
    rows: Sequence[dict], images: dict[str, bytes], *, limit: int | None = None
) -> str:
    """A notable account followed you: one row each, the arrivals shape."""
    from sonde.notify import alerts as arrival_alerts

    limit = settings.verified_alert_max_listed if limit is None else limit
    subject_line = (
        "A notable account just followed you."
        if len(rows) == 1
        else f"{len(rows)} notable accounts just followed you."
    )
    out = [_page(subject_line), f'<tr><td style="padding:0 2px 10px;">']
    out.append(f'<div style="font:600 18px/1.3 {SANS};color:{INK};">'
               f"{escape(subject_line)}</div></td></tr>")

    out.append(f'<tr><td><table role="presentation" width="100%" cellpadding="0" '
               f'cellspacing="0" border="0" style="background:{CARD};border:1px solid '
               f'{RULE};border-radius:8px;margin:0 0 14px;">')
    for row in rows[:limit]:
        why = arrival_alerts.why_notable(row)
        fact = " · ".join(why) or "notable"
        if row.get("event") == "returned":
            fact += " · returned, they had followed before"
        out.append(_person(row, images, fact=fact))
    if len(rows) > limit:
        out.append('<tr><td colspan="2" style="padding:10px 14px 14px;'
                   f'font:12px/1.5 {SANS};color:{MUTED};">'
                   f"… and {len(rows) - limit} more.</td></tr>")
    out.append(_note(
        "Notable means verified, a trusted verifier, or more than "
        f"{settings.large_account_followers:,} followers. Verification is "
        "Bluesky's, not a judgement about the person."
    ))
    out.append("</table></td></tr>")
    out.append(_close(arrival_alerts._footer_link(rows)))
    return "\n".join(out)


def render_interactions(
    rows: Sequence[dict], previews: dict[str, str | None], images: dict[str, bytes]
) -> str:
    """Notable likes and reposts, one card per post of yours."""
    from sonde.notify import interaction_alerts
    from sonde.notify.alerts import why_notable

    headline = (interaction_alerts.subject(rows)
                .removeprefix("sonde: ").capitalize() + " on your posts.")
    out = [_page(headline), '<tr><td style="padding:0 2px 10px;">']
    out.append(f'<div style="font:600 18px/1.3 {SANS};color:{INK};">'
               f"{escape(headline)}</div></td></tr>")

    by_post: dict[str, list[dict]] = {}
    for row in rows:
        by_post.setdefault(row.get("subject") or "", []).append(row)

    for uri, events in by_post.items():
        first = (previews.get(uri) or "").strip().splitlines()
        title = (f"“{escape(first[0][:160])}”" if first
                 else f'<span style="color:{MUTED};">A post of yours</span>')
        out.append(_card_open(title, interaction_alerts.post_link(uri)))
        for row in events:
            verb = "liked" if row.get("kind") == "like" else "reposted"
            why = ", ".join(why_notable(row)) or "notable"
            fact = f"{why} · {verb} · {interaction_alerts._when(row['occurred_at'])}"
            out.append(_person(row, images, fact=fact))
        out.append("</table></td></tr>")

    out.append(_note(
        "Notable means verified, a trusted verifier, or more than "
        f"{settings.large_account_followers:,} followers."
    ))
    out.append(_close(f"https://{settings.service_host}/"))
    return "\n".join(out)


def attach(message: EmailMessage, html: str, images: dict[str, bytes]) -> EmailMessage:
    """Add the HTML alternative and the pictures it refers to."""
    message.add_alternative(html, subtype="html")
    related = message.get_payload()[-1]
    if f"cid:{CHECK_CID}" in html:
        related.add_related(check_png(), maintype="image", subtype="png",
                            cid=f"<{CHECK_CID}>")
    for did, jpeg in images.items():
        if f"cid:{cid_for(did)}" not in html:
            continue
        # No filename=: that turns the part into an attachment.
        related.add_related(
            jpeg, maintype="image", subtype="jpeg", cid=f"<{cid_for(did)}>"
        )
    return message
