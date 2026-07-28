"""Signals from the links people put in their own bios.

No network calls. The URL is already stored; what it *points at* is often more
informative than what the page says, and it is the most consented signal
available — published by its owner, on their own public profile.

Measured across the 564-account enrichment set on 2026-07-27:

  121 (21%) have a URL in their bio
    0        are LinkedIn
   16        are linktr.ee — an aggregator, no signal on its own
    3        are buttondown.com — a newsletter platform, which IS a signal

That distribution is why fetching those pages was dropped: most are
aggregators, shorteners or storefronts rather than personal sites with
structured data. The host itself carries the signal, for free.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.I)

# Host -> what it tells us. Deliberately narrow: a platform in someone's bio is
# evidence they publish there, which is not the same as evidence about them.
PLATFORMS: dict[str, tuple[str, str]] = {
    "substack.com": ("newsletter", "publishes a newsletter on Substack"),
    "buttondown.com": ("newsletter", "publishes a newsletter on Buttondown"),
    "buttondown.email": ("newsletter", "publishes a newsletter on Buttondown"),
    "beehiiv.com": ("newsletter", "publishes a newsletter on beehiiv"),
    "ghost.io": ("newsletter", "publishes on Ghost"),
    "github.com": ("code", "has a GitHub presence"),
    "github.io": ("code", "publishes on GitHub Pages"),
    "gitlab.com": ("code", "has a GitLab presence"),
    "codeberg.org": ("code", "has a Codeberg presence"),
    "patreon.com": ("supported", "takes support on Patreon"),
    "ko-fi.com": ("supported", "takes support on Ko-fi"),
    "itch.io": ("games", "publishes games on itch.io"),
    "gamejolt.com": ("games", "publishes games on Game Jolt"),
    "steampowered.com": ("games", "has a Steam store page"),
    "bandcamp.com": ("music", "publishes music on Bandcamp"),
    "twitch.tv": ("streaming", "streams on Twitch"),
    "youtube.com": ("video", "publishes video on YouTube"),
    "medium.com": ("writing", "writes on Medium"),
    "wordpress.com": ("writing", "writes on WordPress"),
    "linkedin.com": ("professional", "links a LinkedIn profile"),
}

# Aggregators and shorteners hide the destination, so they say nothing.
OPAQUE = {"linktr.ee", "bit.ly", "t.co", "tinyurl.com", "linkin.bio",
          "beacons.ai", "carrd.co", "bsky.app", "lnk.bio", "solo.to"}

# Suffixes that classify without a lookup.
SUFFIXES: list[tuple[str, str, str]] = [
    (".edu", "academic", "institutional email domain (.edu)"),
    (".ac.uk", "academic", "institutional domain (.ac.uk)"),
    (".gov", "government", "government domain (.gov)"),
    (".gov.uk", "government", "government domain (.gov.uk)"),
    (".mil", "government", "military domain (.mil)"),
    (".org", "organisation", "organisation domain (.org)"),
]


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    seen, out = set(), []
    for raw in URL_RE.findall(text):
        url = raw.rstrip(".,;:)")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def classify(url: str) -> dict | None:
    """What a single bio link tells us, if anything."""
    host = host_of(url)
    if not host or host in OPAQUE:
        return None

    for platform, (kind, note) in PLATFORMS.items():
        if host == platform or host.endswith("." + platform):
            return {"url": url, "host": host, "kind": kind, "note": note}

    for suffix, kind, note in SUFFIXES:
        # `www.gov.uk` strips to `gov.uk`, which does not *end with* `.gov.uk`,
        # so the bare form has to match too.
        if host.endswith(suffix) or host == suffix.lstrip("."):
            return {"url": url, "host": host, "kind": kind, "note": note}

    # Anything else is a personal or organisational site. Weak on its own, but
    # a self-hosted domain is a mild signal of professional presence.
    return {"url": url, "host": host, "kind": "site",
            "note": f"self-declared site ({host})"}


def signals_for(description: str | None, handle: str | None = None) -> list[dict]:
    """All link-derived signals for one actor, deduplicated by host."""
    found: dict[str, dict] = {}
    for url in extract_urls(description):
        signal = classify(url)
        if signal and signal["host"] not in found:
            found[signal["host"]] = signal

    # The handle is a self-declared domain too: `tomhannen.ft.com` says more
    # than any bio link, and `someone.bsky.social` says nothing.
    if handle and not handle.endswith(".bsky.social") and "." in handle:
        for suffix, kind, note in SUFFIXES:
            if handle.lower().endswith(suffix) or handle.lower() == suffix.lstrip("."):
                found.setdefault(handle.lower(), {
                    "url": f"https://{handle}", "host": handle.lower(),
                    "kind": kind, "note": f"handle on an {note}",
                })
    return list(found.values())
