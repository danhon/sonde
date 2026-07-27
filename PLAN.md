# sonde — build plan

A probe you send up to take measurements. This one measures who follows
[@danhon.com](https://bsky.app/profile/danhon.com) on Bluesky — which of them are
verified, which of them matter, and who arrived or left.

→ **[README.md](README.md)** — what it is and how to run it

---

## Goals

1. **See who my verified followers are.** Not just a count — who, verified by whom, and when.
2. **See who my influential followers are.** With an *explainable* score, not a black box.
3. **See change over time.** Who followed, who left, and how the shape of the audience moves.

Non-goals for v1: posting, moderation, analytics on my own posts, multi-account support.

---

## What the live probe found

Before designing anything I walked the entire follower list against
`public.api.bsky.app` — 115 requests, all 11,451 follow records — and probed the
awkward edges. Every number here is measured on 2026-07-26, not estimated.

| Measurement | Value |
|---|---|
| Followers claimed by `followersCount` | 11,451 |
| Followers actually enumerable | **10,041** |
| API calls for one full sweep | **115** |
| Page sizes at `limit=100` | 50–100, mean 87.3 — only **5 of 115** pages came back full |
| Verified followers | **147** (1.46%) |
| Trusted verifiers among them | **0** |
| Followers labelled `!no-unauthenticated` | **1,838** (18.3%) |
| Followers showing as `handle.invalid` | 37 |
| List ordering | **Newest follow first** (verified — see finding 5) |

### Finding 1 — a short page does not mean the last page

The obvious pagination loop — stop when a page returns fewer than `limit` — would
have terminated on **page 1 of 115** and quietly recorded 90 followers as the
complete list. Pages come back short because the AppView drops deactivated,
suspended, and blocked accounts *after* selecting 100 follow records.

**The cursor is the only end-of-list signal.** Loop until `cursor` is absent.
Nothing else. This is the most load-bearing line in the sync code and it gets the
first test written, with a fixture of short-but-not-final pages.

### Finding 2 — the follower count will never reconcile, and that's correct

10,041 enumerable against 11,451 claimed is a **12.3% permanent gap**: 1,410
accounts still count as follow records while being unservable as profiles. Not
drift, not a bug to fix.

The dashboard therefore shows **both numbers, side by side and labelled** —
"10,041 tracked · 11,451 reported" — with a footnote. Showing one number invites
a recurring "why is this wrong" that has no answer.

### Finding 3 — 18.3% of followers asked not to be shown logged-out

1,838 followers carry `!no-unauthenticated`, set when someone turns off
logged-out visibility. The API returns them to unauthenticated callers anyway;
the convention is that *clients* hide them.

sonde is a single-user tool behind Authelia showing me my own followers, which is
the case the label is least aimed at. The preference still deserves an explicit
answer rather than a silent one: **store them, mark them, show them with a
"private" chip**, controlled by `RESPECT_NO_UNAUTHENTICATED` (default `false`).
Set it `true` and they're counted in totals but withheld from tables and exports.
Either way it's a decision on the record.

### Finding 4 — `getProfiles` silently drops actors it can't resolve

Tested with a batch of two real DIDs and one nonexistent: **HTTP 200, two
profiles, no error.** A batch of one bad DID returns `{"profiles": []}`, also 200.

Two consequences, one of them a latent data-corruption bug:

- **Hydration must map results back by DID, never by array position.** Zipping
  the request list against the response list assigns every profile after the
  first missing actor to the *wrong person* — wrong follower counts, wrong
  scores, silently, with no error anywhere. This is the single most dangerous
  bug available in this codebase and the reason hydration keys on
  `profile["did"]`.
- **The omission is itself the signal.** An actor requested but not returned is
  unservable — deactivated, deleted, suspended, or blocking. That's a cleaner
  "gone" marker than anything error-based, and it's what distinguishes a genuine
  unfollow from an account that vanished. (An earlier draft of this plan claimed
  hydration would return a 400 for these. It does not. That line was wrong.)

### Finding 5 — the list is ordered newest-follow-first

Verified by comparing account-creation dates at both ends of the list:

| | Page 1 | Page 115 |
|---|---|---|
| Oldest account | 2023-03-25 | 2023-03-19 |
| **Newest account** | **2026-07-24** | **2023-08-16** |
| Median | 2024-06-16 | 2023-07-01 |

Page 115 contains no account created after August 2023; page 1 contains one
created two days ago. An account created in 2026 cannot have followed in 2023,
so the ordering is settled.

**This changes the architecture.** Arrivals are all at the head of the list, so
detecting them doesn't need a full sweep — see [the head sweep](#the-head-sweep)
below. It also means `list_rank` recovers the relative arrival order of the
entire backfilled cohort for free.

### Finding 6 — `getKnownFollowers` returns 401 unauthenticated

Confirmed: `AuthMissing`. This is what confines the relevance signal to tier 3
and makes it the only part of the app needing credentials.

---

## Constraints that shape the design

| Constraint | Value | Consequence |
|---|---|---|
| Enumerable followers | 10,041 | Small enough to hold in SQLite and re-sweep entirely |
| Accounts I follow | 4,520 | Cheap to sweep for mutual detection |
| `graph.getFollowers` page size | max 100 (default 50) | 115 calls per full sweep — the cursor advances by records, so short pages don't cost extra |
| `actor.getProfiles` batch size | max 25 | 402 calls to hydrate every follower |
| Rate limit | 3,000 req / 5 min / **IP** | Shared with everything else on ubuntuplex |
| Verification data | On `profileView` | **Free** with the follower list |
| Follower counts | Only on `profileViewDetailed` | **Not free** — this is what tier 1 buys |
| Auth | Not required for tiers 0–2 | v1 stores no Bluesky credentials |

### The rate limit is per-IP, and the IP is shared

BlueBirdNET and atproto-labeler also live on ubuntuplex and talk to atproto from
the same WAN address. The 3,000/5min budget is **house-wide, not per-app**.
Default is therefore **3 req/s** rather than the 5 the arithmetic alone would
allow, and `/settings` displays the observed `RateLimit-Remaining` header so real
contention is visible instead of assumed.

The cap works out to 864,000 requests/day, and sonde's steady state is ~760. The
binding constraint here is politeness to a shared IP, not the limit itself.

---

## Architecture

Standard [python-apps pattern](../reverse-proxy/docs-site/docs/services/python-apps.md):
one container running FastAPI + APScheduler together, SQLite on a named volume,
Traefik labels for routing, Authelia for auth, `make deploy`.

```
FastAPI (web UI)  ─┐
                   ├─ one process, one container
APScheduler (sync)─┘
        │
        ├─→ Bluesky AppView (public.api.bsky.app) via a rate-limited httpx client
        └─→ SQLite (/data/sonde.db, WAL) via aiosqlite
```

### The head sweep

Because the list is newest-first ([finding 5](#finding-5--the-list-is-ordered-newest-follow-first)),
**arrivals and departures have completely different costs** and shouldn't share a
schedule:

- A **new follower is always on page 1.** Finding arrivals costs 1–2 calls.
- A **departure is an absence**, which can only be proven by walking all 115 pages.

So the sweep splits in two. The head sweep pages from the top until it hits a
full page containing no unknown DIDs, then stops — typically 1 page, more during
a burst, and it self-extends naturally if 300 people follow at once. The full
sweep still runs on its own slower cadence for departures and drift correction.

The result is **arrivals detected within 15 minutes instead of 6 hours, for about
2 calls**, while total daily traffic stays roughly flat. It also gives the
optional digest something worth notifying about promptly.

### Sync tiers

| Tier | What | Source | Cost | Cadence | Buys |
|---|---|---|---|---|---|
| **0a** | Head sweep | `graph.getFollowers` | 1–2 calls | **15 min** | Arrivals, fast |
| **0b** | Full sweep | `graph.getFollowers` + `getProfile` (self) | 116 calls | 6h | Departures, drift correction, verification, labels, reported total |
| **1** | Profile hydration | `actor.getProfiles` | 1 call / 25 actors | New at once; everyone else on a 7-day TTL | Follower / follow / post counts |
| **2** | My own follows | `graph.getFollows` | 46 calls | Daily | Mutual flag |
| **3** | Relevance + liveness *(opt-in, auth)* | `graph.getKnownFollowers`, `feed.getAuthorFeed` | 1 call / actor | Top 500, 30-day TTL | Overlap with my network, real last-post date |

| Sync kind | Calls | Wall time at 3 req/s |
|---|---|---|
| Head sweep | 1–2 | <1 s |
| Full sweep | 116 | ~39 s |
| Cold start (everything) | ~564 | ~3 min |
| **Steady state, per day** | **~760** | ~4 min of traffic, spread out |

Tiers 0–2 answer both core questions with no credentials.

### Why tier 3 exists anyway

Follower count answers "who is famous". `getKnownFollowers` answers **"who is
influential in my corner of the network"** — how many of the 4,520 accounts I
follow also follow this person. A 3,000-follower account that 400 of my follows
also follow matters more to me than a 300,000-follower celebrity none of them do.
That is the better signal; it costs an app password and one call per actor.

---

## The influence score

0–100, computed locally, **fully decomposed in the UI**. Any row expands to show
what produced its number. No component the app can't explain in one sentence.

| Component | Weight | Formula | Rationale |
|---|---|---|---|
| **Reach** | 35 | `clamp(log10(followers) / 5, 0, 1)` | Audience size, log-scaled — the distribution is a power law |
| **Relevance** | 25 | `clamp(known_followers / 250, 0, 1)`, tier 3 only | Influence in *my* network rather than in general |
| **Selectivity** | 20 | `clamp(log10(followers / max(follows,1)) / 2, 0, 1)`, **0 below 500 followers** | Separates a 5k account following 200 from one following 5k. Catches follow-back farming |
| **Output** | 10 | Posts/day since account creation, log-scaled | Prolificacy. Replaced by real last-post recency for tier-3 actors |
| **Verification** | 10 | trusted verifier 1.0, verified 0.7, none 0 | Cheap institutional signal, already in hand |

**The selectivity gate matters.** Ungated, an account with 5 followers that
follows 1 scores 0.35 on selectivity — better than a working journalist with 20k
followers who follows 8k. Below 500 followers the ratio is noise, so it
contributes nothing.

**"Output" is deliberately not called liveness.** Posts-per-day since account
creation is a *lifetime average*: an account that posted furiously for a year and
died in 2024 still scores well. That is a real weakness and the honest fix is to
name the metric after what it measures and weight it low. Genuine recency needs
`getAuthorFeed`, one call per actor, which is tier 3 — so only there does this
component become a true liveness signal.

**Relevance carries 25.** It is the most useful signal available, so when tier 3
is on it should outrank everything except raw reach. When tier 3 is off it
contributes 0 and the UI shows scores "of 75" rather than silently penalising
everyone.

`/influential` offers two ranking modes rather than pretending one number serves
both questions: **By reach** (who is big) and **By relevance** (who is big *to
me*, tier 3 only). Weights live in one dict in `sonde/scoring.py`, are shown
read-only on `/settings`, and changing them enqueues a rescore — scores are
denormalised into `actors` for indexed sorting, so they don't recompute
themselves.

**Known limitation, stated up front:** reach and selectivity are gameable and
correlate with account age. This is a sorting aid, not a verdict. The UI says
"ranked by", never "top".

---

## Correctness problems worth solving properly

Where a naive implementation goes wrong, designed rather than discovered.

### 1. Cursor is the only end-of-list signal

See [finding 1](#finding-1--a-short-page-does-not-mean-the-last-page). Loop until
`cursor` is absent. Tested with a fixture of short-but-not-final pages.

### 2. Hydration maps by DID, never by position

See [finding 4](#finding-4--getprofiles-silently-drops-actors-it-cant-resolve).
`getProfiles` returns 200 with fewer profiles than requested. Index-zipping
silently misassigns follower counts to the wrong people. Keyed by
`profile["did"]`, with the requested-but-absent set recorded as unservable.

### 3. A failed sync must never look like a mass unfollow

Absence from the list is how an unfollow is detected, so a sync that dies during
pagination would mark thousands departed.

- Only a **full** sweep that reaches the final cursor may compute departures. The
  head sweep never can — it deliberately doesn't see most of the list.
- A follower is marked lost after **two consecutive complete full sweeps** miss
  them. `missed_syncs` increments on a complete sweep that doesn't see them and
  **resets to 0 the moment they're seen again**, including by a head sweep.
- A sweep that would mark >2% (≈200 people) lost halts, records `needs_review`,
  writes no events, and raises a banner.

The cost is that unfollows take two full sweeps — 12 hours at the default
cadence — to appear. That is the right trade: this is a history tool, and a false
departure corrupts the record permanently while a slow one doesn't.

**The halt needs an escape hatch.** If a genuine mass departure ever happens — a
moderation action, a bot purge — the circuit breaker would trip on every
subsequent sweep and the app would wedge forever. So the `needs_review` banner
carries an **"accept this sweep"** action that clears the flag and applies the
pending departures once, with the override recorded in `sync_runs`.

### 4. Pagination is not a stable snapshot

Someone following mid-sweep shifts the cursor window, so a page can be skipped or
repeated. Upsert by DID makes repeats harmless; the two-sweep rule makes skips
harmless. That rule earns its keep twice.

### 5. Accounts vanish for reasons that aren't unfollows

Deactivation, deletion, suspension, and blocking all remove someone from the list
— that's the 1,410-account gap, and 37 followers already show as
`handle.invalid`. Absence from the list plus absence from a hydration batch means
**"gone"**; absence from the list while still hydrating fine means **"unfollowed"**.
Where neither is certain, the UI says "gone" rather than guessing.

### 6. Handles churn; DIDs don't

Every table keys on DID. Handle changes are recorded as `handle_changed` events
so a renamed follower stays one person with continuous history rather than
appearing to depart and arrive.

### 7. Day one is not 10,041 new followers

The first sync has no history to compare against, so everyone imported is marked
`backfilled` and excluded from arrival counts, the growth chart, and any digest.
Otherwise the timeline opens with a 10,041-person cliff that never happened.

Since the list is newest-first, `list_rank` at import time recovers the
**relative** arrival order of the entire backfilled cohort. Real timestamps would
need each follow record fetched from its author's PDS; relative order is free and
worth keeping.

### 8. Two syncs must not run at once

The scheduler and the manual trigger can collide, and a head sweep can fire
mid-full-sweep. A single-flight asyncio lock per sync kind, with the current job
surfaced in the job registry; a manual trigger while one is running attaches to
the running job rather than queuing a second.

### 9. The history is the only irreplaceable data

Everything else can be re-fetched from Bluesky; `follow_events` cannot. Nightly
`VACUUM INTO` to a timestamped file in the data volume, keeping 14. See
[open question 5](#open-questions) on getting those off-box.

### 10. `/healthz` will 302 unless it gets its own router

`authelia@file` attaches to the router, not the path, so a health endpoint on the
same router redirects to the login page and the watchdog reads it as down. It
needs a second Traefik router with higher priority and no middleware:

```yaml
- "traefik.http.routers.${COMPOSE_PROJECT_NAME}-health.rule=Host(`${SERVICE_HOST}`) && Path(`/healthz`)"
- "traefik.http.routers.${COMPOSE_PROJECT_NAME}-health.entrypoints=websecure"
- "traefik.http.routers.${COMPOSE_PROJECT_NAME}-health.tls.certresolver=le"
- "traefik.http.routers.${COMPOSE_PROJECT_NAME}-health.priority=100"
```

`/healthz` returns only liveness and last-sync age — no follower data — since it
is the one unauthenticated surface.

---

## Database schema

SQLite, WAL, `/data/sonde.db` on a named Docker volume. Base schema in
`db/schema.sql`, migrations applied at startup (house pattern).

### `actors`
Everything known about a person, whether they currently follow me or not.

| column | type | notes |
|---|---|---|
| did | TEXT PK | Stable identity — handles change, DIDs don't |
| handle | TEXT | Current handle; may be `handle.invalid` |
| display_name | TEXT | |
| avatar_url | TEXT | |
| description | TEXT | Searchable |
| account_created_at | DATETIME | From `createdAt` |
| labels | TEXT | JSON — carries `!no-unauthenticated` |
| followers_count | INTEGER | Tier 1 |
| follows_count | INTEGER | Tier 1 |
| posts_count | INTEGER | Tier 1 |
| verified_status | TEXT | `valid` / `invalid` / `none` |
| trusted_verifier_status | TEXT | `valid` / `invalid` / `none` |
| verifications | TEXT | JSON array of `verificationView` — issuer DID, handle, URI, `createdAt` |
| known_followers_count | INTEGER | Tier 3, nullable |
| last_post_at | DATETIME | Tier 3, nullable |
| profile_fetched_at | DATETIME | Tier-1 TTL |
| unservable_since | DATETIME | Requested from `getProfiles` but not returned — the "gone" marker |
| tier3_fetched_at | DATETIME | Tier-3 TTL |
| influence_score | REAL | Denormalised for indexed sorting; rebuilt by the rescore job |
| score_components | TEXT | JSON — feeds the UI breakdown |

Indexes: `influence_score DESC`, `followers_count DESC`, `verified_status`, `handle`.

### `follower_state`
| column | type | notes |
|---|---|---|
| did | TEXT PK, FK | |
| is_current | BOOLEAN | Follows me right now |
| backfilled | BOOLEAN | Imported on first sync — excluded from arrival stats |
| first_seen_at | DATETIME | |
| last_seen_at | DATETIME | |
| missed_syncs | INTEGER | Consecutive complete full sweeps missing them; resets to 0 on any sighting; 2 → departed |
| lost_at | DATETIME | Nullable |
| list_rank | INTEGER | Position in the newest-first list — relative arrival order |

### `follow_events`
| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| did | TEXT FK | |
| event | TEXT | `followed` / `departed` / `returned` / `handle_changed` |
| reason | TEXT | `unfollow` / `gone` / `unknown` |
| detail | TEXT | e.g. old → new handle |
| detected_at | DATETIME | |

Append-only. Never pruned. This is the irreplaceable table.

### `my_follows`
| column | type | notes |
|---|---|---|
| did | TEXT PK | |
| last_seen_at | DATETIME | Stale rows → I unfollowed them |

Mutual = present in `my_follows` **and** `follower_state WHERE is_current`.

### `daily_snapshots`
| column | type | notes |
|---|---|---|
| day | DATE PK | |
| followers_tracked | INTEGER | What we can enumerate |
| followers_reported | INTEGER | What `followersCount` claims — the gap is worth trending |
| verified_total / mutuals_total / gained / lost | INTEGER | |

### `sync_runs`
| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| kind | TEXT | `head` / `full` / `hydrate` / `follows` / `tier3` / `rescore` |
| started_at / ended_at | DATETIME | |
| completed | BOOLEAN | Reached the final cursor — gates departure detection |
| status | TEXT | `ok` / `failed` / `needs_review` / `overridden` |
| pages_fetched / actors_seen / new_followers / lost_followers / profiles_hydrated / api_calls | INTEGER | |
| error | TEXT | |

---

## Web UI

FastAPI + Jinja2 + Tailwind (CDN), server-rendered, behind Authelia. Server-side
pagination and sorting throughout — 10k rows is too many to ship to a browser and
too few to justify a JS framework. Every route needs a designed empty state; the
first thing anyone sees after M0 is an app with nothing in it.

| Route | Description |
|---|---|
| `/` | Dashboard — tracked vs reported totals, verified, mutuals, growth chart, recent arrivals/departures, top 10 by influence, last sync, `needs_review` banner |
| `/followers` | The full table. Sort by influence / followers / recency / handle. Filter by verified, mutual, private, min followers, free text over handle + name + bio |
| `/followers/{did}` | Detail — profile, score breakdown, verification records, full event history |
| `/influential` | Leaderboard, **By reach** or **By relevance**, each row decomposed |
| `/verified` | The 147, grouped by issuer. `verifiedStatus: invalid` shown distinctly — a verification record exists but fails validation, which is not the same as unverified |
| `/changes` | Arrival/departure timeline, filterable by event type and date |
| `/settings` | Manual sync, job progress, rate-limit headroom, DB stats, scoring weights, backup status, CSV export |
| `/healthz` | Unauthenticated liveness — needs its own Traefik router, see above |

---

## Milestones

Each ends at something deployable and useful on its own.

**M0 — Scaffold.** `Dockerfile`, `compose.yml` (both routers, `mem_limit: 512m`),
`Makefile`, `.env.example`, `pyproject.toml`, `CLAUDE.md`, `/healthz`. Deploy it
empty; confirm `sonde.sgc.rayandhon.com` 302s to Authelia while `/healthz`
returns 200. Do this *first* — routing gotchas are cheaper against a hello-world.

Two house gotchas to get right at the start, both already documented in
[troubleshooting](../reverse-proxy/docs-site/docs/troubleshooting.md):
`[tool.hatch.build.targets.wheel] include` must list `**/*.sql` and `**/*.html`
or the schema and templates won't be in the image; and `.gitignore` needs
`.env*` followed by `!.env.example`.

**M1 — Sync core.** Rate-limited client, cursor-only pagination, head + full
sweeps, `actors` / `follower_state` / `sync_runs` / `follow_events`, the
integrity rules, single-flight lock, backfill marking, scheduler, manual trigger.
Ends with ~10,041 rows and a bare list route.

**M2 — Verified.** `/verified` with issuer grouping. Zero extra API calls — M1
already stored it. Expect 147. **Answers goal 1.**

**M3 — Influence.** Tier-1 hydration with TTL and DID-keyed mapping, `scoring.py`,
rescore job, `/influential` with per-row breakdown, sortable `/followers`.
**Answers goal 2.**

**M4 — Change over time.** `daily_snapshots`, `/changes`, dashboard growth chart,
`needs_review` banner with its override, nightly backup. **Answers goal 3.**

**M5 — Depth.** Tier-2 mutuals, `/followers/{did}`, search and filters, CSV export.

**M6 — Optional extras.** Independent, none required:
- Tier-3 relevance and real liveness (needs an app password)
- Email digest of notable arrivals via Fastmail SMTP (buywanderbot pattern) —
  worth more now that the head sweep detects arrivals within 15 minutes
- Write the top N to a real Bluesky list (needs write auth)
- RSS feed of notable arrivals

---

## Testing

`pytest` + `pytest-asyncio`, matching the buywanderbot layout. Priority goes to
bugs that are *silent* — every one of these was found by probing the real API,
so the fixtures are real captured responses, not hand-written ones.

- **Pagination** — short-but-not-final pages must not end the loop. The bug the
  probe caught; first test written.
- **Hydration mapping** — a batch of 25 where actors 3 and 17 are missing must
  assign all 23 returned profiles to the right DIDs. Index-zipping must fail this
  test loudly.
- **Diff logic** — the >2% halt and its override, the two-sweep rule,
  `missed_syncs` reset on sighting, returning followers, backfill exclusion,
  head-sweep results never triggering departures. Table-driven over synthetic
  sweep sequences.
- **Head sweep termination** — stops on a full page of known DIDs; extends
  correctly when 300 arrive at once.
- **Rate limiter** — token-bucket refill, 429 with `Retry-After`, backoff.
- **Scoring** — known inputs → known outputs; the selectivity gate at the 500
  boundary; the "relevance absent → out of 75" path.
- **Client parsing** — captured fixtures of the awkward real cases, all of which
  exist in the live data: `handle.invalid` (37), a profile with **no
  `verification` key at all** (the common case — omitted, not `"none"`), a
  `!no-unauthenticated` label, a verified account, and a short page.
- **Routes** — 200 against an empty DB and a seeded one, including empty states.

---

## Open questions

| # | Question | Default if unanswered |
|---|---|---|
| 1 | Head sweep at 15 min and full sweep at 6h — right balance? Full-sweep cadence sets unfollow latency at 2× | 15 min / 6h → arrivals in 15 min, departures in 12h |
| 2 | Show the 1,838 `!no-unauthenticated` followers, or withhold them? | Show, marked "private" |
| 3 | Tier 3 needs an app password on the box. Worth it for the relevance signal? | Off; no credentials stored |
| 4 | Email digest for notable arrivals? More attractive now arrivals are near-real-time | Not built; M6 |
| 5 | Any existing off-box backup for Docker volumes on ubuntuplex? `follow_events` is the one thing here that can't be re-fetched | Nightly `VACUUM INTO` inside the volume — better than nothing, not a real backup |
| 6 | Track a second account too? | Single account; schema is DID-keyed, so additive later |

---

## Verified API facts

Checked live against `public.api.bsky.app` and the `bluesky-social/atproto`
lexicons on 2026-07-26 — measured, not recalled:

- `app.bsky.actor.defs#verificationState` carries `verifiedStatus`,
  `trustedVerifierStatus`, and `verifications[]` of `verificationView`
  (issuer DID, issuer handle, AT-URI, `isValid`, `createdAt`).
- `verification` exists on **`profileViewBasic`, `profileView`, and
  `profileViewDetailed`** — so the follower list carries it. It is **omitted
  entirely** for unverified accounts rather than set to `"none"`.
- `followersCount` / `followsCount` / `postsCount` exist **only** on
  `profileViewDetailed`. This one fact is the entire reason tier 1 exists.
- `graph.getFollowers` returns `profileView`, `limit` max 100 / default 50,
  **newest follow first**. Pages arrive short; the cursor advances by records, so
  a full sweep of 11,451 follow records is exactly 115 calls regardless.
- `actor.getProfiles` takes max 25 actors and **silently omits** ones it can't
  resolve — HTTP 200, short array, no error.
- Tiers 0–2 work **unauthenticated** against `public.api.bsky.app`, the
  CDN-cached host Bluesky asks public clients to use.
- `graph.getKnownFollowers` returns **401 `AuthMissing`** unauthenticated.
- Rate limit: 3,000 requests / 5 min / IP, shared across everything on ubuntuplex.
