"""Who may call the machine API, and what they are allowed to do.

sonde's browser surface is guarded by Authelia at the Traefik router. A machine
client cannot do an interactive 2FA login, so the API gets its own router with
no Authelia on it — and that router is only safe because *this* module refuses
everything that arrives without a token sonde issued.

Three properties are load-bearing, and each one is a test:

**Default deny across the whole prefix.** The check is on the path prefix, not
on a list of routes, so a route added under `/api/v1` next month is guarded
before it is written. It also means an unauthenticated caller cannot map the
API by watching 404s change to 401s: the token check runs in front of the
router, so every path under the prefix answers 401 identically.

**The secret never enters `Settings`.** Every other setting is a field on the
frozen `Settings` dataclass, and this one deliberately is not. `Settings` is
passed into every template render, so a field on it is one `{{ settings }}` away
from being displayed; `Authenticator.status()` goes to the same trouble to keep
the app password out of `/settings`. Reading the environment here keeps the
token out of the object that gets rendered, and makes the parse testable with
`monkeypatch.setenv` rather than surgery on a frozen instance.

**Write is granted, never assumed.** A token is read-only unless its entry says
`write`, so the credential a CRM uses to *read* cannot tag anyone if it leaks.

Format, comma-separated, one entry per client:

    SONDE_API_TOKENS=crm:<secret>:write,dashboard:<secret>

`name` is for the log — it is how a refusal is attributed to a client and how
one client is revoked without rotating the others. The secret must contain
neither `:` nor `,`; generate it with `python -c "import secrets;
print(secrets.token_urlsafe(32))"`.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from typing import Mapping

log = logging.getLogger("sonde.web.apikey")

# Everything under here needs a token. `/api/status` is deliberately NOT under
# it: that route serves the nav strip's poller, is guarded by Authelia like
# every other page, and would be exposed to the internet if the API router's
# rule were widened to `/api`. The version segment is part of the prefix for the
# same reason it is part of the URL — a v2 gets its own router and its own
# rollout, rather than silently changing what an existing token reaches.
PREFIX = "/api/v1"

# A short secret is worse than no API at all: it advertises a door and then
# leaves a guessable key in it. 24 characters is below `token_urlsafe(32)` (43
# characters) with room to spare, so a correctly generated token never trips it.
MIN_SECRET = 24

READ, WRITE = "read", "write"
SCOPES = frozenset({READ, WRITE})

# Reads are the only thing a token without `write` may do. HEAD and OPTIONS are
# here for completeness, matching `origin.SAFE_METHODS`.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Client:
    """An authenticated caller. Carries no secret, on purpose.

    This is what gets attached to the request and written to the log, so it must
    stay safe to print in full.
    """

    name: str
    scopes: frozenset[str]

    def may(self, scope: str) -> bool:
        return scope in self.scopes


def _parse(raw: str) -> list[tuple[str, Client]]:
    """`(secret, client)` pairs from the environment string.

    A malformed entry is dropped and logged rather than raising: one fat-
    fingered token in the compose env must not take the whole container down at
    import time, and the log names the entry so the operator can see which one
    went. The secret is never logged, even when it is the thing that is wrong.
    """
    out: list[tuple[str, Client]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, rest = entry.partition(":")
        secret, _, scope_text = rest.partition(":")
        name, secret = name.strip(), secret.strip()
        if not name or not secret:
            log.error("ignoring an API token entry that is not name:secret")
            continue
        if len(secret) < MIN_SECRET:
            log.error("ignoring API token %r: the secret is shorter than %d "
                      "characters", name, MIN_SECRET)
            continue
        scopes = {READ}
        for scope in scope_text.split(":"):
            scope = scope.strip().lower()
            if not scope:
                continue
            if scope not in SCOPES:
                log.error("ignoring unknown scope %r on API token %r",
                          scope, name)
                continue
            scopes.add(scope)
        out.append((secret, Client(name=name, scopes=frozenset(scopes))))
    return out


def clients() -> list[tuple[str, Client]]:
    """Configured clients, read from the environment on each call.

    Not cached. The parse is a handful of string splits against a value that is
    at most a few hundred bytes, and a cache keyed on the raw string would be
    the kind of cleverness that makes a token rotation not take effect.
    """
    return _parse(os.environ.get("SONDE_API_TOKENS", ""))


def configured() -> bool:
    """Whether the API is switched on at all.

    No tokens is the default and it means *off*: an operator who has not issued
    a credential has not asked for a machine surface, and the API answering 401
    to everyone would look like a broken deployment rather than an absent one.
    """
    return bool(clients())


def guards(path: str) -> bool:
    """Whether this path is the API's to defend.

    Exactly the prefix or something below it — `/api/v1abc` is neither, and
    matching it would be a bug in the direction of guarding too much rather
    than too little, which is why it is written out rather than left to
    `startswith`.
    """
    return path == PREFIX or path.startswith(PREFIX + "/")


def needed_scope(method: str) -> str:
    return READ if method.upper() in SAFE_METHODS else WRITE


def bearer(headers: Mapping[str, str]) -> str | None:
    """The token from an `Authorization: Bearer <token>` header."""
    value = headers.get("authorization") or ""
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def identify(headers: Mapping[str, str]) -> Client | None:
    """Which client this is, or None.

    Every configured secret is compared even after one matches. Returning early
    would make the response time a function of the token's position in the list,
    which is a small leak but a free one to close, and `compare_digest` is doing
    the same job one level down.
    """
    presented = bearer(headers)
    if presented is None:
        return None
    found: Client | None = None
    for secret, client in clients():
        if hmac.compare_digest(secret, presented):
            found = client
    return found
