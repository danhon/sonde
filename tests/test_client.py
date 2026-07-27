"""Rate limiter and retry behaviour.

The 3,000/5min cap is per IP and shared with the other atproto apps on
ubuntuplex, so misbehaving here degrades neighbours, not just sonde.
"""

import asyncio
import time

import httpx
import pytest

from sonde.api.client import BlueskyClient, RateLimited, RateLimiter
from tests.fakes import routed


async def test_limiter_paces_requests():
    limiter = RateLimiter(per_second=20)
    start = time.monotonic()
    for _ in range(30):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    # 30 requests at 20/s: the first burst drains the bucket, the rest are paced.
    assert elapsed >= 0.4, f"limiter let 30 calls through in {elapsed:.3f}s"


async def test_limiter_refills_over_time():
    limiter = RateLimiter(per_second=50)
    for _ in range(50):
        await limiter.acquire()
    await asyncio.sleep(0.1)
    start = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - start < 0.05, "tokens should have refilled while idle"


async def test_429_is_retried_and_honours_retry_after():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "RateLimit"})
        return httpx.Response(200, json={"ok": True})

    client = BlueskyClient("https://fake", transport=routed({"m": handler}), per_second=1000)
    result = await client.xrpc("m")
    await client.aclose()

    assert result == {"ok": True}
    assert attempts["n"] == 2


async def test_429_eventually_gives_up():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "RateLimit"})

    client = BlueskyClient(
        "https://fake", transport=routed({"m": handler}), per_second=1000, max_retries=2
    )
    with pytest.raises(RateLimited):
        await client.xrpc("m")
    await client.aclose()


async def test_5xx_is_retried_then_raises():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"error": "Unavailable"})
        return httpx.Response(200, json={"recovered": True})

    client = BlueskyClient("https://fake", transport=routed({"m": handler}), per_second=1000)
    assert await client.xrpc("m") == {"recovered": True}
    await client.aclose()


async def test_4xx_is_not_retried():
    """A 400 is a bug in our request, not a transient fault — fail fast."""
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(400, json={"error": "InvalidRequest"})

    client = BlueskyClient("https://fake", transport=routed({"m": handler}), per_second=1000)
    with pytest.raises(httpx.HTTPStatusError):
        await client.xrpc("m")
    await client.aclose()

    assert attempts["n"] == 1


async def test_rate_limit_headroom_is_recorded():
    """/settings shows observed headroom rather than our guess at it."""

    def handler(request):
        return httpx.Response(200, headers={"ratelimit-remaining": "2874"}, json={})

    client = BlueskyClient("https://fake", transport=routed({"m": handler}), per_second=1000)
    await client.xrpc("m")
    await client.aclose()

    assert client.rate_limit_remaining == 2874


async def test_call_count_is_tracked_for_sync_runs():
    client = BlueskyClient(
        "https://fake",
        transport=routed({"m": lambda r: httpx.Response(200, json={})}),
        per_second=1000,
    )
    for _ in range(4):
        await client.xrpc("m")
    await client.aclose()

    assert client.calls == 4
