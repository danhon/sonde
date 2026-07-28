"""Rate-limited HTTP client for the Bluesky AppView.

The 3,000-requests-per-5-minutes cap is per IP and shared house-wide with
BlueBirdNET and atproto-labeler, so the limiter defaults well under it. The
binding constraint is politeness to a shared address, not the cap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from sonde.api.auth import authenticator
from sonde.config import settings

log = logging.getLogger("sonde.api")

# Authenticated reads must go to the PDS/entryway, not the public AppView —
# public.api.bsky.app ignores bearer tokens and returns no viewer state.
AUTHED_BASE = "https://bsky.social"


class RateLimiter:
    """Token bucket. Capacity equals one second's worth of tokens, min 1."""

    def __init__(self, per_second: float) -> None:
        self.rate = max(per_second, 0.1)
        self.capacity = max(self.rate, 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._refill(now)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


class RateLimited(Exception):
    """Raised when a 429 survives every retry."""


# ONE limiter for the whole process, shared by every client.
#
# Clients used to build their own, so the cap multiplied by the number of jobs
# running: clicking three jobs on the settings page gave 3x the configured rate
# against an IP already shared with BlueBirdNET and atproto-labeler. Measured at
# 18 req/s against a configured 3.
#
# Created lazily because a RateLimiter binds to the running event loop.
_shared: RateLimiter | None = None


def shared_limiter() -> RateLimiter:
    global _shared
    if _shared is None:
        _shared = RateLimiter(settings.rate_limit_per_second)
    return _shared


def reset_shared_limiter() -> None:
    """Tests only: drop the process-wide limiter between event loops."""
    global _shared
    _shared = None


class BlueskyClient:
    """Thin XRPC wrapper. Counts its own calls so sync_runs can record them."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        per_second: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.api_base).rstrip("/")
        # An explicit rate gets a private limiter (tests want to run flat out);
        # everything else shares the process-wide one, so concurrent jobs add up
        # to the configured rate rather than multiplying it.
        self.limiter = (
            RateLimiter(per_second) if per_second is not None else shared_limiter()
        )
        self.max_retries = max_retries if max_retries is not None else settings.max_retries
        self.calls = 0
        self.rate_limit_remaining: int | None = None
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            transport=transport,
            headers={"User-Agent": "sonde/0.1 (+https://github.com/danhon/sonde)"},
            follow_redirects=True,
        )

    async def __aenter__(self) -> "BlueskyClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def plc(self, did: str) -> dict:
        """Resolve a DID document. Verification records live in the verifier's
        own repo, so we need their PDS endpoint before we can list them."""
        await self.limiter.acquire()
        self.calls += 1
        resp = await self._client.get(f"https://plc.directory/{did}")
        resp.raise_for_status()
        return resp.json()

    async def xrpc_post(self, method: str, body: dict[str, Any]) -> dict:
        """POST an XRPC procedure. Always authenticated, never retried.

        No automatic retry, deliberately. Every procedure sonde calls *writes*
        to a real account, and a retry after an ambiguous failure risks writing
        twice — a duplicate follow record is not something a user asked for. A
        failed write is reported and left for a human to repeat.
        """
        token = await authenticator.token()
        if not token:
            raise PermissionError("this needs the Bluesky app password")
        url = f"{AUTHED_BASE.rstrip('/')}/xrpc/{method}"
        await self.limiter.acquire()
        self.calls += 1
        resp = await self._client.post(
            url, json=body, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def xrpc(
        self, method: str, params: dict[str, Any] | None = None,
        *, base_url: str | None = None, authed: bool = False,
    ) -> dict:
        """GET an XRPC method, retrying on 429 and transient 5xx.

        `authed=True` asks for an authenticated read. If no session is
        available the call silently proceeds unauthenticated — auth is an
        enhancement, so a bad credential costs viewer state, not the sweep.
        """
        headers: dict[str, str] = {}
        if authed:
            token = await authenticator.token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
                base_url = base_url or AUTHED_BASE
        url = f"{(base_url or self.base_url).rstrip('/')}/xrpc/{method}"
        attempt = 0
        while True:
            await self.limiter.acquire()
            self.calls += 1
            try:
                resp = await self._client.get(url, params=params, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= self.max_retries:
                    raise
                delay = min(2 ** attempt, 30)
                log.warning("%s transport error (%s); retrying in %ss", method, exc, delay)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            remaining = resp.headers.get("ratelimit-remaining")
            if remaining is not None:
                try:
                    self.rate_limit_remaining = int(remaining)
                except ValueError:
                    pass

            if resp.status_code == 429:
                if attempt >= self.max_retries:
                    raise RateLimited(f"429 from {method} after {attempt} retries")
                delay = _retry_after(resp) or min(2 ** attempt, 60)
                log.warning("%s rate limited; sleeping %ss", method, delay)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if resp.status_code >= 500:
                if attempt >= self.max_retries:
                    resp.raise_for_status()
                delay = min(2 ** attempt, 30)
                log.warning("%s returned %s; retrying in %ss", method, resp.status_code, delay)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            resp.raise_for_status()
            return resp.json()


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None
