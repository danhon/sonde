# sonde

A follower tracker for [Bluesky](https://bsky.app). Sweeps who follows
[@danhon.com](https://bsky.app/profile/danhon.com), tracks arrivals and
departures over time, shows which followers are **verified**, and ranks which
ones are **influential** — with a score it can explain.

> **Status: in production**, deployed on ubuntuplex behind Authelia. All three
> goals are answered, plus mutuals, circles, institutions, interaction
> leaderboards, follow-back, five visualisations, a nightly snapshot and a
> read-mostly JSON API for other programs.
> 900 tests pass; the browser eval checks 226 page renders across two engines
> and seven viewports.

This README is for running and deploying sonde. The reasoning behind it lives
elsewhere:

| | |
|---|---|
| [PLAN.md](PLAN.md) | The design — what was measured and what follows from it |
| [HISTORY.md](HISTORY.md) | What was built, in order, and why |
| [BUGS.md](BUGS.md) | What is currently wrong |
| [SCORING.md](SCORING.md) | How the influence score is calculated |
| [API.md](API.md) | The HTTP/JSON API other programs read sonde through |

---
## What it does

- **Spots new followers within 15 minutes** for about two API calls, because the follower list is ordered newest-first
- **Sweeps the whole list** every 6 hours — 115 requests, ~39 seconds — to catch departures, which can only be proven by absence
- **Shows verified followers** — currently 147 — with who verified them and when. This data rides along with the follower list at no extra API cost
- **Ranks influential followers** on an explainable 0–100 score built from reach, relevance, selectivity, output, and verification. Every row shows exactly what produced its number
- **Tracks change over time** — arrivals, departures, returns, handle changes, and a daily growth chart, with guards so a failed sync can never masquerade as a mass unfollow
- **Flags mutuals** by cross-referencing your own follow list
- **Needs no credentials.** Everything above runs unauthenticated. An app password is optional and only unlocks the extras (exact affinity, interactions, follow-back, follow dates)
## Why "sonde"

A sonde is an instrument you send up into something you can't observe directly,
to take measurements and radio them back. Same idea.
## Web UI

**FastAPI** + **Jinja2** + **Tailwind CSS** (CDN), server-rendered. Behind
Authelia 2FA at `sonde.sgc.rayandhon.com`.

| Route | Description |
|---|---|
| `/` | Dashboard: tracked vs reported totals, verified, mutuals, growth chart, recent arrivals/departures, top 10 influential, last sync |
| `/followers` | Full table — sort by influence / follower count / recency / handle; filter by verified, mutual, private, minimum followers, free text |
| `/followers/{did}` | Detail: profile, score breakdown, verification records, full event history |
| `/influential` | Leaderboard — **by reach** or **by relevance to you** — each row decomposed |
| `/verified` | The 147, grouped by issuing verifier |
| `/changes` | Arrival/departure timeline |
| `/api/status` | Live job progress, next scheduled runs, last-sync age (polled by the nav strip) |
| `/api/v1/*` | Machine-readable JSON for other programs — token, not Authelia. See [API.md](API.md) |
| `/healthz` | Unauthenticated liveness probe — also reports running jobs and scheduler state |

Every page carries a live activity strip showing what is running, how far
through it is, and when the next job is due — polling every 3s while a job runs
and every 15s when idle.
## Running

```bash
uv run sonde                  # web UI only
uv run sonde --once           # one full sweep (115 calls, ~38s) and exit
uv run sonde --head           # one head sweep (1–2 calls) and exit
uv run sonde --hydrate        # fill in follower counts; --limit N to cap
uv run sonde --rescore        # recompute every influence score
uv run sonde --backup         # daily rollup + snapshot
uv run sonde --schedule       # web UI + scheduler (production mode)
```

A cold start needs `--once` before anything else: the head sweep deliberately
no-ops without a baseline, since with nothing known every page looks new and it
would walk all 115 pages every 15 minutes.
## Local development

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync
cp .env.example .env
$EDITOR .env

uv run pytest
```
## Configuration

Copy `.env.example` to `.env` and fill it in. **Never commit `.env`.**

```env
# Traefik routing (set by Makefile; override here only if needed)
SERVICE_HOST=sonde.sgc.rayandhon.com

# Whose followers to track — handle or DID
BLUESKY_ACTOR=danhon.com

# Bluesky API
BLUESKY_API_BASE=https://public.api.bsky.app
API_RATE_LIMIT_PER_SECOND=3        # cap is 3000 req / 5 min / IP, shared house-wide
                                   # with BlueBirdNET and atproto-labeler — hence 3, not 5

# Sync cadence
HEAD_SWEEP_MINUTES=15              # cheap check for new followers (1–2 calls)
FULL_SWEEP_HOURS=6                 # whole list; also sets unfollow latency: 2 sweeps = 12h
PROFILE_TTL_DAYS=7                 # how often to re-fetch each follower's counts

# Display policy for followers who turned off logged-out visibility (1,838 of them).
# false = show them, marked "private". true = count them, withhold from tables/exports.
RESPECT_NO_UNAUTHENTICATED=false

# Optional — only for tier-3 enrichment and list writing. Leave blank to run fully
# unauthenticated. Use an app password from bsky.app/settings/app-passwords,
# never your account password.
BLUESKY_APP_PASSWORD=
ENABLE_TIER3=false
TIER3_TOP_N=500

# API access for other programs. Comma-separated name:secret[:write] entries;
# empty (the default) switches the API off entirely. A token is read-only
# unless its entry says `write`. See API.md.
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
SONDE_API_TOKENS=

# Database — set by Dockerfile default; only override if needed
# DB_PATH=/data/sonde.db
TZ=America/Los_Angeles
```
## Deploying (Docker — this is the live production deployment)

Production runs as a Docker container behind Traefik + Authelia, following the
standard [python-apps pattern](../reverse-proxy/docs-site/docs/services/python-apps.md).

```bash
make deploy
```

**Never run `docker compose build` / `docker compose up` directly on the server.**
`SERVICE_HOST` is only ever set by the Makefile. Bypass it and Compose
substitutes an empty string, the Traefik rule becomes ``Host(``)``, and the site
404s while `docker ps` shows the container perfectly healthy — see
[troubleshooting](../reverse-proxy/docs-site/docs/troubleshooting.md#site-404s-after-a-manual-deploy-container-looks-perfectly-healthy)
(hit for real on buywanderbot, 2026-07-17).

Other targets: `make preview` (runs `sonde-preview.sgc.rayandhon.com` alongside
prod), `make logs`, `make stop`.

### Three Traefik routers, not one

Authelia attaches to the *router*, not the path, so anything that must be
reachable without a browser login needs its own.

| Router | Rule | Guard |
|---|---|---|
| main | `Host(sonde…)` | `authelia@file` |
| `-health` | `… && Path(/healthz)`, priority 100 | none — liveness and sync age only |
| `-api` | `… && PathPrefix(/api/v1)`, priority 90 | none from Traefik; a bearer token in the app |

On a single router `/healthz` 302s to the login page and the watchdog reads the
app as down, and the API is unusable by any program.

**The API router's `PathPrefix` is its containment and is the most dangerous
label in `compose.yml`.** Widen it to `/api` and `/api/status` loses Authelia;
drop it and the entire admin surface does. `make verify` asserts the rule, that
the router carries no Authelia, that `/api/v1/meta` answers 401 without a token
(302 there means the router is missing) and that `/api/status` still answers 302.

### First-time server setup

```bash
git clone git@github.com:danhon/sonde.git ~/dev/sonde
cp ~/dev/sonde/.env.example ~/dev/sonde/.env
$EDITOR ~/dev/sonde/.env   # the SERVICE_HOST line here is documentation only;
                           # `make deploy` always overrides it
cd ~/dev/sonde && make deploy
```

Requires the `traefik_default` external network to exist and Authelia to be
running for the `authelia@file` middleware to resolve.

### Verifying a deploy

```bash
docker ps --filter name=sonde-app-1
docker inspect sonde-app-1 --format '{{json .Config.Labels}}' | grep rule
# must show Host(`sonde.sgc.rayandhon.com`) — NOT Host(``)
curl -sk -o /dev/null -w '%{http_code}\n' https://sonde.sgc.rayandhon.com/
# 302 to auth.sgc.rayandhon.com is correct (Authelia); 404 means SERVICE_HOST broke
curl -sk -o /dev/null -w '%{http_code}\n' https://sonde.sgc.rayandhon.com/healthz
# 200 — a 302 here means the health router lost its priority
curl -sk -o /dev/null -w '%{http_code}\n' https://sonde.sgc.rayandhon.com/api/v1/meta
# 401 — 302 means the API router is missing, 200 means the token check is not running
```
## Evals

Unit tests prove the logic is self-consistent; the evals prove it agrees with
Bluesky. They make real API calls, so they're deliberately outside `pytest`.

```bash
uv run pytest                                   # 217 unit tests
uv run python -m evals.live_sweep               # full sweep vs measured baseline
uv run python -m evals.live_sweep --head        # head sweep cost
uv run python -m evals.verified_check           # issuer distribution
uv run python -m evals.score_check --limit 1500 # hydrate, then inspect the ranking
uv run python -m evals.affinity_check           # affinity + institution signal
```

The baselines are the 2026-07-26 measurements. A failure means either the API
changed or sonde did — both worth knowing.
## Snapshots

`follow_events` is the only data here that can't be re-fetched from Bluesky, so a
nightly `VACUUM INTO` writes a timestamped copy to `/backup`, keeping 14. It's a
consistent snapshot of the live database taken without stopping the app.

**It is on-box only, by choice.** It guards against DB corruption or a bad
migration, not against losing the host — off-box backup isn't a priority for this
app. Don't read the snapshots as disaster recovery.
