# sonde — operational notes for Claude

Fleet-wide ubuntuplex documentation (architecture, all services, Traefik/Authelia
patterns, troubleshooting) lives centrally in `~/dev/reverse-proxy/` — its
`README.md`, `TODO.md`, `HISTORY.md`, and `docs-site/docs/`. Check there first
for anything that isn't specific to this repo. In particular
[python-apps.md](../reverse-proxy/docs-site/docs/services/python-apps.md) is the
canonical deploy pattern this app follows.

Design docs in this repo: [PLAN.md](PLAN.md) for the app, [SCORING.md](SCORING.md)
for the influence score. Both are grounded in measurements taken against the live
API on 2026-07-26 — if you change a number, re-measure rather than re-estimate.

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

## Backups — deliberately incomplete

`follow_events` is the only table that cannot be re-fetched from Bluesky. A
nightly `VACUUM INTO` writes a snapshot to `/backup` (a host bind mount, 14 kept).

**That is on-box only, and knowingly so.** It protects against DB corruption or a
bad migration, not against losing the host. Docker volumes on ubuntuplex are not
backed up, and the operator has said off-box backup is not a priority and does
not use Syncthing for it — so don't wire one up, and don't describe the snapshots
as off-box. If this ever becomes a priority, ask which mechanism to target rather
than assuming.
