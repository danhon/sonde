# sonde API — v1

A read-mostly HTTP/JSON interface to sonde's follower data, for other programs
on the fleet. Built for a personal CRM; nothing in it is CRM-specific.

**Base URL** `https://sonde.sgc.rayandhon.com/api/v1`
**Auth** `Authorization: Bearer <token>` on every request, including reads.

The whole surface is nine endpoints. If you only ever call two, call
`GET /people` and `GET /changes`.

```bash
curl -sH "Authorization: Bearer $SONDE_TOKEN" \
     https://sonde.sgc.rayandhon.com/api/v1/people?limit=5 | jq
```

---

## 1. Getting a token

Tokens live in sonde's environment, comma-separated, one entry per client:

```env
SONDE_API_TOKENS=crm:<secret>:write,dashboard:<secret>
```

| Field | |
|---|---|
| `name` | Yours, for the log. It is how a refusal is attributed and how one client is revoked without rotating the others |
| `secret` | At least 24 characters, no `:` and no `,`. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| scopes | `write` grants the three write endpoints. Omit it and the token is read-only |

A token is **read-only unless it says `write`**. Give the CRM a write token and
anything else a read token; a read token that leaks cannot change anything.

An entry whose secret is under 24 characters is dropped with an error in the
log rather than honoured — a guessable token advertises a door and then leaves a
key in it. With no tokens set at all the API answers `503 api_disabled`, because
an operator who has issued no credential has not asked for a machine surface.

Restart the container after changing tokens.

## 2. Shape of every response

Success is always an object with `data`. Lists add `next_cursor`.

```json
{ "data": [ … ], "next_cursor": "eyJmIjoiOWY…" }
```

Errors are always an object with `error`, at every status, from every endpoint
— including 401s and mistyped URLs:

```json
{ "error": { "code": "cursor_mismatch", "message": "That cursor belongs to a different query…" } }
```

Branch on `code`, not on `message`; the wording may improve, the codes will not.

| Status | `code` | Means |
|---|---|---|
| 400 | `bad_parameter`, `bad_cursor`, `cursor_mismatch`, `bad_body`, `bad_name`, `too_many` | Fix the request |
| 401 | `unauthenticated` | Missing or wrong token |
| 403 | `forbidden` | Valid token, wrong scope |
| 404 | `not_found`, `no_such_circle` | |
| 409 | `circle_archived` | The circle exists but was archived in sonde |
| 503 | `api_disabled` | No tokens configured |

All timestamps are ISO-8601 UTC strings. All responses are `Cache-Control:
private, no-store`.

### Pagination

Two lines of client code, the same for every list:

```python
cursor = None
while True:
    page = get("/people", cursor=cursor)
    yield from page["data"]
    cursor = page["next_cursor"]
    if cursor is None:
        break
```

A cursor is opaque and carries a fingerprint of the query that produced it.
Change any filter, order or limit and reuse the old cursor, and you get
`400 cursor_mismatch` rather than rows from one query numbered by another.

`limit` defaults to 50 and is capped at 200; ask for more and you get 200.

## 3. Endpoints

| | |
|---|---|
| `GET /meta` | Token, counts, freshness |
| `GET /people` | The roster, filtered and paged |
| `GET /people/{did-or-handle}` | Everything about one person |
| `GET /resolve` | Match your contacts against sonde's |
| `GET /circles` | The classification vocabulary |
| `GET /changes` | The follow-event log, for incremental sync |
| `POST /circles` | Create a circle · **write** |
| `PUT /people/{id}/circles/{slug}` | Add to a circle · **write** |
| `DELETE /people/{id}/circles/{slug}` | Remove from a circle · **write** |

### `GET /meta`

Call it first, and on a schedule.

```json
{"data": {
  "api_version": "v1",
  "client": {"name": "crm", "scopes": ["read", "write"]},
  "actor": "danhon.com",
  "build": "e926edc",
  "counts": {"tracked": 10038, "verified": 147, "mutuals": 412,
             "departed": 1203, "private": 1838, "ignored": 61, "reported": 11451},
  "last_sync": {"kind": "head", "ended_at": "…"},
  "last_sync_age_seconds": 431,
  "limits": {"default_limit": 50, "max_limit": 200, "max_resolve": 100}
}}
```

`last_sync_age_seconds` is the honest answer to "is sonde still running". The
head sweep is every 15 minutes, so an age in hours means something is wrong
upstream and you are looking at a frozen picture rather than a quiet one.

On `counts`: `reported` is what Bluesky says the follower count is and
`tracked` is what sonde can enumerate. **The gap is permanent, not a bug** — the
AppView drops accounts it cannot serve. `ignored` is a count with no names
behind it anywhere in this API; see §5.

### `GET /people`

| Parameter | |
|---|---|
| `q` | Substring of handle, display name or bio |
| `circle` | Slug. This is also how you list a circle's members |
| `mutual`, `verified` | `true` to filter |
| `min_followers` | Integer |
| `order` | `did` (default), `influence`, `followers`, `follows`, `since`, `handle`, `name` |
| `direction` | `asc` (default) or `desc` |
| `limit`, `cursor` | See above |

