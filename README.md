# sonde

A follower tracker for [Bluesky](https://bsky.app). Sweeps who follows
[@danhon.com](https://bsky.app/profile/danhon.com), tracks arrivals and
departures over time, shows which followers are **verified**, and ranks which
ones are **influential** — with a score it can explain.

→ **[PLAN.md](PLAN.md)** — architecture, schema, milestones, open questions

> **Status: not built yet.** This README describes the app as designed. Nothing
> below Configuration works until M0/M1 land — see [Milestones](PLAN.md#milestones).

## What it does

- **Sweeps the follower list** every 6 hours via the public Bluesky AppView — 115 requests, ~38 seconds
- **Shows verified followers** — currently 147 of them — with who verified them and when. This data rides along with the follower list at no extra API cost
- **Ranks influential followers** on an explainable 0–100 score built from reach, selectivity, verification, liveness, and (optionally) overlap with your own network. Every row shows exactly what produced its number
- **Tracks change over time** — arrivals, departures, returns, handle changes, and a daily growth chart, with guards so a failed sync can never masquerade as a mass unfollow
- **Flags mutuals** by cross-referencing your own follow list
- **Needs no credentials.** Everything above runs unauthenticated. An app password is optional and only unlocks the [extras](#optional-extras)

## Why "sonde"

A sonde is an instrument you send up into something you can't observe directly,
to take measurements and radio them back. Same idea.

## What the numbers will look like

Measured against the live API on 2026-07-26, so the app can be checked against
reality on day one:

| | |
|---|---|
| Followers Bluesky reports | 11,451 |
| Followers actually enumerable | **10,041** |
| Verified followers | **147** (1.46%) |
| Followers who asked not to be shown logged-out | 1,838 (18.3%) |
| Followers with broken handle resolution | 37 |

**The two follower numbers will never match, and that's correct.** 1,410
accounts still count as follow records while being unservable as profiles —
deactivated, suspended, deleted, or blocking. sonde shows both numbers side by
side rather than picking one and inviting a permanent "why is this wrong".

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
| `/settings` | Manual sync, job progress, rate-limit headroom, DB stats, scoring weights, backup status, CSV export |
| `/healthz` | Unauthenticated liveness probe |

## How the influence score works

Follower count answers "who is famous", which isn't quite the question. The
score blends five signals, all computed locally:

| Component | Weight | What it captures |
|---|---|---|
| Reach | 35 | Follower count, log-scaled — the distribution is a power law |
| Selectivity | 20 | Followers ÷ following, ignored below 500 followers. Separates a 5k account following 200 from one following 5k |
| Verification | 10 | Trusted verifier > verified > neither |
| Liveness | 15 | Posting rate. A dormant 50k account isn't influential today |
| Relevance | 20 | How many accounts *you* follow also follow them (optional, needs auth) |

The selectivity gate is load-bearing: ungated, an account with 5 followers and 1
follow out-scores a working journalist with 20k followers who follows 8k.

Weights live in one dict in `sonde/scoring.py` and are shown read-only on
`/settings`, so the ranking is always auditable against what produced it.
Changing them enqueues a rescore.

**It's a sorting aid, not a verdict.** Reach and selectivity are both gameable
and both correlate with account age. The UI says "ranked by", not "top".

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
SYNC_INTERVAL_HOURS=6              # also sets unfollow-detection latency: 2 syncs = 12h
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

# Database — set by Dockerfile default; only override if needed
# DB_PATH=/data/sonde.db
TZ=America/Los_Angeles
```

## Running

```bash
# Web UI only — sync manually from /settings
uv run sonde

# Single sync and exit
uv run sonde --once

# Web UI + scheduled syncs (production mode)
uv run sonde --schedule
```

## Local development

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync
cp .env.example .env
$EDITOR .env

uv run pytest
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
[troubleshooting](../reverse-proxy/docs-site/docs/troubleshooting.md#site-404s-after-a-manual-deploy--container-looks-perfectly-healthy)
(hit for real on buywanderbot, 2026-07-17).

Other targets: `make preview` (runs `sonde-preview.sgc.rayandhon.com` alongside
prod), `make logs`, `make stop`.

### Two Traefik routers, not one

`compose.yml` declares a second router for `/healthz` with no Authelia
middleware and a higher priority. Authelia attaches to the *router*, not the
path — on a single router the health endpoint 302s to the login page and the
watchdog reads the app as down. `/healthz` exposes only liveness and last-sync
age, never follower data.

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
```

## Architecture

One container runs the web UI and the scheduler together, as buywanderbot does.

```
sonde/
├── sonde/
│   ├── main.py            # Entry point — --once, --schedule, or web-only
│   ├── config.py          # Settings from .env
│   ├── scheduler.py       # APScheduler — sync every SYNC_INTERVAL_HOURS
│   ├── scoring.py         # Influence score — weights and components in one place
│   ├── jobs.py            # In-memory job progress for the UI
│   ├── api/
│   │   ├── client.py      # httpx + token-bucket rate limiter, 429 backoff
│   │   └── graph.py       # getFollowers / getProfiles / getFollows — cursor-only paging
│   ├── sync/
│   │   ├── followers.py   # Tier 0 — list sweep, diff, departure rules
│   │   ├── profiles.py    # Tier 1 — TTL-driven profile hydration
│   │   ├── mutuals.py     # Tier 2 — my own follow list
│   │   └── enrich.py      # Tier 3 — knownFollowers + last-post (optional, auth)
│   ├── db/
│   │   ├── schema.sql     # Base schema; migrations applied at startup
│   │   └── store.py       # aiosqlite read/write helpers
│   └── web/
│       ├── app.py         # FastAPI application
│       └── templates/     # Jinja2 (Tailwind via CDN)
├── tests/
├── Dockerfile
├── compose.yml            # Two Traefik routers; labels read SERVICE_HOST /
│                          # COMPOSE_PROJECT_NAME from the shell — only `make deploy` sets these
├── Makefile               # deploy / preview / logs / stop
├── .env                   # never committed — see .env.example
├── .env.example
├── pyproject.toml
├── PLAN.md
└── README.md
```

## Technical approach

Bluesky's AppView exposes everything needed through three read-only XRPC
endpoints on the CDN-cached public host, no credentials required. Data is
fetched in **tiers**, because cost and volatility differ by an order of magnitude:

| Tier | Endpoint | Cost | Cadence | Buys |
|---|---|---|---|---|
| 0 | `app.bsky.graph.getFollowers` | 115 calls | 6h | Membership, arrivals, departures, **verification**, labels |
| 1 | `app.bsky.actor.getProfiles` | 402 calls (full) | New immediately, rest on a 7d TTL | Follower/follow/post counts |
| 2 | `app.bsky.graph.getFollows` | 46 calls | Daily | Mutual flag |
| 3 | `app.bsky.graph.getKnownFollowers` | 1 call/actor | Top 500, 30d TTL | Overlap with your own network |

A cold start is ~563 calls (~3 min at 3 req/s); steady state is ~173 calls
(~58 s), about 700 calls/day. The limit is 3,000 requests per 5 minutes **per
IP** — shared with BlueBirdNET and atproto-labeler on the same box, which is why
the default is 3 req/s rather than the 5 the arithmetic alone would allow.

The one non-obvious detail driving all of this: **verification data is attached
to every profile view, including the lightweight ones in the follower list, but
follower counts are not.** So "who is verified" is free with the sweep, while
"who is influential" needs a second pass over every follower in batches of 25.

### Data integrity

Absence from the follower list is how an unfollow is detected, which makes a
half-finished sync dangerous. Four rules keep the history honest:

1. **The cursor is the only end-of-list signal.** Pages come back short — mean
   87.3 of a requested 100, and only 5 of 115 pages full. Stopping when a page
   is under-full would have ended the sweep on page 1 and recorded 90 followers
   as the complete list.
2. Only a sync that reaches the final cursor may compute departures.
3. A follower is marked lost after **two consecutive complete syncs** miss them
   — which also absorbs the page skips that happen when the list shifts during
   pagination. The cost is 12 hours of latency on unfollow detection.
4. A sync that would mark more than 2% of followers lost halts, records
   `needs_review`, and shows a banner instead of writing events.

Departures are labelled "gone" rather than "unfollowed" when the cause can't be
distinguished — deactivation, deletion, suspension, and blocks all look
identical to an unfollow from outside.

Handle changes are recorded as events, not as a departure and an arrival: every
table keys on DID, because handles churn and DIDs don't.

## Optional extras

None are needed for the core questions; all are post-M5 and independent:

- **Tier-3 relevance** — needs an app password. Adds "how many accounts you
  follow also follow this person", a better influence signal for your purposes
  than raw fame
- **Email digest** of notable new followers via Fastmail SMTP (buywanderbot pattern)
- **Bluesky list writing** — push the top N to a real list
- **RSS feed** of notable arrivals

## Backups

`follow_events` is the only data here that can't be re-fetched from Bluesky.
Nightly `VACUUM INTO` writes a timestamped copy into the data volume, keeping
14. That is not an off-box backup — see [open question 5](PLAN.md#open-questions).

## Database schema

See [PLAN.md — Database schema](PLAN.md#database-schema) for full definitions.
In short: `actors` (everything known about a person), `follower_state` (whether
they follow me and since when), `follow_events` (append-only history),
`my_follows` (for mutuals), `daily_snapshots` (growth), `sync_runs` (what each
sync did).
