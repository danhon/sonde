"""Every write must come from one of sonde's own pages.

sonde has no CSRF tokens, and nothing used to check that a write came from
sonde at all. The Authelia cookie was doing that work by accident, and it does
not actually do it. The cookie is scoped to `domain: sgc.rayandhon.com` with
Authelia's default `same_site: lax`, and `SameSite=Lax` stops *cross-site*
requests — but subdomains of one registrable domain are **same-site**. So any
other service on the fleet (calibre-web, which serves user-supplied ebook
content; homepage; docs; scrypted; birdnet-go; the watchdog) could POST here and
the browser would attach the operator's session. An XSS in any of them was write
access to sonde: follow, unfollow, archive a circle, merge two, start a batch.

So the check is on where the request says it came from, and the load-bearing
line is the one that rejects `same-site` — that value means a different host
under `sgc.rayandhon.com`, which is exactly the attack. Most CSRF advice treats
same-site as friendly. Here it is the threat.

Kept apart from `app.py` because it is a trust boundary and a rule about
headers, testable without a route, a database or a template.
"""

from __future__ import annotations

from typing import Mapping

# Everything that does not change state. OPTIONS and TRACE are here for
# completeness; sonde answers neither.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# `Sec-Fetch-Site` values a write may carry.
#
#   same-origin  sonde's own pages — the only case that happens normally
#   none         user-initiated with no initiator document: a typed URL or a
#                bookmark. A POST is realistically never this, but allowing it
#                costs nothing an attacker can reach, because a hostile page
#                always produces `cross-site` or `same-site`.
#
# `same-site` is deliberately absent. That is the sibling-subdomain case this
# module exists for.
ALLOWED_FETCH_SITE = frozenset({"same-origin", "none"})


def refusal(method: str, headers: Mapping[str, str]) -> str | None:
    """Why this request must not be served, or None to serve it.

    Returns a short reason rather than a bool so the refusal can be logged with
    enough detail to tell an attack from a misconfiguration.
    """
    if method.upper() in SAFE_METHODS:
        return None

    # Preferred: the browser states the relationship itself, so there is no
    # host string to parse and get wrong.
    site = headers.get("sec-fetch-site")
    if site:
        if site in ALLOWED_FETCH_SITE:
            return None
        return f"Sec-Fetch-Site: {site}"

    # Fallback for browsers too old to send it. `Origin` is sent on every
    # cross-origin form POST and cannot be suppressed by the page making it.
    origin = headers.get("origin")
    if origin:
        host = headers.get("host", "")
        # Compare host AND port: in dev the two are `localhost:8090`, and
        # behind Traefik both are the bare service hostname.
        if host and origin.split("://", 1)[-1] == host:
            return None
        return f"Origin: {origin}"

    # Neither header. Every current browser sends at least one on a form POST,
    # and a hostile page cannot strip them, so this is a non-browser client —
    # curl, a test, a script on the box. Allowed on purpose: refusing here
    # would block the operator's own tooling while blocking nothing that a
    # cross-origin page is capable of sending.
    return None
