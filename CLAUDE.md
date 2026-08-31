# sonde — operational notes for Claude

Fleet-wide ubuntuplex documentation (architecture, all services, Traefik/Authelia
patterns, troubleshooting) lives centrally in `~/dev/reverse-proxy/` — its
`README.md`, `TODO.md`, `HISTORY.md`, and `docs-site/docs/`. Check there first
for anything that isn't specific to this repo. In particular
[python-apps.md](../reverse-proxy/docs-site/docs/services/python-apps.md) is the
canonical deploy pattern this app follows.

**Before editing anything in that repo, read
[DOCUMENTING.md](../reverse-proxy/DOCUMENTING.md).** It is the contract every
session follows when updating fleet docs: what belongs centrally versus here,
where each kind of fact goes, the checklist for adding or changing a service,
the house style, and the deploy a `docs-site/` change needs. Recording the
fleet-relevant part of work done here is part of that work, as its own commit
in that repo.

Docs in this repo, split by what they answer:

| | |
|---|---|
| [README.md](README.md) | Running, configuring and deploying it |
| [PLAN.md](PLAN.md) | The design — what was measured, what follows from it, and the rules the code must obey |
| [HISTORY.md](HISTORY.md) | What was built, in order, and why. Where a threshold's justification lives |
| [ACCESS.md](ACCESS.md) | Who can see what — the public read-only view and the rules that keep it redacted |
| [BUGS.md](BUGS.md) | Reproduced defects, ranked. Delete an entry when it is fixed |
| [SCORING.md](SCORING.md) | The influence score in full |

All grounded in measurements against the live API from 2026-07-26 onward — if
you change a number, re-measure rather than re-estimate. Put new milestone
narrative in HISTORY.md, not PLAN.md: that is how PLAN.md reached 1,888 lines,
three quarters of which was changelog appended to a design document.

## Deployment: always `make deploy`, never bare `docker compose`

`SERVICE_HOST` is only ever injected by the Makefile. Bypass it and Traefik's
router rule becomes `Host(``)`, silently 404ing the site while `docker ps` shows
the container perfectly healthy. `make verify` checks the label after a deploy.

## Non-obvious things that will bite

These were all found by probing the live API. Tests exist for each; if a test
here starts failing, suspect the API changed rather than the test.

1. **The cursor is the only end-of-list signal.** `getFollowers` pages come back
   short — mean 87.3 of a requested 100, only 5 of 115 pages full — because the
   AppView drops unservable accounts *after* selecting 100 records. Stopping on
   `len(page) < limit` ends the sweep on page 1.
2. **`getProfiles` silently omits actors it can't resolve** — HTTP 200, shorter
   array, no error. Always map results back by DID. Index-zipping assigns
   follower counts to the wrong people with no error anywhere.
3. **`followersCount` will never equal the number of enumerable followers.**
   11,451 vs 10,041 as measured. That gap is permanent, not a bug.
4. **`verification` is omitted entirely for unverified accounts** — it is not
   set to `"none"`. Parse defensively.
5. **The follower list is newest-follow-first.** This is what makes the head
   sweep possible; if it ever stops being true, arrivals will be missed silently.
6. **Only a complete full sweep may mark departures.** Head sweeps must never
   compute them — they deliberately don't see most of the list.

## Following is the one thing sonde writes

Everything else reads. `/followers/{did}/follow` creates a real, public follow
record on the operator's account, so it is guarded: follow-back only (the
subject must be a current, non-hidden follower), never retried on failure, the
record URI is stored so the same control undoes it, and every attempt — success
or failure — lands in `follow_events`.

`replace_my_follows` deletes and reinserts wholesale. It must keep carrying
`follow_uri` across: `getFollows` returns subjects, not record keys, so a lost
URI can never be recovered and that follow becomes impossible to undo from
sonde.

Turn it all off with `ENABLE_FOLLOW_WRITE=false`.

## Every write must come from sonde's own pages

A middleware refuses any non-GET request whose `Sec-Fetch-Site` is not
`same-origin`/`none`, falling back to an `Origin`-against-`Host` comparison. The
line that matters refuses **`same-site`**: the Authelia cookie is scoped to
`domain: sgc.rayandhon.com` with the default `same_site: lax`, so every sibling
service on ubuntuplex is same-site and could otherwise POST here with the
operator's session attached.

