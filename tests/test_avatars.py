"""Avatars for the alert emails: fetched at send time, shrunk, capped.

Nothing here may reach the network — every fetch goes through a MockTransport —
and every failure mode ends in "no picture", because an email that doesn't go
is worse than an email with an initial in it.
"""

import asyncio
import io

import httpx
import pytest
from PIL import Image

from sonde.notify import avatars as av


def png(size=(200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 40)).save(buf, "PNG")
    return buf.getvalue()


def transport_for(body: bytes, *, delay: float = 0.0, status: int = 200):
    async def handler(request):
        await asyncio.sleep(delay)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def test_shrink_returns_a_64px_jpeg():
    out = av.shrink(png())
    assert out is not None and out[:2] == b"\xff\xd8"
    assert Image.open(io.BytesIO(out)).size == (64, 64)


def test_shrink_rejects_what_is_not_an_image():
    assert av.shrink(b"not an image") is None


async def test_rows_without_an_avatar_are_skipped_without_a_request():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, content=png())

    rows = [{"did": "did:plc:a", "avatar_url": None}, {"did": "did:plc:b"}]
    assert await av.avatars(rows, transport=httpx.MockTransport(handler)) == {}
    assert requests == []


async def test_http_urls_are_refused():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, content=png())

    rows = [{"did": "did:plc:a", "avatar_url": "http://example.com/a.jpg"}]
    assert await av.avatars(rows, transport=httpx.MockTransport(handler)) == {}
    assert requests == []


async def test_an_oversized_body_is_dropped():
    rows = [{"did": "did:plc:a", "avatar_url": "https://cdn.example/a.jpg"}]
    big = b"x" * (av.MAX_BYTES + 1)
    assert await av.avatars(rows, transport=transport_for(big)) == {}


async def test_a_slow_fetch_does_not_hold_the_send(monkeypatch):
    """The deadline is the whole budget, and it is short on purpose."""
    monkeypatch.setattr(av, "TOTAL_SECONDS", 0.2)
    rows = [{"did": "did:plc:a", "avatar_url": "https://cdn.example/a.jpg"}]
    got = await av.avatars(rows, transport=transport_for(png(), delay=1.0))
    assert got == {}


async def test_the_cap_is_twelve_and_order_is_kept():
    rows = [
        {"did": f"did:plc:{i:02d}", "avatar_url": f"https://cdn.example/{i}.jpg"}
        for i in range(14)
    ]
    got = await av.avatars(rows, transport=transport_for(png()))
    assert list(got) == [r["did"] for r in rows[:12]]


async def test_a_failed_fetch_does_not_cost_the_others():
    async def handler(request):
        if request.url.path.endswith("/bad.jpg"):
            return httpx.Response(500)
        return httpx.Response(200, content=png())

    rows = [
        {"did": "did:plc:bad", "avatar_url": "https://cdn.example/bad.jpg"},
        {"did": "did:plc:good", "avatar_url": "https://cdn.example/good.jpg"},
    ]
    got = await av.avatars(rows, transport=httpx.MockTransport(handler))
    assert list(got) == ["did:plc:good"]
