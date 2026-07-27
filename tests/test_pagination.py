"""Pagination — the highest-value tests in the suite.

Measured against the live API: a full sweep of @danhon.com is 115 pages at
limit=100, mean yield 87.3, and only 5 pages come back full. The first page
returned 90 of 100. A `while len(page) == limit` loop therefore terminates on
page 1 and records 90 followers as the complete list, silently.
"""

import httpx
import pytest

from sonde.api.client import BlueskyClient
from sonde.api.graph import get_profiles, iter_followers
from tests.fakes import actor, follower_pages, profiles_response, routed


def _client(transport: httpx.MockTransport) -> BlueskyClient:
    return BlueskyClient("https://fake", transport=transport, per_second=1000)


async def collect(client: BlueskyClient, **kw) -> list[dict]:
    out: list[dict] = []
    async for _page, actors in iter_followers(client, "danhon.com", **kw):
        out.extend(actors)
    return out


async def test_short_first_page_does_not_end_the_sweep():
    """The exact shape of the real bug: page 1 returns 90 of a requested 100."""
    pages = [
        [actor(f"did:plc:p1-{i}") for i in range(90)],   # short, but NOT last
        [actor(f"did:plc:p2-{i}") for i in range(72)],
        [actor(f"did:plc:p3-{i}") for i in range(83)],
    ]
    client = _client(routed({"app.bsky.graph.getFollowers": follower_pages(pages)}))
    got = await collect(client)
    await client.aclose()

    assert len(got) == 245, "a short page must not be treated as the last page"
    assert client.calls == 3


async def test_only_an_absent_cursor_ends_the_sweep():
    """Even a completely empty page keeps going while a cursor is present."""
    pages = [
        [actor("did:plc:a")],
        [],                                  # empty, mid-list — still not the end
        [actor("did:plc:b"), actor("did:plc:c")],
    ]
    client = _client(routed({"app.bsky.graph.getFollowers": follower_pages(pages)}))
    got = await collect(client)
    await client.aclose()

    assert [a["did"] for a in got] == ["did:plc:a", "did:plc:b", "did:plc:c"]


async def test_full_page_without_cursor_ends_the_sweep():
    """A full page that carries no cursor is the end — don't request page 2."""
    pages = [[actor(f"did:plc:x{i}") for i in range(100)]]
    client = _client(routed({"app.bsky.graph.getFollowers": follower_pages(pages)}))
    got = await collect(client)
    await client.aclose()

    assert len(got) == 100
    assert client.calls == 1


async def test_page_limit_caps_the_head_sweep():
    pages = [[actor(f"did:plc:p{p}-{i}") for i in range(80)] for p in range(10)]
    client = _client(routed({"app.bsky.graph.getFollowers": follower_pages(pages)}))
    got = await collect(client, limit=2)
    await client.aclose()

    assert len(got) == 160
    assert client.calls == 2, "the head sweep must not walk the whole list"


# ---------------------------------------------------------------- hydration

async def test_get_profiles_maps_by_did_not_position():
    """getProfiles omits unresolvable actors silently — HTTP 200, short array.

    Index-zipping would assign every profile after the first gap to the wrong
    person: wrong follower counts, wrong scores, no error anywhere.
    """
    requested = [f"did:plc:a{i}" for i in range(6)]
    resolvable = {
        d: {**actor(d), "followersCount": (i + 1) * 1000}
        for i, d in enumerate(requested)
        if i not in (1, 4)  # these two are deactivated / suspended
    }
    client = _client(routed({"app.bsky.actor.getProfiles": profiles_response(resolvable)}))
    by_did, missing = await get_profiles(client, requested)
    await client.aclose()

    assert missing == ["did:plc:a1", "did:plc:a4"]
    assert len(by_did) == 4
    # Every survivor keeps its OWN count. Positional zipping fails right here.
    for i, did in enumerate(requested):
        if did in by_did:
            assert by_did[did]["followersCount"] == (i + 1) * 1000


async def test_get_profiles_rejects_oversized_batches():
    client = _client(routed({"app.bsky.actor.getProfiles": profiles_response({})}))
    with pytest.raises(ValueError, match="at most 25"):
        await get_profiles(client, [f"did:plc:{i}" for i in range(26)])
    await client.aclose()


async def test_get_profiles_handles_a_wholly_unresolvable_batch():
    client = _client(routed({"app.bsky.actor.getProfiles": profiles_response({})}))
    by_did, missing = await get_profiles(client, ["did:plc:ghost"])
    await client.aclose()

    assert by_did == {}
    assert missing == ["did:plc:ghost"]