Consequences for anything added later: a new POST route is covered automatically
and needs no decorator, but **an endpoint meant to be called from another origin
will 403** and no amount of route-level code will change that — the middleware
runs in front of the router. Requests with neither header (curl, the test suite)
are allowed by design, which is why existing POST tests did not have to change.
`sonde/web/origin.py` has the reasoning; `tests/test_origin.py` enumerates every
write route and checks both directions.

## The API router is the one thing here Authelia does not guard

`/api/v1` is served by its own Traefik router with no `authelia@file`, because a
program cannot do an interactive 2FA login. A bearer token checked in
`sonde/web/apikey.py` stands in for it, as a middleware over the whole prefix.

**`PathPrefix(`/api/v1`)` in `compose.yml` is the containment.** Widen it to
`/api` and `/api/status` — scheduler internals — loses Authelia. Drop it and the
whole admin surface does. `make verify` and `tests/test_api_auth.py` both assert
the rule; keep it that way.

Consequences for anything added later: a route added under `/api/v1` is
token-guarded before it is written, and a route added anywhere else is not
reachable with a token at all. Responses are assembled from explicit dict
literals, never by copying a row — `SELECT a.*` means a blocklist publishes the
next column added to `actors`. `tests/test_api.py` asserts every response
against a declared key allowlist, so adding a field means editing a test on
purpose. API.md is the contract other projects read; `/openapi.json` is off.

## Jinja macros must be imported `with context`

`{% from "_sort.html" import who %}` does **not** give the macro access to
`settings`, so the follow button silently never renders. Every import of
`_sort.html` carries `with context`, and a test enforces it.

## Point the mobile eval at a POPULATED database

The env var is `DB_PATH`, not `SONDE_DB_PATH`. Getting it wrong starts the
server on a fresh empty `./sonde.db`, every page renders its empty state, and
the eval passes while testing nothing. Four real overflow bugs were sitting
behind that mistake — the dashboard tiles, both dashboard lists, and the
chart bar rows — none of which can overflow when there are no rows to render.

    DB_PATH=/path/to/real.db uv run --with playwright python -m evals.mobile_check

A grid or flex item defaults to `min-width: auto` and will not shrink below its
content however much its children truncate. `min-w-0` on the item is the fix;
truncating the children is not.

## Mobile: the static tests cannot prove a page fits

`tests/test_responsive.py` checks structure — tables wrapped, nav collapsing,
grids with a mobile base — and runs in the normal suite. It cannot detect actual
overflow. Only the browser can:

```
uv run --with playwright playwright install chromium   # first run only
uv run --with playwright python -m evals.mobile_check  # needs a server running
```

Never add `overflow-x: hidden` to the body to silence a layout bug. It hides the
symptom, keeps the broken layout, and blinds the eval by clipping the evidence
it measures.

## Alerting never reports the past, and claims before it sends

`sonde/notify/alerts.py` emails when a verified account (or a trusted verifier)
arrives. Two invariants, both load-bearing:

**The watermark is only stamped when alerting is actually enabled.** It records
where the event log stood the first time alerting ran, and nothing before it is
ever alerted. If it were stamped on a disabled run, switching alerts on later
would be silent for the wrong reason; if it weren't stamped at all, deploying
the feature would email about every verified arrival of the past two days.

**The `verified_alerts` row is written before the send, not after.** A crash
mid-send therefore costs a missed alert, not a duplicate one — and a missed
alert isn't lost, because the daily digest lists the same arrival and reports
alerts that exhausted their three attempts. Reversing the order trades a
bounded, backstopped failure for an unbounded resend loop.

Consequences for anything added later: sending runs from `sonde/scheduler.py`
after each sweep, never inside `sync/runner.py` — SMTP latency must not extend
a sweep and an SMTP error must not fail one. The retry path is driven by the
15-minute head sweep, so there is no separate timer. A new alertable event kind
goes in `store.ALERT_EVENTS`; keying the claim table on the event id rather
than the DID is what lets someone leave and come back and be news twice.

## Backups — deliberately incomplete

`follow_events` is the only table that cannot be re-fetched from Bluesky. A
nightly `VACUUM INTO` writes a snapshot to `/backup` (a host bind mount, 14 kept).

**That is on-box only, and knowingly so.** It protects against DB corruption or a
bad migration, not against losing the host. Docker volumes on ubuntuplex are not
backed up, and the operator has said off-box backup is not a priority and does
not use Syncthing for it — so don't wire one up, and don't describe the snapshots
as off-box. If this ever becomes a priority, ask which mechanism to target rather
than assuming.