**Use the default order to sync. Use any other order to display.** `did` is the
only column here that cannot change, and it pages by keyset: a sweep writing to
the table while you walk it can neither hand you a row twice nor skip one. Every
other order pages by offset, and a rescore between page 3 and page 4 moves
people across the boundary.

Do not check your row count against `counts.tracked`. `/people` returns current,
non-hidden followers and not the operator's own account; `tracked` counts more
than that. Page until `next_cursor` is null.

```json
{"did": "did:plc:…", "handle": "alice.example", "display_name": "Alice",
 "description": "Reporter. https://ft.com/…", "avatar_url": "https://…",
 "account_created_at": "2023-05-02T…", "followers_count": 8123,
 "follows_count": 431, "posts_count": 2044, "last_post_at": "2026-08-05T…",
 "verified": true, "verified_status": "valid", "trusted_verifier": false,
 "is_private": false, "influence_score": 71.4, "relationship_score": 12.0,
 "following_since": "2025-11-03T…", "following_since_exact": true,
 "first_seen_at": "2026-07-26T…", "list_rank": 42, "is_mutual": true,
 "circles": ["journalists"],
 "organisation": {"name": "…", "role": "…", "confidence": 0.95, "method": "domain"},
 "last_interaction_at": "2026-08-01T…",
 "bsky_url": "https://bsky.app/profile/alice.example",
 "sonde_url": "https://sonde.sgc.rayandhon.com/followers/did:plc:…"}
```

Four fields worth reading twice:

- **`following_since_exact`.** `false` means `following_since` is when *sonde
  first saw them*, not when they followed. Exact dates need an authenticated
  sweep, and rows predating that never get one. Do not present an inexact date
  as an anniversary.
- **`is_private`.** They asked Bluesky not to show them to logged-out viewers.
  sonde shows them to the operator; if your client republishes anything, this is
  the flag that says not to.
- **`organisation`** is the institution matcher's answer and is currently `null`
  for everyone: the institution tables are empty in production (BUG-11).
  Employers live in `affiliations` on the detail record.
- **`last_interaction_at`** is the last like, reply, repost, quote or mention in
  either direction — the field a contact list sorts by.

### `GET /people/{did-or-handle}`

Everything sonde knows, in one call: the summary above plus `score`,
`relationship`, `circles` (as objects, with the evidence for each), `affiliations`,
`wikidata`, `links`, `interactions`, `shared_connections`, `posts` and `events`.

This is the expensive endpoint — seven queries. It is one call rather than seven
so your client does not need seven; do not put it in a loop over the whole
roster. Use `/people` for that.

DIDs and handles both work. **Store the DID**: handles change, and when one
does you will see it as a `handle_changed` event rather than as a person
vanishing.

`shared_connections.exact` matters. `true` means the list is complete
(`getKnownFollowers`); `false` means it came from a sampled index and reads "at
least these". A sampled answer presented as complete invites the conclusion that
you share nobody with someone you share thirty with.

### `GET /resolve`

Repeatable `did=` and `handle=`, up to 100 per call, mixed freely. Handles are
matched case-insensitively.

```
GET /resolve?handle=alice.example&did=did:plc:xyz&handle=nobody.example
```

You get **one result per identifier you asked for, in the order you asked**,
including the ones sonde has never heard of — so you can zip the response
against your own list without checking lengths.

```json
{"data": [
  {"query": "alice.example", "status": "follower", "did": "did:plc:…",
   "handle": "alice.example", "display_name": "Alice",
   "following_since": "2025-11-03T…", "i_follow": true, "sonde_url": "https://…"},
  {"query": "nobody.example", "status": "unknown", "did": null, "handle": null,
   "display_name": null, "following_since": null, "i_follow": false,
   "sonde_url": null}
]}
```

| `status` | |
|---|---|
| `follower` | Currently follows the operator |
| `departed` | Did once, does not now |
| `known` | In sonde's data but never a follower — usually someone the operator follows |
| `unknown` | Not tracked, **or** hidden (§5) |

### `GET /circles`

```json
{"data": [{"slug": "journalists", "name": "Journalists", "members": 214,
           "unreviewed": 12, "people_url": "/api/v1/people?circle=journalists"}]}
```

Live circles only; archived ones are the record of a decision, not a category to
file anyone under. `members` counts the same people `/people?circle=` returns —
not memberships, which would include the hidden and the departed and disagree
with the list under it. `unreviewed` is how many memberships a rule proposed and
no human has confirmed.

### `GET /changes`

The follow-event log, **oldest first**, for keeping a local copy current without
re-reading the roster.

| Parameter | |
|---|---|
| `since` | ISO timestamp. First call only |
| `limit`, `cursor` | See above |

```json
{"data": [{"event": "handle_changed", "detected_at": "2026-08-04T…",
           "reason": null, "detail": "old.example → alice.example",
           "did": "did:plc:…", "handle": "alice.example",
           "display_name": "Alice", "sonde_url": "https://…"}],
 "next_cursor": "…"}
```

Events: `followed`, `returned`, `departed`, `handle_changed`. `detail` is
populated only for `handle_changed`, where it is `old → new`.

