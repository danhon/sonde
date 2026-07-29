# sonde

A follower tracker for [Bluesky](https://bsky.app). Sweeps who follows
[@danhon.com](https://bsky.app/profile/danhon.com), tracks arrivals and
departures over time, shows which followers are **verified**, and ranks which
ones are **influential** — with a score it can explain.

→ **[PLAN.md](PLAN.md)** — architecture, schema, milestones, open questions

> **Status: M0–M6 built and verified against the live API.** All three goals are
> answered, plus mutuals, detail pages, the affinity index and institutional
> matching. Still to come — M7 (external reputation: Wikidata, Wikipedia, GDELT)
> and M8 (digest, list writing, RSS). See [Milestones](PLAN.md#milestones).
>
> 217 unit tests pass. Live evals against `@danhon.com` reproduce every measured
> number: 115 pages, 10,042 followers, 147 verified across 7 issuers, 2,170
> mutuals, mean page yield 87.3, newest-first ordering confirmed.

## What it does

- **Spots new followers within 15 minutes** for about two API calls, because the follower list is ordered newest-first
- **Sweeps the whole list** every 6 hours — 115 requests, ~39 seconds — to catch departures, which can only be proven by absence
- **Shows verified followers** — currently 147 — with who verified them and when. This data rides along with the follower list at no extra API cost
- **Ranks influential followers** on an explainable 0–100 score built from reach, relevance, selectivity, output, and verification. Every row shows exactly what produced its number
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
| `/api/status` | Live job progress, next scheduled runs, last-sync age (polled by the nav strip) |
| `/healthz` | Unauthenticated liveness probe — also reports running jobs and scheduler state |

Every page carries a live activity strip showing what is running, how far
through it is, and when the next job is due — polling every 3s while a job runs
and every 15s when idle.

## How the influence score works

Follower count answers "who is famous", which isn't quite the question. The
score blends seven signals, all computed locally:

| Component | Weight | What it captures |
|---|---|---|
| Reach | 18 | Follower count, log-scaled — the distribution is a power law |
| Institution | 18 | Where they work — verification issuer, handle domain, bio, Wikipedia |
| Affinity | 16 | How many selective accounts *you* follow also follow them |
| Verified affinity | 13 | How many verified accounts in your network follow them |
| Public profile | 12 | Wikidata notability, Wikipedia pageviews, news volume |
| Selectivity | 11 | Followers ÷ following, ignored below 500 followers |
| Liveness | 7 | Days since last post |
| Verification | 5 | Trusted verifier > verified > neither |

→ **[SCORING.md](SCORING.md)** — the full design: evidence, confidence tiers,
worked examples, and what it deliberately refuses to do.

The short version of why it looks like this. A Bluesky-verified NYT columnist and
a 741k-follower engagement farm have the same reach; this score puts about 61
points between them. Getting there needed four findings from the live data:

- Only **10% of verified followers are verified by their institution** (the rest
  by Bluesky, which says nothing about employer), so affiliation comes mostly
  from bio text — trusted far more when something else corroborates it.
- Counting an account's verified followers globally would cost **7,418 API calls
  for one person**, so sonde reports a network-scoped count and says so.
- Wikidata has a **Bluesky handle property**, so external reputation is one bulk
  query — 10,563 pairs in under six seconds — rather than a lookup per follower.
- Every component with a cheap approximation and an expensive truth **marks which
  one it used** instead of blending them silently.

Weights and the institution table are editable on `/settings`; changing either
enqueues a rescore.

**It's a sorting aid, not a verdict.** Reach and selectivity are gameable, bio
text is self-reported, and all of it correlates with account age. The UI says
"ranked by", not "top".

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

# Database — set by Dockerfile default; only override if needed
# DB_PATH=/data/sonde.db
TZ=America/Los_Angeles
```

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
[troubleshooting](../reverse-proxy/docs-site/docs/troubleshooting.md#site-404s-after-a-manual-deploy-container-looks-perfectly-healthy)
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
│   ├── scheduler.py       # APScheduler — head sweep, full sweep, hydration, backup
│   ├── scoring.py         # Influence score — weights and components in one place
│   ├── jobs.py            # In-memory job progress + single-flight sync locks
│   ├── api/
│   │   ├── client.py      # httpx + token-bucket rate limiter, 429 backoff
│   │   └── graph.py       # getFollowers / getProfiles / getFollows — cursor-only paging
│   ├── sync/
│   │   ├── followers.py   # Head + full sweeps, diff, departure rules
│   │   ├── profiles.py    # Tier 1 — TTL hydration, DID-keyed result mapping
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

Bluesky's AppView exposes everything needed through read-only XRPC endpoints on
the CDN-cached public host, no credentials required. Data is fetched in **tiers**,
because cost and volatility differ by an order of magnitude.

The key insight is that **the follower list is ordered newest-follow-first**
(verified — page 115 contains no account created after Aug 2023, while page 1 has
one created two days ago). So arrivals and departures have completely different
costs and shouldn't share a schedule: a new follower is always on page 1, while a
departure is an *absence* that can only be proven by walking all 115 pages.

| Tier | Endpoint | Cost | Cadence | Buys |
|---|---|---|---|---|
| 0a | `graph.getFollowers` (head) | 1–2 calls | 15 min | Arrivals, fast |
| 0b | `graph.getFollowers` (full) | 116 calls | 6h | Departures, drift, verification, labels |
| 1 | `actor.getProfiles` | 402 calls (full) | New at once, rest on a 7d TTL | Follower / follow / post counts |
| 2 | `graph.getFollows` | 46 calls | Daily | Mutual flag |
| 3 | `graph.getKnownFollowers` | 1 call/actor | Top 500, 30d TTL | Overlap with your own network |

Steady state is about **760 calls a day**. The limit is 3,000 requests per 5
minutes **per IP** — shared with BlueBirdNET and atproto-labeler on the same box,
which is why the default is 3 req/s rather than the 5 the arithmetic allows. The
binding constraint is politeness to a shared IP, not the cap.

The other non-obvious detail: **verification data is attached to every profile
view, including the lightweight ones in the follower list, but follower counts are
not.** So "who is verified" is free with the sweep, while "who is influential"
needs a second pass over every follower in batches of 25.

### Data integrity

Absence from the follower list is how an unfollow is detected, which makes a
half-finished sync dangerous. These rules keep the history honest — each one
exists because probing the real API showed it was needed:

1. **The cursor is the only end-of-list signal.** Pages come back short — mean
   87.3 of a requested 100, only 5 of 115 full. Stopping when a page is
   under-full would have ended the sweep on page 1 and recorded 90 followers as
   the complete list.
2. **Hydration maps results by DID, never by array position.** `getProfiles`
   returns HTTP 200 with unresolvable actors silently omitted, so zipping the
   request list against the response list assigns follower counts to the wrong
   people — no error, anywhere. That omission is also the cleanest available
   signal that an account is gone rather than merely unfollowed.
3. Only a **full** sweep reaching the final cursor may compute departures. The
   head sweep deliberately doesn't see most of the list, so it can never mark
   anyone lost.
4. A follower is marked lost after **two consecutive complete full sweeps** miss
   them — which also absorbs page skips when the list shifts during pagination.
   The cost is 12 hours of latency on unfollow detection.
5. A sweep that would mark more than 2% of followers lost halts, records
   `needs_review`, and shows a banner. The banner carries an **"accept this
   sweep"** override, so a genuine mass departure can't wedge the app forever.

Departures are labelled "gone" rather than "unfollowed" when the cause can't be
distinguished — deactivation, deletion, suspension, and blocks all look identical
to an unfollow from outside.

Handle changes are recorded as events, not as a departure and an arrival: every
table keys on DID, because handles churn and DIDs don't.

## Optional extras

None are needed for the core questions; all are post-M5 and independent:

- **Tier-3 relevance** — needs an app password. Adds "how many accounts you
  follow also follow this person", a better influence signal for your purposes
  than raw fame, plus genuine last-post recency
- **Email digest** of notable new followers via Fastmail SMTP (buywanderbot
  pattern) — more useful now that arrivals surface within 15 minutes
- **Bluesky list writing** — push the top N to a real list
- **RSS feed** of notable arrivals

## Snapshots

`follow_events` is the only data here that can't be re-fetched from Bluesky, so a
nightly `VACUUM INTO` writes a timestamped copy to `/backup`, keeping 14. It's a
consistent snapshot of the live database taken without stopping the app.

**It is on-box only, by choice.** It guards against DB corruption or a bad
migration, not against losing the host — off-box backup isn't a priority for this
app. Don't read the snapshots as disaster recovery.

## Database schema

See [PLAN.md — Database schema](PLAN.md#database-schema) for full definitions.
In short: `actors` (everything known about a person), `follower_state` (whether
they follow me and since when), `follow_events` (append-only history),
`my_follows` (for mutuals), `daily_snapshots` (growth), `sync_runs` (what each
sync did).
