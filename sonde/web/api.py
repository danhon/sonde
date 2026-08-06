"""The machine-readable read surface, for sonde's own tooling.

Written for a personal CRM that needs to know who these people are, but the
contract is general: a token-holding program asks sonde what it knows about a
person and gets JSON back.

Three rules run through the whole module.

**Nothing is serialised by reflection.** Every response is assembled from an
explicit dict literal. `SELECT a.*` hands this module the raw actors row —
`ignored_reason`, `follow_uri`, `unservable_since`, whatever is added to the
table next year — and a serialiser that copied a row and deleted the bad keys
would leak each new column on the day it lands. ACCESS.md states the same rule
for the public web view and gives the reason: *the failure mode of a blocklist
is silence.* `tests/test_api.py` asserts the resulting JSON against a declared
key allowlist, so a field cannot appear here without a test being edited.

**One filter path for people.** Every list of persons — the roster, a circle's
members, a search — is `store.ranked_followers`, which is the one place that
knows to exclude departed followers, hidden followers and the operator's own
account. `store.group_members` would have been the obvious way to serve a
circle and it applies none of those three, so a circle listing would have
published exactly the people the rest of the API withholds.

**Hidden people do not exist.** They are absent from every list, their detail
route is a 404, and `/resolve` reports them as unknown. Omitting someone from
a list while leaving their record fetchable is not a redaction, it is an
unindexed leak — ACCESS.md §2 makes the same argument about accounts that opted
out of logged-out visibility.

Authentication is not here. It is a middleware over the whole `/api/v1` prefix
in `apikey.py`, for the reason the same-origin guard is a middleware: a check
that each route performs is a check that the route added next month forgets.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sonde.config import settings
from sonde.web import apikey

log = logging.getLogger("sonde.web.api")

router = APIRouter(prefix=apikey.PREFIX)

API_VERSION = "v1"

# Page sizes. The ceiling is a courtesy to the client as much as to SQLite: a
# person record with its circles is a few hundred bytes, so 200 is a response
# measured in tens of kilobytes rather than one that has to be streamed.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# `/resolve` runs one scan of `actors` per call whatever the batch size, so the
# cap is about response size and about keeping a mistyped loop from asking for
# ten thousand identifiers in one URL.
MAX_RESOLVE = 100

# Sort orders a client may ask for. Deliberately a subset of `store.SORTABLE`:
# `recent` inverts its own direction inside the store, which is right for a
# table with a "most recent first" header and is a trap in an API where the
# client sees only the parameter it sent.
ORDERS = ("did", "influence", "followers", "follows", "since", "handle", "name")

# Events a client is told about. Not a filter for tidiness — `followed_back`
# and `unfollowed_back` carry the follow record's URI in `detail`, and
# `follow_failed` carries an exception string. Both are sonde's own operational
# history rather than anything that happened to the follower, and the fact that
# you follow someone back is already on their record as `is_mutual`.
PUBLIC_EVENTS = ("followed", "returned", "departed", "handle_changed")

# Departure reasons are written by the sweep and are currently only ever
# "unknown", but an allowlist is what stops a reason added later — a moderation
# outcome, say — reaching a client through a column nobody thought about again.
PUBLIC_REASONS = ("unfollow", "gone", "unknown")


class ApiError(Exception):
    """A refusal a client can act on.

    Carries a stable `code` as well as a message, so a client can branch on
    something that will not change when the wording improves.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    """The one error shape. Also used by the auth middleware, which refuses
    before any of this module's routes are reached."""
    response = JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status)
    # Set here as well as on the success path in the middleware, because a
    # refusal can be as revealing as an answer: a 404 from `/people/{handle}`
    # tells a cache whether sonde tracks that person.
    response.headers["Cache-Control"] = "private, no-store"
    if code == "unauthenticated":
        # Correct for a 401 and, more usefully, it tells a client library that
        # it is missing a credential rather than forbidden from having one.
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


# ------------------------------------------------------------------ cursors

