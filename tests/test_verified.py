"""Verified followers, grouped by issuer.

The distribution measured on 2026-07-26 is the point of this view: 147 verified,
but only 14 (10%) verified by an institution. The rest were verified by Bluesky
itself, which tells us nothing about where anyone works.
"""

import pytest
from fastapi.testclient import TestClient

from sonde.db import store
from sonde.web.app import create_app
from tests.fakes import actor, verified


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "verified.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


async def add_follower(profile: dict, rank: int = 0) -> None:
    await store.upsert_actor(profile)
    await store.mark_seen(profile["did"], rank)


async def test_groups_by_issuer_and_ranks_institutions_first():
    await add_follower(verified("did:plc:a", issuer="bsky.app"), 0)
    await add_follower(verified("did:plc:b", issuer="wired.com"), 1)
    await add_follower(verified("did:plc:c", issuer="wired.com"), 2)
    await add_follower(verified("did:plc:d", issuer="nytimes.com"), 3)

    summary = await store.verified_summary()

    assert summary["total"] == 4
    assert summary["institutional"] == 3
    issuers = [g["issuer"] for g in summary["groups"]]
    assert issuers[0] == "wired.com", "largest institution first"
    assert issuers[-1] == "bsky.app", "Bluesky's own verifications sort last"


async def test_unverified_followers_are_excluded():
    await add_follower(actor("did:plc:plain"), 0)
    await add_follower(verified("did:plc:v"), 1)

    summary = await store.verified_summary()
    assert summary["total"] == 1


async def test_departed_verified_followers_are_excluded():
    await add_follower(verified("did:plc:gone"), 0)
    await store.mark_departed(["did:plc:gone"], reason="unfollow")

    summary = await store.verified_summary()
    assert summary["total"] == 0
    assert summary["groups"] == []


async def test_invalid_verification_is_counted_apart():
    """A record that fails validation is not the same as no record at all."""
    payload = actor("did:plc:bad")
    payload["verification"] = {
        "verifications": [],
        "verifiedStatus": "invalid",
        "trustedVerifierStatus": "none",
    }
    await add_follower(payload, 0)

    summary = await store.verified_summary()
    assert summary["invalid"] == 1
    assert summary["total"] == 0, "invalid must not inflate the verified count"


async def test_trusted_verifiers_are_counted():
    payload = verified("did:plc:tv")
    payload["verification"]["trustedVerifierStatus"] = "valid"
    await add_follower(payload, 0)

    summary = await store.verified_summary()
    assert summary["trusted_verifiers"] == 1


async def test_multiple_issuers_place_a_follower_in_each_group():
    payload = verified("did:plc:multi", issuer="wired.com")
    payload["verification"]["verifications"].append(
        {
            "issuer": "did:plc:nyt",
            "issuerHandle": "nytimes.com",
            "uri": "at://x",
            "isValid": True,
            "createdAt": "2025-05-22T00:00:00.000Z",
        }
    )
    await add_follower(payload, 0)

    summary = await store.verified_summary()
    assert {g["issuer"] for g in summary["groups"]} == {"wired.com", "nytimes.com"}
    assert summary["total"] == 1, "the person is still one follower"


async def test_route_renders_grouped_output():
    await add_follower(verified("did:plc:x", issuer="washingtonpost.com"), 0)
    with TestClient(create_app()) as client:
        r = client.get("/verified")

    assert r.status_code == 200
    assert "washingtonpost.com" in r.text
    assert "x.bsky.social" in r.text


async def test_route_renders_empty_state():
    with TestClient(create_app()) as client:
        r = client.get("/verified")

    assert r.status_code == 200
    assert "No verified followers yet" in r.text
