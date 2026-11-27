"""The pictures in the alert emails.

Inline, not linked. A remote `<img src="https://cdn.bsky.app/…">` would tell
Bluesky's CDN that the email was opened and when, and Gmail hides remote images
for a sender it hasn't seen much of — so the pictures would usually not show
anyway. Bytes embedded in the message have no such problem: nothing loads when
it is opened.

Shrunk, not passed through. A production avatar is 6 KB to 177 KB as served
(measured 2026-11-26), and Gmail clips a message over 102 KB. Re-encoded at 64px
one is under 2 KB, so twelve of them fit with room to spare.

Re-encoded, not embedded as fetched. Anyone can upload an avatar, so the bytes
that arrive here are attacker-adjacent; what we put in the message is what
Pillow produced from them, never the original file, which also drops whatever
metadata it carried.

Every failure — no URL, not https, timeout, a body over the cap, bytes that are
not an image — means "no picture" for that person and the email still goes.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Sequence

import httpx
from PIL import Image, UnidentifiedImageError

log = logging.getLogger("sonde.avatars")

SIZE = 64
QUALITY = 80
MAX_AVATARS = 12
MAX_BYTES = 2_000_000
PER_IMAGE_SECONDS = 5.0
TOTAL_SECONDS = 8.0

# A crafted image could otherwise expand to gigabytes inside Pillow. Well above
# any real avatar, well below anything that would hurt the box.
Image.MAX_IMAGE_PIXELS = 20_000_000


def shrink(raw: bytes, size: int = SIZE) -> bytes | None:
    """Re-encode whatever this is as a small JPEG, or None if it isn't one."""
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            image.thumbnail((size, size), Image.LANCZOS)
            out = io.BytesIO()
            image.save(out, "JPEG", quality=QUALITY, optimize=True)
            return out.getvalue()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        return None


async def _fetch(client: httpx.AsyncClient, url: str) -> bytes | None:
    """The body, if it is https, 200, and under the cap."""
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    return None
            return bytes(body)
    except httpx.HTTPError as exc:
        log.debug("avatar fetch failed for %s: %s", url, exc)
        return None


async def avatars(
    rows: Sequence[dict],
    *,
    limit: int = MAX_AVATARS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, bytes]:
    """JPEG bytes per did, in row order, for at most `limit` people."""
    picks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        did, url = row.get("did"), row.get("avatar_url") or ""
        if not did or did in seen or not url.startswith("https://"):
            continue
        seen.add(did)
        picks.append((did, url))
        if len(picks) == limit:
            break
    if not picks:
        return {}

    out: dict[str, bytes] = {}
    async with httpx.AsyncClient(
        timeout=PER_IMAGE_SECONDS, transport=transport, follow_redirects=True
    ) as client:
        tasks = [asyncio.ensure_future(_fetch(client, url)) for _, url in picks]
        done, pending = await asyncio.wait(tasks, timeout=TOTAL_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    for (did, _), task in zip(picks, tasks):
        if task not in done or task.cancelled():
            continue
        exc = task.exception()
        if exc is not None or not task.result():
            continue
        # Decoding is CPU-bound; a thread keeps the mail job responsive.
        jpeg = await asyncio.to_thread(shrink, task.result())
        if jpeg:
            out[did] = jpeg
    return out