**Keep the last `next_cursor` and pass it back next time.** The feed is ordered
and resumed by row id, not by timestamp, because a full sweep writes hundreds of
departures inside one second and a second-granularity timestamp cannot order
within it. `since` is a convenience for the first call, not a resume mechanism.

An empty `data` with a null `next_cursor` means you are caught up.

### Writes

Three endpoints, all requiring the `write` scope, all about circle membership.
Following is not among them: it writes a real, public follow record on the
operator's account, so it stays a deliberate click on a page that has already
checked the subject is a current follower.

**`POST /circles`** — `{"name": "VC friends"}` → `201` with
`{"slug": "vc-friends", "name": "VC friends", "created": true}`, or `200` with
`created: false` if it already existed. `409 circle_archived` if the slug exists
but was archived; restore it in sonde first.

**`PUT /people/{id}/circles/{slug}`** and **`DELETE`** the same — idempotent. The
response carries the person's full circle list afterwards, so you never need a
follow-up read:

```json
{"data": {"did": "did:plc:…", "circle": "journalists", "member": true,
          "changed": true, "circles": ["journalists", "uk"]}}
```

`changed` says whether *this call* moved anything, so a reconciliation loop can
count real edits.

Tagging will not create a circle: `404 no_such_circle`. Type-to-create is right
on a page a human is looking at and wrong for a program, where a typo becomes a
near-duplicate category nobody notices until a report is wrong. Create it
explicitly, then tag.

There is no batch form, deliberately. sonde's other bulk write carries a
refuse-an-implausible-sweep rail because a bug once wiped the follow list, and a
bulk tag endpoint would need the same rail before it could be trusted. One
person per call is auditable and cannot lose anything wholesale.

## 4. A minimal client

```python
import os, httpx

BASE = "https://sonde.sgc.rayandhon.com/api/v1"
http = httpx.Client(headers={"Authorization": f"Bearer {os.environ['SONDE_TOKEN']}"},
                    base_url=BASE, timeout=30)

def get(path, **params):
    r = http.get(path, params={k: v for k, v in params.items() if v is not None})
    if r.status_code >= 400:
        raise RuntimeError(r.json()["error"])          # always this shape
    return r.json()

def pages(path, **params):
    cursor = None
    while True:
        page = get(path, cursor=cursor, **params)
        yield from page["data"]
        cursor = page.get("next_cursor")
        if cursor is None:
            return

# Full sync: default order, so it is safe while the sweeps are running.
people = {p["did"]: p for p in pages("/people", limit=200)}

# Incremental afterwards: keep the cursor, not a timestamp.
cursor = None
while True:
    page = get("/changes", cursor=cursor, limit=200)
    for event in page["data"]:
        apply(event)
    if page["next_cursor"] is None:
        break
    cursor = page["next_cursor"]
```

Suggested cadence: a full `/people` walk daily, `/changes` every 15 minutes to
match the head sweep, `/people/{did}` on demand when a human opens a contact.

## 5. What this API will not tell you

Not oversights. Each has a reason, and asking a different way will not produce
them.

**Hidden people do not exist here.** sonde can hide a follower, usually because
a curated moderation list names them. Those people are absent from `/people`,
their detail route is `404`, they cannot be tagged, and `/resolve` reports them
as `unknown` — indistinguishable from someone sonde has never seen. That
ambiguity is the point: the alternative tells you that sonde hid them, which
republishes an accusation about a named person. `/meta` gives the count and no
names.

**No moderation data at all** — not the lists, not which list matched, not the
reason an event was recorded. Departure reasons are allowlisted for the same
reason.

**No operations.** Job history, scheduler state, sync internals, weights, SMTP,
backup state and credentials are not on this surface at anything but
`/meta`-level aggregate. `/api/status` is a different route, still behind
Authelia.

**No bulk export.** `/export.csv` stays operator-only. Paging `/people` gets you
the same rows; the point is that it is deliberate.

**Nothing about the operator's own posts** beyond interaction counts — no record
URIs, no notification history.

Two more, which are properties rather than omissions:

- **Everything here is personal data**, and an aggregate is a new artefact even
  when every input was public on Bluesky. The influence score and the circles are
  sonde's opinions about named people. Treat a local copy the way you would treat
  sonde's database.
- **The token is not a session.** It carries no Authelia identity and grants
  nothing outside `/api/v1`. It does not expire; rotate it by editing the
  environment and restarting.

## 6. Compatibility

Within `v1`: fields may be **added**, and endpoints may be added. Fields will not
be removed or change meaning, and `code` values will not change. Ignore keys you
do not recognise. Anything else gets a `/api/v2` and its own router, so an
existing token's behaviour cannot change under it.

There is no `/openapi.json` — it is switched off, because it would publish a
machine-readable map of every write route on a router that has no Authelia. This
document is the contract.

---

Design notes and the reasoning behind the refusals are in `sonde/web/api.py`
and `sonde/web/apikey.py`; `tests/test_api.py` asserts every field named here.
`ACCESS.md` covers a different audience — an anonymous public reader — and its
rules are stricter than these.
