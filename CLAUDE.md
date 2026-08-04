# sonde — operational notes for Claude

Fleet-wide ubuntuplex documentation (architecture, all services, Traefik/Authelia
patterns, troubleshooting) lives centrally in `~/dev/reverse-proxy/` — its
`README.md`, `TODO.md`, `HISTORY.md`, and `docs-site/docs/`. Check there first
for anything that isn't specific to this repo. In particular
[python-apps.md](../reverse-proxy/docs-site/docs/services/python-apps.md) is the
canonical deploy pattern this app follows.

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

## Backups — deliberately incomplete

`follow_events` is the only table that cannot be re-fetched from Bluesky. A
nightly `VACUUM INTO` writes a snapshot to `/backup` (a host bind mount, 14 kept).

**That is on-box only, and knowingly so.** It protects against DB corruption or a
bad migration, not against losing the host. Docker volumes on ubuntuplex are not
backed up, and the operator has said off-box backup is not a priority and does
not use Syncthing for it — so don't wire one up, and don't describe the snapshots
as off-box. If this ever becomes a priority, ask which mechanism to target rather
than assuming.
