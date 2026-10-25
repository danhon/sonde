"""Session refresh/login must be serialized.

Bluesky refresh tokens are single-use and rotate on every call. Before the
lock in `Authenticator.token()`, two coroutines racing to refresh the same
stale session meant the loser got a 400 from `refreshSession` and fell back
to a full password login — hammering `createSession` with the real app
password every time two authed reads overlapped a refresh window.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from sonde.api.auth import Authenticator, Session


def handler_counting(calls: list[str]):
    # An async handler with a real suspension point, so concurrent callers
    # actually interleave instead of one running to completion before the
    # next is scheduled — a sync handler wouldn't expose the race at all.
    async def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        await asyncio.sleep(0.01)
        if method == "com.atproto.server.createSession":
            return httpx.Response(200, json={
                "did": "did:plc:me", "handle": "danhon.com",
                "accessJwt": f"access-{len(calls)}", "refreshJwt": f"refresh-{len(calls)}",
            })
        if method == "com.atproto.server.refreshSession":
            return httpx.Response(200, json={
                "did": "did:plc:me", "handle": "danhon.com",
                "accessJwt": f"access-{len(calls)}", "refreshJwt": f"refresh-{len(calls)}",
            })
        raise AssertionError(f"unexpected call: {method}")

    return handle


async def test_a_concurrent_callers_share_one_login():
    calls: list[str] = []
    auth = Authenticator(
        "danhon.com", "app-password",
        transport=httpx.MockTransport(handler_counting(calls)),
    )

    tokens = await asyncio.gather(*[auth.token() for _ in range(10)])

    assert calls == ["com.atproto.server.createSession"]
    assert len(set(tokens)) == 1


async def test_b_concurrent_callers_share_one_refresh():
    calls: list[str] = []
    auth = Authenticator(
        "danhon.com", "app-password",
        transport=httpx.MockTransport(handler_counting(calls)),
    )
    auth.session = Session(
        did="did:plc:me", handle="danhon.com",
        access_jwt="stale-access", refresh_jwt="stale-refresh",
        obtained_at=time.monotonic() - 10_000,
    )

    tokens = await asyncio.gather(*[auth.token() for _ in range(10)])

    assert calls == ["com.atproto.server.refreshSession"]
    assert len(set(tokens)) == 1
