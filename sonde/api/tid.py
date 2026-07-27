"""Decode an atproto TID (record key) to the time the record was written.

A follow record's rkey is a TID: 13 base32-sortable characters encoding
microseconds since the epoch in the high 53 bits, and a clock id in the low 10.

This is how follow dates cost nothing. An authenticated sweep already returns
`viewer.followedBy` — the AT-URI of a follower's follow of us — so the date is
arithmetic on a string we have, not another request.

Prefer the TID over the record's own `createdAt`: `createdAt` is written by
whatever client made the follow and can be wrong or backdated, whereas the TID
is stamped by the PDS at write time. Verified against three live records on
2026-07-27, agreeing to within 0.15s.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
_VALUES = {ch: i for i, ch in enumerate(ALPHABET)}

# Syntactically valid is not the same as plausible: "xxxxxxxxxxxxx" is a
# well-formed TID that decodes to the year 5000. atproto did not exist before
# 2022, and a follow cannot have happened tomorrow, so anything outside this
# window is corrupt input rather than a date.
EARLIEST = datetime(2022, 1, 1, tzinfo=timezone.utc)


def decode(tid: str) -> datetime | None:
    """Return the write time, or None if this is not a well-formed TID."""
    if not tid or len(tid) != 13:
        return None
    total = 0
    for char in tid:
        value = _VALUES.get(char)
        if value is None:
            return None
        total = total * 32 + value
    micros = total >> 10
    try:
        when = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if when < EARLIEST or when > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return when


def from_at_uri(uri: str | None) -> datetime | None:
    """Decode the rkey out of an at:// URI, e.g. viewer.followedBy."""
    if not uri or "/" not in uri:
        return None
    return decode(uri.rsplit("/", 1)[-1])
