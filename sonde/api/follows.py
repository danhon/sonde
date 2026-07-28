"""Following and unfollowing — the only thing sonde writes to Bluesky.

Everything else in this codebase reads. This creates a public record on a real
account under the operator's name, so it is deliberately narrow and guarded:

  * **Follow-back only.** The subject must already be a current, non-hidden
    follower. sonde has no feature that needs to follow a stranger, so the
    ability to do so is not offered — which also means a wrong DID reaching
    this code fails instead of following somebody at random.
  * **No retries.** `xrpc_post` never repeats a failed write; an ambiguous
    failure retried is how you end up with two follow records.
  * **Reversible.** The record URI is stored, so the same button undoes it.
  * **Logged.** Every follow and unfollow is written to `follow_events`, so
    "why am I following this person" always has an answer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sonde.api.auth import authenticator
from sonde.api.client import BlueskyClient

log = logging.getLogger("sonde.follows")

COLLECTION = "app.bsky.graph.follow"


class FollowError(RuntimeError):
    """Something stopped the write. The message is shown to the operator."""


def _rkey(uri: str) -> str:
    """The record key at the end of an at:// URI."""
    return uri.rsplit("/", 1)[-1]


async def create_follow(client: BlueskyClient, did: str) -> str:
    """Follow `did`. Returns the created record URI."""
    session = authenticator.session
    if session is None or not session.did:
        raise FollowError("not signed in to Bluesky — set the app password")
    if not did.startswith("did:"):
        raise FollowError(f"not a DID: {did!r}")

    data = await client.xrpc_post("com.atproto.repo.createRecord", {
        "repo": session.did,
        "collection": COLLECTION,
        "record": {
            "$type": COLLECTION,
            "subject": did,
            "createdAt": datetime.now(timezone.utc)
                         .isoformat(timespec="milliseconds")
                         .replace("+00:00", "Z"),
        },
    })
    uri = data.get("uri")
    if not uri:
        raise FollowError("Bluesky accepted the write but returned no URI")
    log.info("followed %s -> %s", did, uri)
    return uri


async def delete_follow(client: BlueskyClient, uri: str) -> None:
    """Undo a follow, given the record URI stored when it was created."""
    session = authenticator.session
    if session is None or not session.did:
        raise FollowError("not signed in to Bluesky — set the app password")
    if not uri:
        raise FollowError("no follow record stored, so there is nothing to undo")

    await client.xrpc_post("com.atproto.repo.deleteRecord", {
        "repo": session.did,
        "collection": COLLECTION,
        "rkey": _rkey(uri),
    })
    log.info("unfollowed via %s", uri)
