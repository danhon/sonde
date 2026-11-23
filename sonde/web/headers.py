"""The response headers sonde sets itself, and the static files they allow.

Traefik sends `X-Content-Type-Options` and `X-Frame-Options` on every response
and overwrites any value an app sets for them (NET-7). The
`Content-Security-Policy` and `Referrer-Policy` are sonde's own job.

The policy is only worth having because nothing on a page comes from anywhere
else. Until sonde#6 every page loaded Tailwind's Play CDN, a script from a third
party running in the operator's session, where it could drive the follow button
(BUG-20). It also needed `'unsafe-eval'` and inline styles, so no useful policy
could be written (BUG-21). Now the stylesheet is compiled into
`static/app.css` (`make css`) and the one script is `static/sonde.js`.

What each directive is for:

- `script-src 'self'`: the line that matters. No inline script, no `eval`,
  no other origin. A template that grows an inline `<script>` or an `on…=`
  handler breaks visibly, and `tests/test_headers.py` refuses both.
- `style-src-attr 'unsafe-inline'`: the bar widths and chart font sizes are
  `style="…"` attributes computed per row. An attribute can't run code, so
  allowing them costs little; `<style>` blocks stay refused.
- `img-src https://cdn.bsky.app`: every avatar in production comes from there
  (measured 2026-11-22: 12,976 of 12,976).
- `frame-ancestors 'self'`: the CSP form of Traefik's `X-Frame-Options:
  SAMEORIGIN`, which NET-7 asks every app to send as well.
- `form-action 'self'`: an injected `<form>` can't post anywhere else. A form
  submitted after the Authelia session has expired is redirected to the login
  host and the browser blocks that redirect; reloading the page signs in again.
- `connect-src 'self'`: sonde.js polls `/api/status` and nothing else.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' https://cdn.bsky.app",
    "connect-src 'self'",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'self'",
])

# Links out to Bluesky profiles would otherwise tell bsky.app which sonde
# page, and so whose DID, they were followed from.
REFERRER_POLICY = "same-origin"

HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": REFERRER_POLICY,
}


@cache
def static_url(name: str) -> str:
    """`/static/<name>?v=<hash of its contents>`.

    The hash changes when the file does, so a deploy never leaves a browser
    holding last week's stylesheet against this week's markup. It is computed
    once per process, which matches the files' lifetime: they're baked into
    the image.
    """
    digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
    return f"/static/{name}?v={digest}"
