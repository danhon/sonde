"""A fake AppView built to reproduce the API's real, awkward behaviour.

Page sizes deliberately vary and are almost never full: measured against the
live API, `getFollowers` at limit=100 returned a mean of 87.3 actors with only
5 of 115 pages full. A fake that always returns exactly `limit` would let the
`len(page) < limit` pagination bug pass every test.
"""

from __future__ import annotations

import json
from typing import Callable

import httpx


def actor(did: str, handle: str | None = None, **extra) -> dict:
    profile = {
        "did": did,
        "handle": handle or f"{did.split(':')[-1]}.bsky.social",
        "displayName": extra.pop("displayName", None),
        "createdAt": extra.pop("createdAt", "2024-01-01T00:00:00.000Z"),
    }
    profile.update(extra)
    return profile


def verified(did: str, issuer: str = "bsky.app", **extra) -> dict:
    """A verified actor. Note unverified actors omit `verification` entirely."""
    return actor(
        did,
        verification={
            "verifications": [
                {
                    "issuer": "did:plc:z72i7hdynmk6r22z27h6tvur",
                    "issuerHandle": issuer,
                    "uri": f"at://{issuer}/app.bsky.graph.verification/x",
                    "isValid": True,
                    "createdAt": "2025-04-21T10:47:14.844Z",
                }
            ],
            "verifiedStatus": "valid",
            "trustedVerifierStatus": "none",
        },
        **extra,
    )


def follower_pages(pages: list[list[dict]]) -> Callable[[httpx.Request], httpx.Response]:
    """Serve `pages` in order; the cursor is present on all but the last."""

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        index = int(cursor) if cursor else 0
        body: dict = {"followers": pages[index]}
        if index + 1 < len(pages):
            body["cursor"] = str(index + 1)
        return httpx.Response(200, json=body)

    return handler


def routed(routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> httpx.MockTransport:
    """Dispatch by XRPC method name."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method not in routes:
            return httpx.Response(404, json={"error": "NotFound", "method": method})
        return routes[method](request)

    return httpx.MockTransport(handler)


def profiles_response(known: dict[str, dict]) -> Callable[[httpx.Request], httpx.Response]:
    """getProfiles: returns 200 and SILENTLY OMITS anything it can't resolve."""

    def handler(request: httpx.Request) -> httpx.Response:
        requested = request.url.params.get_list("actors")
        found = [known[d] for d in requested if d in known]
        return httpx.Response(200, json={"profiles": found})

    return handler


def json_body(resp: httpx.Response) -> dict:
    return json.loads(resp.content)