def _fingerprint(**parts: Any) -> str:
    """A short digest of everything that decides which rows a query returns.

    Stapled into the cursor so that reusing page 2's cursor against a different
    filter is refused rather than silently answered. Without it the client gets
    rows from one query numbered by another and no error anywhere — the same
    class of fault as index-zipping `getProfiles` results, which CLAUDE.md
    records as costing real debugging time.
    """
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _encode_cursor(fingerprint: str, **state: Any) -> str:
    payload = json.dumps({"f": fingerprint, **state}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, fingerprint: str) -> dict:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ApiError(400, "bad_cursor",
                       "That cursor is not one sonde issued.") from None
    if not isinstance(payload, dict) or payload.get("f") != fingerprint:
        raise ApiError(
            400, "cursor_mismatch",
            "That cursor belongs to a different query. Start again without a "
            "cursor when you change any filter, order or limit.")
    return payload


# ------------------------------------------------------------- parameters

def _int_param(value: str | None, name: str) -> int | None:
    """A query parameter as an int, or a refusal naming it.

    Typed `int | None` on the signature, FastAPI answers a malformed value with
    its own 422 in its own envelope — a different error shape from every other
    error this API produces, arriving exactly when a client is least able to
    parse it. The web routes learned the same lesson from blank form fields;
    see the docstring on `followers` in `app.py`.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_parameter",
                       f"{name} must be a whole number.") from None


def _limit_param(value: str | None) -> int:
    limit = _int_param(value, "limit")
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1:
        raise ApiError(400, "bad_parameter", "limit must be at least 1.")
    return min(limit, MAX_LIMIT)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# ----------------------------------------------------------- serialisers

def _bsky_url(row: dict) -> str:
    return f"https://bsky.app/profile/{row.get('handle') or row['did']}"


def _sonde_url(did: str) -> str:
    return f"https://{settings.service_host}/followers/{did}"


def _organisation(row: dict) -> dict | None:
    """The best-evidence employer match, if the institution matcher found one.

    Flat columns on `actors` rather than a join, so this is free. Note BUG-11:
    the institution tables are empty in production, so this is currently null
    for everyone and `affiliations` on the detail record is where employers
    actually live.
    """
    if not row.get("institution_name"):
        return None
    return {
        "name": row["institution_name"],
        "role": row.get("institution_role"),
        "confidence": row.get("institution_confidence"),
        "method": row.get("institution_method"),
    }


def person_summary(row: dict, circles: list[dict] | None = None,
                   interaction: dict | None = None) -> dict:
    """One person, as they appear in a list.

    `circles` and `interaction` are passed in rather than fetched, because the
    caller has already fetched them for the whole page in one query each. Doing
    it per row is a hundred round trips a page — the N+1 that `tags_for_many`
    exists to prevent, rediscovered.
    """
    return {
        "did": row["did"],
        "handle": row.get("handle"),
        "display_name": row.get("display_name"),
        "description": row.get("description"),
        "avatar_url": row.get("avatar_url"),
        "account_created_at": row.get("account_created_at"),
        "followers_count": row.get("followers_count"),
        "follows_count": row.get("follows_count"),
        "posts_count": row.get("posts_count"),
        "last_post_at": row.get("last_post_at"),
        "verified": row.get("verified_status") == "valid",
        "verified_status": row.get("verified_status"),
        "trusted_verifier": row.get("trusted_verifier_status") == "valid",
        # They asked Bluesky not to show them to logged-out viewers. sonde shows
        # them to the operator, and says so here, because a client that
        # republishes this data needs to know which people said no.
        "is_private": bool(row.get("is_private")),
        "influence_score": row.get("influence_score"),
        "relationship_score": row.get("relationship_score"),
        "following_since": row.get("since"),
        "following_since_exact": bool(row.get("since_exact")),
        "first_seen_at": row.get("first_seen_at"),
        "list_rank": row.get("list_rank"),
        "is_mutual": bool(row.get("is_mutual")),
        "circles": [c["slug"] for c in (circles or [])],
        "organisation": _organisation(row),
        "last_interaction_at": (interaction or {}).get("last_at"),
        "bsky_url": _bsky_url(row),
        "sonde_url": _sonde_url(row["did"]),
    }


def _event(row: dict) -> dict | None:
    if row.get("event") not in PUBLIC_EVENTS:
        return None
    out = {
        "event": row["event"],
        "detected_at": row.get("detected_at"),
        "reason": row.get("reason") if row.get("reason") in PUBLIC_REASONS
        else None,
        "detail": None,
    }
    if row["event"] == "handle_changed":
        # The one event whose detail is about the follower rather than about
        # sonde: "oldhandle → newhandle". A CRM keyed on handles cannot survive
        # a rename without it.
        out["detail"] = row.get("detail")
    return out


def person_detail(row: dict, *, circles: list[dict], affiliations: list[dict],
                  interactions: dict, recent_interactions: list[dict],
                  shared: dict, posts: list[dict]) -> dict:
    """Everything sonde knows about one person, in one response.

    One call rather than six, because the alternative is a client that fires
    six requests per contact and a rate conversation that did not need to
    happen. It is the expensive endpoint and API.md says so.
    """
    out = person_summary(row, circles)
    out["circles"] = [
        {"slug": c["slug"], "name": c.get("name"), "tier": c.get("tier"),
         "confidence": c.get("confidence"), "evidence": c.get("evidence")}
        for c in circles
    ]
    out["score"] = {
        "value": row.get("influence_score"),
        "components": row.get("components") or [],
    }
    out["relationship"] = {
        "value": row.get("relationship_score"),
        "components": row.get("relationship") or {},
    }
    out["affiliations"] = [
        {"organisation": a.get("org_name"), "role": a.get("role"),
         "kind": a.get("kind"), "method": a.get("method"),
         "confidence": a.get("confidence"), "note": a.get("note"),
         "url": a.get("url"), "source_url": a.get("source_url")}
        for a in affiliations
    ]
    out["wikidata"] = {
        "id": row.get("wikidata_id"),
        "occupations": row.get("wikidata_occupations") or [],
        "employers": row.get("wikidata_employers") or [],
        "past_employers": row.get("wikidata_past_employers") or [],
        "positions": row.get("wikidata_positions") or [],
        "wikipedia_title": row.get("wikipedia_title"),
        "wikipedia_views_30d": row.get("wikipedia_views_30d"),
    }
    out["links"] = [
        {"url": s.get("url"), "host": s.get("host"), "kind": s.get("kind"),
         "note": s.get("note")}
        for s in (row.get("link_signals") or [])
    ]
    out["interactions"] = {
        "totals": {
            kind: {"inbound": v.get("inbound", 0),
                   "outbound": v.get("outbound", 0),
                   "last_at": v.get("last_at")}
            for kind, v in interactions.items()
        },
        # Kind, direction and when. Not the record URIs: those name the
        # operator's own posts, and a CRM that wants the conversation has
        # bsky_url to follow.
        "recent": [
            {"kind": i.get("kind"), "direction": i.get("direction"),
             "occurred_at": i.get("occurred_at")}
            for i in recent_interactions
        ],
    }
    out["last_interaction_at"] = max(
        (v.get("last_at") for v in interactions.values() if v.get("last_at")),
        default=None)
    out["shared_connections"] = {
        # `exact` is the difference between "these" and "at least these" —
        # getKnownFollowers is complete, the affinity index is a sample. Passing
        # a sampled answer off as a complete one invites the conclusion that you
        # share nobody with someone you share thirty with.
        "total": shared.get("total", 0),
        "exact": bool(shared.get("exact")),
        "people": [
            {"did": p["did"], "handle": p.get("handle"),
             "display_name": p.get("display_name")}
            for p in shared.get("people", [])
        ],
    }
    out["posts"] = [
        {"uri": p.get("uri"), "text": p.get("text"),
         "indexed_at": p.get("indexed_at"), "like_count": p.get("like_count"),
         "repost_count": p.get("repost_count"),
         "reply_count": p.get("reply_count"),
         "is_repost": bool(p.get("is_repost"))}
        for p in posts
    ]
    events = [_event(e) for e in row.get("events") or []]
    out["events"] = [e for e in events if e is not None]
    return out


# ---------------------------------------------------------------- helpers

async def _person_row(store, identifier: str) -> dict:
    """One person by DID or handle, or a 404 that does not distinguish
    'nobody by that name' from 'hidden'."""
    did = identifier
    if not identifier.startswith("did:"):
        matches = await store.resolve_identities(handles=[identifier])
        if not matches:
            raise ApiError(404, "not_found", "sonde does not track that person.")
        did = matches[0]["did"]

    row = await store.follower_detail(did)
    # `follower_detail` answers for anyone in `actors`, including departed
    # followers and accounts sonde only knows because the operator follows
    # them. The API's people are current, non-hidden followers, and that has to
    # be re-checked here rather than assumed from the lookup above.
    if row is None or not row.get("is_current") or row.get("ignored_at"):
        raise ApiError(404, "not_found", "sonde does not track that person.")
    return row


async def _decorated(store, rows: list[dict]) -> list[dict]:
    """Attach circles and interaction recency to a page of people. Two queries
    for the page, not two per row."""
    dids = [r["did"] for r in rows]
    circles = await store.tags_for_many(dids)
    interactions = await store.last_interactions_for(dids)
    return [person_summary(r, circles.get(r["did"], []),
                           interactions.get(r["did"])) for r in rows]


# ----------------------------------------------------------------- routes

@router.get("/meta")
async def meta(request: Request) -> JSONResponse:
    """What this token can do, and how stale the data behind it is.

    A client should call this first and on a schedule. `last_sync_age_seconds`
    is the honest answer to "is sonde still running": the head sweep is every 15
    minutes, so an age in hours means something is wrong upstream and the CRM
    is looking at a frozen picture rather than a quiet one.
    """
    from sonde.db import store

    # Set by the middleware on every guarded path. Read defensively rather than
    # trusted: if this route is ever reachable without it, the answer is a
    # refusal, not a 500 that reveals the route ran unauthenticated.
    client = getattr(request.state, "api_client", None)
    if client is None:
        raise ApiError(401, "unauthenticated", "Send Authorization: Bearer <token>.")
    return JSONResponse({"data": {
        "api_version": API_VERSION,
        "client": {"name": client.name, "scopes": sorted(client.scopes)},
        "actor": settings.actor,
        "build": settings.build_sha,
        "counts": await store.counts(),
        "last_sync": await store.last_sync_summary(),
        "last_sync_age_seconds": await store.last_sync_age_seconds(),
        "limits": {"default_limit": DEFAULT_LIMIT, "max_limit": MAX_LIMIT,
                   "max_resolve": MAX_RESOLVE},
    }})


@router.get("/people")
async def people(
    q: str | None = None,
    circle: str | None = None,
    mutual: str | None = None,
    verified: str | None = None,
    min_followers: str | None = None,
    order: str = "did",
    direction: str = "asc",
    limit: str | None = None,
    cursor: str | None = None,
) -> JSONResponse:
    """Current followers, filtered and paged.

    The default order is `did`, which is the only column here that cannot
    change: it pages by keyset, so a sweep writing to the table while a client
    walks it can neither duplicate a row nor skip one. Every other order pages
    by offset and is for showing a ranking, not for syncing — API.md says which
    to use for what, and this docstring is the reason.
    """
    from sonde.db import store

    if order not in ORDERS:
        raise ApiError(400, "bad_parameter",
                       f"order must be one of: {', '.join(ORDERS)}")
    direction = "asc" if direction == "asc" else "desc"
    size = _limit_param(limit)
    floor = _int_param(min_followers, "min_followers")
    filters = dict(q=q, circle=circle, mutual=_flag(mutual),
                   verified=_flag(verified), min_followers=floor,
                   order=order, direction=direction, limit=size)
    fingerprint = _fingerprint(**filters)
    state = _decode_cursor(cursor, fingerprint) if cursor else {}

    common = dict(order=order, direction=direction, verified_only=_flag(verified),
                  mutual_only=_flag(mutual), min_followers=floor, query=q,
                  tag=circle)
    if order == "did":
        rows = await store.ranked_followers(
            limit=size, after_did=state.get("d"), **common)
        next_cursor = (_encode_cursor(fingerprint, d=rows[-1]["did"])
                       if len(rows) == size else None)
    else:
        offset = int(state.get("o", 0))
        rows = await store.ranked_followers(limit=size, offset=offset, **common)
        next_cursor = (_encode_cursor(fingerprint, o=offset + size)
                       if len(rows) == size else None)

    return JSONResponse({"data": await _decorated(store, rows),
                         "next_cursor": next_cursor})


@router.get("/people/{identifier}")
async def person(identifier: str) -> JSONResponse:
    """Everything about one person, by DID or handle."""
    from sonde.db import store

    row = await _person_row(store, identifier)
    did = row["did"]
    return JSONResponse({"data": person_detail(
        row,
        circles=await store.groups_for(did),
        affiliations=await store.affiliations_for(did),
        interactions=await store.interaction_breakdown(did),
        recent_interactions=await store.interactions_for(did),
        shared=await store.shared_connections(did),
        posts=await store.posts_for(did),
    )})


@router.get("/resolve")
async def resolve(request: Request) -> JSONResponse:
    """Line a client's own contacts up against sonde's, in one call.

    Repeatable `did` and `handle` parameters; one result per identifier
    *asked for*, in the order asked, including the ones sonde has never heard
    of. Returning only the matches would leave the client index-zipping its
    request against a shorter response — precisely the `getProfiles` hazard
    CLAUDE.md warns about, reproduced one layer up.
    """
    from sonde.db import store

    # `multi_items()` rather than two `getlist` calls, so `?handle=a&did=b`
    # answers in that order instead of grouping every did ahead of every
    # handle. The client is pairing this response with its own list.
    wanted = [(k, v.strip()) for k, v in request.query_params.multi_items()
              if k in ("did", "handle") and v.strip()]
    if not wanted:
        raise ApiError(400, "bad_parameter",
                       "Pass at least one did= or handle= parameter.")
    if len(wanted) > MAX_RESOLVE:
        raise ApiError(400, "too_many",
                       f"At most {MAX_RESOLVE} identifiers per call.")

    rows = await store.resolve_identities(
        dids=[v for k, v in wanted if k == "did"],
        handles=[v for k, v in wanted if k == "handle"])
    by_did = {r["did"]: r for r in rows}
    by_handle = {(r["handle"] or "").lower(): r for r in rows}

    out = []
    for kind, value in wanted:
        row = by_did.get(value) if kind == "did" else by_handle.get(value.lower())
        if row is None:
            # Hidden people land here too, and are indistinguishable from
            # people sonde has never seen. See `store.resolve_identities`.
            out.append({"query": value, "status": "unknown", "did": None,
                        "handle": None, "display_name": None,
                        "following_since": None, "i_follow": False,
                        "sonde_url": None})
            continue
        if row.get("is_current"):
            status = "follower"
        elif row.get("is_current") is not None:
            status = "departed"
        else:
            status = "known"
        out.append({
            "query": value, "status": status, "did": row["did"],
            "handle": row.get("handle"),
            "display_name": row.get("display_name"),
            "following_since": row.get("following_since"),
            "i_follow": bool(row.get("i_follow")),
            "sonde_url": _sonde_url(row["did"]),
        })
    return JSONResponse({"data": out})


@router.get("/circles")
async def circles() -> JSONResponse:
    """The operator's own classification vocabulary. Live circles only —
    archived ones are the record of a decision, not a category to file
    anybody under."""
    from sonde.db import store

    rows = await store.group_summary()
    # Counted the way the roster counts, not the way the circles page does:
    # `members` here must equal the number of people `/people?circle=` returns,
    # or a client reconciling the two has to guess which is wrong.
    listable = await store.circle_member_counts()
    return JSONResponse({"data": [
        {"slug": r["slug"], "name": r["name"],
         "members": listable.get(r["slug"], 0),
         "unreviewed": r.get("unreviewed") or 0,
         "people_url": f"{apikey.PREFIX}/people?circle={r['slug']}"}
        for r in rows
    ]})


@router.get("/changes")
async def changes(since: str | None = None, limit: str | None = None,
                  cursor: str | None = None) -> JSONResponse:
    """The follow-event log, oldest first, for incremental sync.

    Ascending by `id` and paged on it, which is the only ordering an append-only
    log can be resumed on: `detected_at` has second granularity and a sweep
    writes hundreds of rows inside one second, so a client resuming from a
    timestamp would re-read or skip whatever shared it.

    Keep the last `next_cursor` and pass it back next time. `since` is for the
    first call only.
    """
    from sonde.db import store

    size = _limit_param(limit)
    fingerprint = _fingerprint(since=since, limit=size)
    state = _decode_cursor(cursor, fingerprint) if cursor else {}
    rows = await store.change_feed(
        since=since, after_id=state.get("i"), limit=size,
        events=PUBLIC_EVENTS)

    out = []
    for row in rows:
        event = _event(row)
        if event is None:
            continue
        event["did"] = row["did"]
        event["handle"] = row.get("handle")
        event["display_name"] = row.get("display_name")
        event["sonde_url"] = _sonde_url(row["did"])
        out.append(event)

    next_cursor = (_encode_cursor(fingerprint, i=rows[-1]["id"])
                   if len(rows) == size else None)
    return JSONResponse({"data": out, "next_cursor": next_cursor})


# ------------------------------------------------------------------ writes
#
# Circle membership, and nothing else. Following is the one thing sonde writes
# to Bluesky, it is public and it is done in someone's name, so it stays a
# deliberate click on a page that has already checked the subject is a current
# follower — not something a program can be made to do in a loop.
#
# There is no batch form on purpose. `replace_my_follows` carries a
# refuse-an-implausible-sweep rail because a bug once wiped the follow list, and
# a bulk tag endpoint would need the same rail before it could be trusted. One
# person per call is auditable in `group_members.decided_at` and cannot lose
# anything wholesale.

@router.post("/circles")
async def create_circle(request: Request) -> JSONResponse:
    """Create a circle, or report that it already exists.

    Separate from tagging, and tagging will not create one: an API that
    conjures a circle from a typo would fill the vocabulary with near-misses
    that nobody notices until a report is wrong.
    """
    from sonde.db import store

    body = await _json_body(request)
    name = str(body.get("name") or "").strip()
    result = await store.create_group(name)
    if result["status"] == "invalid":
        raise ApiError(400, "bad_name",
                       "A circle needs a name with at least one letter or "
                       "digit in it.")
    if result["status"] == "archived":
        # The web UI drops this on the floor and says nothing, which is BUG-01.
        # A program cannot notice silence, so here it is a refusal with the
        # reason in it.
        raise ApiError(409, "circle_archived",
                       f"A circle with the slug {result['slug']!r} exists but "
                       "is archived. Restore it in sonde before using it.")
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        {"data": {"slug": result["slug"], "name": result["name"],
                  "created": result["status"] == "created"}},
        status_code=status)


@router.put("/people/{identifier}/circles/{slug}")
async def add_to_circle(identifier: str, slug: str) -> JSONResponse:
    """Put someone in a circle. Idempotent."""
    from sonde.db import store

    row = await _person_row(store, identifier)
    await _require_live_circle(store, slug)
    return JSONResponse({"data": await _set_membership(
        store, row["did"], slug, member=True)})


@router.delete("/people/{identifier}/circles/{slug}")
async def remove_from_circle(identifier: str, slug: str) -> JSONResponse:
    """Take someone out of a circle. Idempotent: removing a membership that was
    never there is a no-op reported as `changed: false`, not an error."""
    from sonde.db import store

    row = await _person_row(store, identifier)
    await _require_live_circle(store, slug)
    return JSONResponse({"data": await _set_membership(
        store, row["did"], slug, member=False)})


async def _set_membership(store, did: str, slug: str, *, member: bool) -> dict:
    """Add or remove, and report honestly whether anything moved.

    `changed` is computed from the memberships either side of the write rather
    than from what the store function returned. Neither return value answers
    this question: `tag_actor` upserts and reports success, which is True for
    someone already in the circle, and `untag_actor` reports the row count of an
    UPDATE that matches a row it has already set to rejected. A client
    reconciling two systems needs to know whether *this* call moved anything.
    """
    before = {c["slug"] for c in await store.groups_for(did)}
    if member:
        await store.tag_actor(slug, did)
    else:
        await store.untag_actor(slug, did)
    after = [c["slug"] for c in await store.groups_for(did)]
    return {"did": did, "circle": slug, "member": member,
            "changed": (slug in after) != (slug in before),
            "circles": after}


async def _require_live_circle(store, slug: str) -> None:
    """`tag_actor` returns False for a missing circle and for an archived one
    alike. A client needs them apart: one is a typo, the other is a decision it
    is about to undo."""
    state = await store.group_state(slug)
    if state is None:
        raise ApiError(404, "no_such_circle",
                       f"There is no circle with the slug {slug!r}.")
    if state["archived"]:
        raise ApiError(409, "circle_archived",
                       f"The circle {slug!r} is archived.")


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_body", "Send a JSON object.") from None
    if not isinstance(body, dict):
        raise ApiError(400, "bad_body", "Send a JSON object.")
    return body
