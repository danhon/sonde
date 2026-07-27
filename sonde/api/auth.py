"""Bluesky session auth.

Everything sonde does works unauthenticated. Auth is an *enhancement*, never a
dependency: if a session cannot be established or refreshed, every tier falls
back to its public path rather than the sweep failing. A wrong password should
cost follow dates, not the whole app.

What it buys:
  * `viewer.followedBy` on every follower in the sweep we already pay for —
    the AT-URI of their follow of us, whose rkey is a TID that decodes to an
    exact follow date. No extra calls.
  * `getKnownFollowers`, for an exact cross-check on the sampled affinity index.

Sessions live in memory. A restart just re-authenticates.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("sonde.auth")

# Bluesky access tokens last ~2h; refresh with margin rather than on failure.
REFRESH_MARGIN_SECONDS = 15 * 60
SESSION_LIFETIME_SECONDS = 110 * 60


@dataclass
class Session:
    did: str
    handle: str
    access_jwt: str
    refresh_jwt: str
    obtained_at: float = field(default_factory=time.monotonic)

    @property
    def stale(self) -> bool:
        age = time.monotonic() - self.obtained_at
        return age > (SESSION_LIFETIME_SECONDS - REFRESH_MARGIN_SECONDS)


class Authenticator:
    """Holds at most one session. Failures are logged, never raised."""

    def __init__(
        self,
        identifier: str | None = None,
        password: str | None = None,
        *,
        pds: str = "https://bsky.social",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.identifier = identifier
        self.password = password
        self.pds = pds.rstrip("/")
        self.session: Session | None = None
        self.last_error: str | None = None
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.identifier and self.password)

    async def token(self) -> str | None:
        """Current access token, refreshing or logging in as needed.

        Returns None when auth is unconfigured or failing — callers treat that
        as "use the public path".
        """
        if not self.configured:
            return None
        if self.session is None:
            await self._login()
        elif self.session.stale:
            await self._refresh()
        return self.session.access_jwt if self.session else None

    async def _post(self, method: str, *, json: dict | None = None,
                    token: str | None = None) -> dict | None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(
                timeout=30, transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self.pds}/xrpc/{method}", json=json or {}, headers=headers
                )
            if resp.status_code >= 400:
                self.last_error = f"{method} returned {resp.status_code}"
                return None
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — auth must never take the app down
            self.last_error = f"{method}: {exc}"
            return None

    async def _login(self) -> None:
        data = await self._post(
            "com.atproto.server.createSession",
            json={"identifier": self.identifier, "password": self.password},
        )
        if not data or not data.get("accessJwt"):
            log.warning("Bluesky auth failed (%s); continuing unauthenticated",
                        self.last_error)
            self.session = None
            return
        self.session = Session(
            did=data.get("did", ""), handle=data.get("handle", ""),
            access_jwt=data["accessJwt"], refresh_jwt=data.get("refreshJwt", ""),
        )
        self.last_error = None
        log.info("authenticated to Bluesky as %s", self.session.handle)

    async def _refresh(self) -> None:
        assert self.session is not None
        data = await self._post(
            "com.atproto.server.refreshSession", token=self.session.refresh_jwt
        )
        if not data or not data.get("accessJwt"):
            # A refresh token can be revoked; fall back to a full login rather
            # than silently losing authentication until the next restart.
            log.info("session refresh failed (%s); logging in again", self.last_error)
            self.session = None
            await self._login()
            return
        self.session = Session(
            did=data.get("did", self.session.did),
            handle=data.get("handle", self.session.handle),
            access_jwt=data["accessJwt"],
            refresh_jwt=data.get("refreshJwt", self.session.refresh_jwt),
        )

    def status(self) -> dict:
        """For /settings. Never includes the token or the password."""
        return {
            "configured": self.configured,
            "authenticated": self.session is not None,
            "handle": self.session.handle if self.session else None,
            "error": self.last_error,
        }


def from_env() -> Authenticator:
    return Authenticator(
        identifier=os.environ.get("BLUESKY_ACTOR") or None,
        password=os.environ.get("BLUESKY_APP_PASSWORD") or None,
    )


authenticator = from_env()
