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

## Decisions taken

| Decision | Made | Consequence |
|---|---|---|
| Authelia guards the web UI | 2026-07-26 | Same as every other app on ubuntuplex; `/healthz` needs its own router |
| sonde holds a **Bluesky app password** | 2026-07-26 | Unlocks `getKnownFollowers` for exact affinity and writing to a real Bluesky list. Read-only by default |
| Docker volumes are **not** backed up on ubuntuplex | 2026-07-26 | Noted, and **deprioritised 2026-07-27**: off-box backup is not wanted for now, and Syncthing is not the mechanism. Snapshots stay on-box — see [§9](#9-the-history-is-the-only-irreplaceable-data-and-the-snapshots-are-on-box-only) |

---

## What the live probe found

Before designing anything I walked the entire follower list against
`public.api.bsky.app` — 115 requests, all 11,451 follow records — then probed the
awkward edges and measured the cost of every optional tier. Every number here is
measured on 2026-07-26, not estimated.

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
| List ordering | **Newest follow first** (verified — finding 5) |
| Accounts I follow, reported / enumerable | 4,520 / **4,429** |
| Their `followsCount` | median 658, mean 1,360, p90 2,661, max 193,518 |

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
drift, not a bug to fix. The same pattern appears in my own follows (4,429 of
4,520), so it generalises.

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
detecting them doesn't need a full sweep — see [the head sweep](#the-head-sweep).
It also means `list_rank` recovers the relative arrival order of the entire
backfilled cohort for free.

### Finding 6 — the affinity signal is better computed from public data

`getKnownFollowers(F)` — "how many accounts I follow also follow F" — is the best
available measure of whether a follower matters *in my world* rather than being
famous in general. It returns 401 unauthenticated, and sonde now has an app
password, so it is available. But it costs **one call per follower**, so it can
only ever cover a top-N slice of the 10,041.

The inverse question is public. "Who does each account I follow, follow?" is
`getFollows`, and fetching those lists once builds an inverted index that scores
**every follower at once**. I measured what that costs across my 4,429 follows:

| Coverage | Calls | Wall time at 3 req/s |
|---|---|---|
| All 4,433 accounts I follow | **62,552** | 5.8 hours |
| Every source in the 150–2,000 band | ~19,600 | 1.8 hours |
| **600 band sources (the default)** | **~4,300** | **24 min** |
| 200 sources with the fewest follows | 200 | 1 min — but only **0.8%** coverage |

Full coverage is out — 62k calls on a shared IP is antisocial regardless of the
rate limit. Sources come from a **band** of follow-list sizes instead.

An earlier draft said to take the *most selective* accounts, reasoning that an
account following 200 endorses more meaningfully than one following 50,000. True
per follow — but a pilot showed it collapses in practice: sorting by
fewest-follows selects accounts following 0–15 people, which are cheap precisely
because they endorse almost nobody. Measured, 200 such sources reached **0.8%**
of followers, while 60 mid-band sources reached **14.0%**. The selectivity
insight belongs on the *hit weight*, not the source list. Full detail and the
corrected design in
[SCORING.md](SCORING.md#choosing-index-sources).

**So both get used, for different jobs.** The public index (~4,300 calls monthly)
ranks all 10,041. The authenticated exact count (1,000 calls monthly) refines and
displays the top slice, and doubles as a validation check on the sample — if the
sampled ranking and the exact numbers disagree badly, the sample size is wrong
and the settings page will show it.

The sampled figure is labelled honestly wherever it appears: "weighted overlap
with the 600 accounts sampled for the index", never presented as the true
`knownFollowers` count.

### Finding 7 — liveness is public

`app.bsky.feed.getAuthorFeed` returns 200 unauthenticated, so genuine last-post
recency costs one call per actor with no credential. That gives the score a real
liveness signal for the enriched set rather than a lifetime average.

---

## Constraints that shape the design

| Constraint | Value | Consequence |
|---|---|---|
| Enumerable followers | 10,041 | Small enough to hold in SQLite and re-sweep entirely |
| Accounts I follow | 4,429 | Cheap to sweep for mutuals; the source of the affinity index |
| `graph.getFollowers` page size | max 100 (default 50) | 115 calls per full sweep — the cursor advances by records, so short pages don't cost extra |
| `actor.getProfiles` batch size | max 25 | 402 calls to hydrate every follower |
| Rate limit | 3,000 req / 5 min / **IP** | Shared with everything else on ubuntuplex |
| Verification data | On `profileView` | **Free** with the follower list |
| Follower counts | Only on `profileViewDetailed` | **Not free** — this is what tier 1 buys |

### The rate limit is per-IP, and the IP is shared

BlueBirdNET and atproto-labeler also live on ubuntuplex and talk to atproto from
the same WAN address. The 3,000/5min budget is **house-wide, not per-app**.
Default is therefore **3 req/s** rather than the 5 the arithmetic alone would
allow, and `/settings` displays the observed `RateLimit-Remaining` header so real
contention is visible instead of assumed.

The cap works out to 864,000 requests/day against a steady state of ~917. The
binding constraint is politeness to a shared address, not the limit.

**Worth measuring once credentials are in:** authenticated reads may be accounted
per-DID rather than per-IP. If so, routing the authenticated tier through the
session would relieve contention with the other two apps outright. Treat that as
a hypothesis to check in M6, not a design assumption.

### Credential handling

The app password lives in `.env` on ubuntuplex, gitignored, never logged, and
revocable at any time from `bsky.app/settings/app-passwords`. Sessions refresh via
`com.atproto.server.refreshSession` and are held in memory — a restart simply
re-authenticates.

An app password **cannot** change the account password, email, or handle, and
cannot create further app passwords. It **can** post and follow as me, so the
honest statement of risk is: if the box is compromised, the attacker can post as
me until the password is revoked. `ENABLE_LIST_WRITE` therefore defaults to
`false`, and every authenticated call in tiers 0–3 is a read.

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
        ├─→ Bluesky AppView, authenticated session — tier 3b only
        ├─→ SQLite (/data/sonde.db, WAL) via aiosqlite
        └─→ nightly snapshot → /backup (host bind mount, on-box only)
```

### The head sweep

Because the list is newest-first ([finding 5](#finding-5--the-list-is-ordered-newest-follow-first)),
**arrivals and departures cost wildly different amounts**:

- A **new follower is always on page 1.** Finding arrivals costs 1–2 calls.
- A **departure is an absence**, provable only by walking all 115 pages.

So the sweep splits in two. The head sweep pages from the top until it hits a
full page containing no unknown DIDs, then stops — usually one page, and it
self-extends if three hundred people arrive at once. The full sweep keeps its own
slower cadence for departures and drift correction.

New followers surface **within 15 minutes instead of 6 hours, for about two
calls**, while total daily traffic stays flat.

### Sync tiers

| Tier | What | Source | Cost | Cadence | Buys |
|---|---|---|---|---|---|
| **0a** | Head sweep | `graph.getFollowers` | 1–2 calls | **15 min** | Arrivals, fast |
| **0b** | Full sweep | `graph.getFollowers` + `getProfile` (self) | 116 calls | 6h | Departures, drift, verification, labels, reported total |
| **1** | Profile hydration | `actor.getProfiles` | 1 / 25 actors | New at once; rest on a 7-day TTL | Follower / follow / post counts |
| **2** | My own follows | `graph.getFollows` | 46 calls | Daily | Mutual flag; input to 3a |
| **3a** | Affinity index | `graph.getFollows` × 600 band sources | ~4,300 calls | Monthly | Ranks **all** 10,041 followers |
| **3b** | Exact affinity *(authenticated)* | `graph.getKnownFollowers` | 1 / actor | Top 1,000, monthly | Exact count + validates the 3a sample |
| **3c** | Liveness | `feed.getAuthorFeed` | 1 / actor | Top 1,000, 14-day TTL | Real last-post date |

| Sync kind | Calls | Wall time at 3 req/s |
|---|---|---|
| Head sweep | 1–2 | <1 s |
| Full sweep | 116 | ~39 s |
| Affinity index rebuild | ~4,300 | ~24 min, monthly |
| Cold start (everything) | ~4,150 | ~23 min |
| **Steady state, per day** | **~1,060** | ~6 min of traffic, spread out |

---

## The influence score

0–100, computed locally, **fully decomposed in the UI**. Any row expands to show
what produced its number. No component the app can't explain in one sentence.

→ **Full design, evidence, and worked examples: [SCORING.md](SCORING.md)**

| Component | Weight | Source | Cost |
|---|---|---|---|
| **Reach** | 18 | `followersCount` | Tier 1 |
| **Institution** | 18 | Verification issuer, handle domain, bio text, Wikipedia | **Free** + ~3 calls/institution/month |
| **Affinity** | 16 | Inverted follow-graph index | Tier 3a |
| **Verified affinity** | 13 | Same index, verified sources only | ~2,200 calls/month |
| **Public profile** | 12 | Wikidata, Wikipedia pageviews, news volume | **~1 query/month** + ~45 calls/day |
| **Selectivity** | 11 | `followers ÷ follows`, **0 below 500 followers** | Tier 1 |
| **Liveness** | 7 | Last post date | Tier 3c |
| **Verification** | 5 | `verifiedStatus` | **Free** — rides on the sweep |

Four things drive that shape, all measured rather than assumed:

- **Only 10% of verified followers are verified by an institution** (14 of 147;
  the other 134 by Bluesky itself). So institutional affiliation comes mostly
  from bio text, and its confidence is conditional on corroboration.
- **Global verified-follower counts are unaffordable** — 7,418 calls for a single
  741k-follower account. sonde reports a network-scoped count it can stand behind
  instead, labelled as such.
- **External reputation is a bulk join, not a lookup.** Wikidata property
  `P12361` holds Bluesky handles, so one SPARQL query returns all 10,563 pairs in
  under six seconds — 107 of them followers here. No per-follower network call.
- **Components degrade rather than lie.** Affinity is exact for the top 1,000 and
  sampled below; liveness is real for the top 1,000 and a lifetime average below;
  institution is cryptographic for some and self-reported for most. In every case
  the UI marks which measurement a row used.

### Tier 4 — external reputation

Wikidata, Wikipedia pageviews, GDELT news volume, and self-declared homepages,
all free and licensed for reuse. Everything is either a bulk join or gated on
internal score, so the ~9,000 followers who will never rank are never looked up:
**~45 calls/day, about 5% on top of the Bluesky traffic.** A LinkedIn module
exists but ships **off by default** and is scoped to URLs a follower published in
their own bio — its User Agreement prohibits scraping and the yield is expected
to be negligible. Full design in [SCORING.md](SCORING.md#public-profile--reputation-from-outside-bluesky).

`/influential` offers two ranking modes rather than pretending one number serves
both questions: **By reach** (who is big) and **By affinity** (who is big *to
me*). Weights and the institution table live in `sonde/scoring.py` and the DB,
are editable on `/settings`, and changing either enqueues a rescore — scores are
denormalised into `actors` for indexed sorting, so they don't recompute
themselves.

**Known limitation, stated up front:** reach and selectivity are gameable, bio
text is self-reported, and all of it correlates with account age. This is a
sorting aid, not a verdict. The UI says "ranked by", never "top".

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

- Only a **full** sweep that reaches the final cursor may compute departures. The
  head sweep never can — it deliberately doesn't see most of the list.
- A follower is marked lost after **two consecutive complete full sweeps** miss
  them. `missed_syncs` increments on a complete sweep that doesn't see them and
  **resets to 0 the moment they're seen again**, including by a head sweep.
- A sweep that would mark >2% (≈200 people) lost halts, records `needs_review`,
  writes no events, and raises a banner.

The cost is that unfollows take two full sweeps — 12 hours at the default
cadence. That is the right trade: this is a history tool, and a false departure
corrupts the record permanently while a slow one doesn't.

**The halt needs an escape hatch.** If a genuine mass departure ever happens — a
moderation action, a bot purge — the circuit breaker would trip on every
subsequent sweep and wedge the app forever. So the `needs_review` banner carries
an **"accept this sweep"** action that clears the flag and applies the pending
departures once, with the override recorded in `sync_runs`.

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
need each follow record fetched from its author's PDS; relative order is free.

### 8. Two syncs must not run at once

The scheduler and the manual trigger can collide, and a head sweep can fire
mid-full-sweep. A single-flight asyncio lock per sync kind, with the current job
surfaced in the job registry; a manual trigger while one is running attaches to
the running job rather than queuing a second.

### 9. The history is the only irreplaceable data, and the snapshots are on-box only

Every other table can be re-fetched from Bluesky. `follow_events` cannot — once a
departure is missed, it is gone.

A nightly `VACUUM INTO /backup/sonde-YYYY-MM-DD.db` keeps 14 snapshots. `VACUUM
INTO` rather than a file copy because it takes a consistent snapshot of a live
WAL database without stopping the app.

**Stated plainly: this is not a backup against losing the host.** Docker volumes
on ubuntuplex are not backed up, and `/backup` is a bind mount on that same host.
What the snapshots do protect against is real but narrower — DB corruption, a bad
migration, an accidental delete, or wanting yesterday's state back.

An earlier version of this plan proposed a Syncthing folder to replicate them off
the machine. **That was dropped on 2026-07-27**: off-box backup is not a priority
for this app, and Syncthing is not the mechanism the operator uses for it. The
bind mount is kept anyway — it costs nothing, and it leaves the snapshots
somewhere an external tool could pick them up later without touching this app.

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

`/healthz` returns only liveness, last-sync age, and last-backup age — no
follower data — since it is the one unauthenticated surface.

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
| verifications | TEXT | JSON array of `verificationView` |
| affinity_sampled | INTEGER | Hits in the tier-3a index |
| affinity_exact | INTEGER | Tier 3b `knownFollowers`, nullable |
| last_post_at | DATETIME | Tier 3c, nullable |
| profile_fetched_at | DATETIME | Tier-1 TTL |
| unservable_since | DATETIME | Requested from `getProfiles` but not returned — the "gone" marker |
| enriched_at | DATETIME | Tier 3b/3c TTL |
| influence_score | REAL | Denormalised for indexed sorting; rebuilt by the rescore job |
| score_components | TEXT | JSON — feeds the UI breakdown, including which measurement each component used |

Indexes: `influence_score DESC`, `followers_count DESC`, `verified_status`, `handle`.

### `affinity_sources`
The 1,000 selective accounts whose follow lists build the index. Kept so the
sample is reproducible and its drift visible.

| column | type | notes |
|---|---|---|
| did | TEXT PK | An account I follow |
| follows_count | INTEGER | At selection time — the selectivity criterion |
| pages_fetched | INTEGER | |
| fetched_at | DATETIME | |

### `follower_state`
| column | type | notes |
|---|---|---|
| did | TEXT PK, FK | |
| is_current | BOOLEAN | Follows me right now |
| backfilled | BOOLEAN | Imported on first sync — excluded from arrival stats |
| first_seen_at / last_seen_at | DATETIME | |
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

Append-only. Never pruned. **This is the table the backup exists for.**

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
| followers_tracked / followers_reported | INTEGER | The gap is worth trending |
| verified_total / mutuals_total / gained / lost | INTEGER | |

### `sync_runs`
| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| kind | TEXT | `head` / `full` / `hydrate` / `follows` / `affinity` / `enrich` / `rescore` / `backup` |
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
| `/` | Dashboard — tracked vs reported totals, verified, mutuals, growth chart, recent arrivals/departures, top 10 by influence, last sync, last backup, `needs_review` banner |
| `/followers` | The full table. Sort by influence / followers / recency / handle. Filter by verified, mutual, private, min followers, free text over handle + name + bio |
| `/followers/{did}` | Detail — profile, score breakdown, verification records, full event history |
| `/influential` | Leaderboard, **By reach** or **By affinity**, each row decomposed, sampled vs exact marked |
| `/verified` | The 147, grouped by issuer. `verifiedStatus: invalid` shown distinctly — a verification record exists but fails validation, which is not the same as unverified |
| `/changes` | Arrival/departure timeline, filterable by event type and date |
| `/settings` | Manual sync, job progress, rate-limit headroom, DB stats, scoring weights, affinity sample health, backup status, CSV export |
| `/healthz` | Unauthenticated liveness — needs its own Traefik router, see above |

---

## Milestones

**Status is maintained here as work lands — not reconstructed afterwards.**
Sub-steps for the scoring work are sequenced in
[SCORING.md](SCORING.md#build-order).

| # | Milestone | Status | Notes |
|---|---|---|---|
| M0 | Scaffold — Docker, Traefik, health router | ✅ done | Two routers; `/healthz` unguarded |
| M1 | Sync core — head + full sweeps, integrity rules | ✅ done | Cursor-only pagination; 5 integrity rules |
| M2 | Verified followers by issuer | ✅ done | **Goal 1.** 147 verified, 7 issuers |
| M3 | Influence scoring + hydration | ✅ done | **Goal 2.** Explainable, decomposed per row |
| M4 | Change over time, snapshots | ✅ done | **Goal 3.** On-box snapshots only, by choice |
| M5 | Mutuals, detail pages, settings, CSV | ✅ done | 2,170 mutuals |
| M6 | Affinity index + institutional matching | ✅ done | 24.5% coverage; 56 institution matches |
| M7 | External reputation — Wikidata + Wikipedia | ✅ done | 107 matched, 58 with pageviews. `public_profile` is live |
| M7b | Link signals from bios | ✅ done | GDELT and LinkedIn **dropped on evidence** — see below |
| M8 | Affiliations table, kinds, notes/links | ✅ done | 103 affiliations for 60 people; prose rationale still to come |
| M9 | Auth, hiding, follow dates | ✅ done | Follow dates free via `viewer.followedBy` TIDs |
| M10 | Recent posts | ✅ done | Top 500 + verified automatic; others on demand |
| M11 | Groups | ✅ done | 215 people, 325 memberships, sortable. T5 propagation moved to M15c |
| M12 | Email digest | ✅ done | Daily 14:00 America/Los_Angeles; quiet days silent, broken days always send |
| M15a | Institution slices | ✅ done | Sortable, kind-filtered, former separated |
| M15b | Group discovery + review queue | ✅ done | 25 candidates proposed, none auto-created |
| M15c | Follow-graph propagation | ✅ done | 194 proposals over 11,038 real edges; found Whittaker for privacy |
| M14 | Relationship score | ✅ done | Separate from influence; notifications ~190x cheaper than per-post |
| M16 | Job batches on /settings | ✅ done | Five ordered batches; individual jobs collapsed |
| M17 | Attention scarcity in the relationship score | ✅ done | 388 of 1,500 score; the ratio's top score is this one's zero |
| M18 | Game industry group | ✅ done | 84 members; validation pass killed 18 of the first 105 |
| M19 | Mobile responsiveness | ✅ done | 767px of sideways scroll at 320px, fixed |
| M19b | Nav regression fix | ✅ done | M19 removed the desktop menu entirely; eval was rect-based and missed it |
| M20 | Institutions page fixes | ✅ done | Detail rendered below a 76-row table; name lookup case-sensitive; counts included departed followers |
| M20b | Test isolation | ✅ done | `db`-fixture tests were writing to the real `./sonde.db` |
| M21 | Latent group discovery | ✅ done | 75 clusters proposed; rediscovers Game industry at 67% overlap without being told it exists |
| M22 | Interaction leaderboards | ✅ done | One tab per kind, inbound by default; fixed `by_kind` conflating directions |
| M23 | One-click follow-back | ✅ done | The first write to Bluesky; guarded, reversible, logged |
| M24 | Five visualisations | ✅ done | Found four overflow bugs invisible against an empty DB |
| M25 | Groups become tags | ✅ done | Hand decisions outrank every job. Archive, not delete — seeded slugs resurrect. Digest line killed at 4.8% coverage |
| M26 | Candidate previews | ✅ done | See who a proposal contains before accepting, and pick a subset. Split the matcher out of the applier so preview and accept cannot disagree |
| M27 | Phrase discovery repair | ✅ done | Proposer and matcher disagreed twice over. Dead candidates 30 → 18; 17 of 64 no longer proposed |
| M28 | Merging circles | ✅ done | Overlap table, no duplicate *detector* — the data says name similarity and containment both lie |
| M29 | Circles, not Groups | ✅ done | Visible text and URLs only; 308 redirects keep old links working. Two silent breakages, fixed in M32 |
| M30 | Build status widget | ✅ done | Click the build stamp: how far behind `main` prod is. Private repo, so it needs a token and stays silent without one |
| M31 | Weekly arrivals | ✅ done | Front page shows who found you lately; creation cohorts moved to /followers |
| M32 | Circle toggle chips | ✅ done | One gesture either way, 44px targets, no reload. Found and fixed three bugs — see below |
| M13 | Remaining extras | ⬜ optional | Bluesky list writing, RSS, per-DID rate-limit test |

### M26–M32 — what changed after M25

Seven pieces of work, all driven by using the thing. Recorded here because the
reasons matter more than the diffs.

**M26 — previewing a candidate.** `/circles/discover` asked you to accept or
reject while showing a label and a count, and for the 91 cluster candidates that
label is often "Unnamed community of 92". Each candidate now has a page listing
who it would contain, with a checkbox each, so a cluster that is mostly right
can be taken without its strays. Required splitting `_apply_discovered_group`
into a matcher and a writer — worth doing anyway, since the rule that *proposed*
a candidate and the rule that *populated* it were separate code that could
silently disagree. They did. See M27.

**M27 — phrase discovery repaired.** The preview made it obvious: 30 of 64
undecided phrase candidates matched nobody. Two defects, one cause — the
proposer and the matcher meant different things by "contains this phrase". The
proposer read bios *and posts* while the matcher read bios only, so "data
center" (21 people's posts, nobody's bio) proposed a circle that would have been
created empty. And bigrams are built after stopwords are dropped, so "wrote a
book" yields "wrote book", a string nobody typed and a literal `LIKE` can never
find. `discovery.bigrams` is now the single definition, used by both.
Measured: dead candidates 30 → 18, and 17 of 64 no longer proposed at all. The
remaining 18 are honestly stale — posts are replaced wholesale on each fetch.

**M28 — merging.** Merging folds a source into a target and archives the source,
recording `merged_into`. Hand decisions on the target outrank the merge: someone
deliberately untagged is not resurrected by absorbing a circle they belong to.
The overlap table deliberately detects *nothing*. The live data refuses to
support a duplicate detector: `game-dev` sits 100% inside `game-industry` and is
a real duplicate; `bafta-games-game` sounds identical and shares 21%; `novelists`
is 81% inside `writers` and that is simply true. No threshold separates
duplication from a legitimate subset, so the table shows the numbers and the
judgement stays human.

**M29 — Circles.** Visible text and URLs; tables, Python identifiers and
template filenames still say group. `/groups` 308s to `/circles` carrying its
query string, so bookmarks and links in already-sent digests keep working. The
rename broke two things silently — see BUG-01 in the list below.

**M30 — build status.** Clicking the build stamp says how far behind `main` the
deployed commit is. The repository is private, so it needs `GITHUB_TOKEN` and
degrades to "cannot tell" without one, never to a reassuring "up to date".
Fetched on open, never polled.

**M31 — weekly arrivals.** Read literally, "when your audience joined Bluesky,
last four weeks" is 12 people, because new accounts rarely follow anyone. The
front page shows *arrivals* per rolling week instead; the creation-date history
moved to /followers. Rolling seven-day windows, because a calendar bucket makes
the current week look like a collapse every Monday.

**M32 — toggle chips.** Every circle is a chip: filled if they are in it,
outlined if not, one gesture either way, whole chip a 44px target. Replaced a
10px `×` and a text box you had to type a remembered name into. Also the first
control here that needs JavaScript to feel right — so it degrades: without it
each chip is a plain form and the redirect lands on `#circles` instead of the
top of the page.

### Off-milestone fixes

- **Backups had never once worked in production.** The `/backup` bind mount
  keeps the host directory's ownership and the container runs as uid 10001, so
  every `VACUUM INTO` since deployment failed with "unable to open database".
  Each failure was written to `sync_runs` and read by nobody, while /settings
  said "No snapshot has been taken yet" — which reads as *not yet* rather than
  *never*. Fixed on the host; a failing or stalled backup is now a loud notice
  on the dashboard, and its signature carries the day so dismissing tonight's
  failure does not silence tomorrow's.
- **An open redirect** in the post-write return path. `_safe_back` only treated
  a referer as off-site when it contained `"://"`, so a scheme-relative
  `//evil.example/phish` passed through into a `Location` header. Shipped in M23
  and inherited by five more call sites in M25.
- **The dashboard shows arrivals, not departures.** Departures were 12 of 22
  events, so the first thing the front page said was who had left. They are
  still recorded and still on /changes.

---

## Known bugs

Triaged 2026-07-29 by auditing the codebase rather than recalling it: every
template variable cross-checked against the context its route passes, every
link and form action resolved against the route table (30 targets, all valid),
every route hit against a production snapshot including hostile parameters (no
500s), job wiring and store references checked, and the chip interaction driven
in a real browser.

Everything below is **reproduced, not suspected**. Where a number appears it was
measured against the 2026-07-29 snapshot.

Ranked by what it costs to leave alone.

### P1 — wrong behaviour, silently

**BUG-01 · Typing an archived circle's name on a profile does nothing, and says
nothing.** `_apply_tags` gets `status: "archived"` from `create_group` and
redirects with no message, because a profile has no notice surface — `/circles`
has one, which is why it was never noticed there. The operator believes they
tagged someone and did not. Reproduced: `POST /followers/{did}/tags` with an
archived name returns 303, writes nothing, and the page says nothing.
*Fix:* give the profile the `_notices.html` treatment, or pass the outcome back
as a query parameter the way `/circles` does.

### P2 — misleading, or friction the design meant to remove

**BUG-02 · Batch tagging still bounces you to the top of the list.** M32 fixed
this for profiles by redirecting to `#circles`; the batch bar still redirects to
`#tagged`, and nothing on any page has that id. Confirmed by grep. Less painful
than the profile case only because the bar is `position: sticky`.
*Fix:* give the batch tables an anchor and pass it, exactly as the profile
route now does.

**BUG-03 · Restoring a merged circle leaves it claiming it was merged.**
`archive_group(slug, archived=False)` clears `archived_at` but not
`merged_into`, so a restored circle is live again while still recording that its
people went elsewhere — and they are now in both. Reproduced. Nothing warns
before restoring a merged circle, which is the more useful fix.
*Fix:* clear `merged_into` on restore, and say what restoring will produce.

**BUG-04 · Dead phrase candidates accumulate forever.** Discovery only inserts
and updates; nothing prunes. 18 undecided candidates currently match nobody, and
re-running "Find new circles" will not remove them — the corrected rules stop
them being *proposed again*, but the rows stay. They have to be rejected by
hand.
*Fix:* a job that rejects undecided candidates matching nobody under current
rules. Bounded and auditable, and it must leave decided ones alone.

**BUG-05 · A rule-based candidate preview takes ~1.8 seconds.** Measured; a
cluster preview is 0.01s. Nearly all of it is `group_target_dids` calling
`sweep_candidate_dids`, which reads every current follower's bio (~8,000) and
runs the M18 regex set over them in Python. Pre-existing and invisible inside
`classify_groups`; the preview is the first thing to put it behind a click.
*Fix:* not caching — the preview must agree exactly with what accepting does.
Either precompute sweep matches into a column, or narrow the target set for
preview and prove the two still agree.

### P3 — cosmetic, dead, or preventive

**BUG-06 · Stored `why` text still says "group".** Existing candidates keep the
wording they were written with; only a fresh discovery run rewrites them. Self-
healing, so this is a note rather than work.

**BUG-07 · `create_group` returns the untruncated name** while storing
`name[:64]`. No caller trusts the return value as the persisted one today.

**BUG-08 · Dead code.** The `tag_chip` macro has no callers since M32 replaced
it. `POST /circles/{slug}/{did}/review` became unreachable when M25 removed the
per-row remove button, taking `review_group_member` with it — both still exist
and are still tested.

**BUG-09 · The rigid-flex-row guard only scans `<nav>`.**
`test_the_nav_row_never_reverts_to_a_rigid_flex_row` would have caught the
profile-footer overflow found in M32, but it never looks outside the nav. The
browser eval caught it instead, and only once a profile page was added to its
routes — which took until M32.

**BUG-10 · Job keys still say `groups` in URLs.** `/settings/sync/groups` and
the batch step keys are internal, and the labels shown to a person were fixed in
M29, but the key leaks into a form action.

### Not a bug, but the biggest risk here

**M26 onwards had no independent review.** Twelve commits — previews, the
discovery repair, merging, the rename, bios, the build widget, weekly arrivals
and the chips — were written and reviewed by the same context, after a spend
limit ended subagent review partway through M25. The tests are real and the
load-bearing ones were deliberately falsified to check they fail. That is not
the same as another reader. Three bugs in that stretch reached `main` before
being caught here, two of them user-visible for several commits. A whole-branch
review over `661cf68..HEAD` is the single highest-value thing left.

### Detail on what is done

**M0** `Dockerfile`, `compose.yml` (both routers, backup bind mount,
`mem_limit: 512m`), `Makefile`, `.env.example`, `pyproject.toml`, `CLAUDE.md`,
`/healthz`. Two house gotchas handled up front: hatchling `include` patterns for
`**/*.sql` and `**/*.html`, and `.gitignore` with `.env*` plus `!.env.example`.

**M1** Rate-limited client, cursor-only pagination, head + full sweeps,
`actors` / `follower_state` / `sync_runs` / `follow_events`, the integrity
rules, single-flight lock, backfill marking, scheduler, manual trigger.

**M2** `/verified` grouped by issuer. Zero extra API calls — M1 already stored
it. 147 verified, 14 institutional, 0 trusted verifiers.

**M3** Tier-1 hydration with TTL and DID-keyed mapping, `scoring.py`, rescore
job, `/influential` with per-row breakdown. Components with no data corpus-wide
are excluded from every denominator equally, so scores stay comparable.

**M4** `daily_snapshots`, `/changes`, growth chart, `needs_review` banner with
its override, nightly `VACUUM INTO` to the bind mount (on-box only — see §9).

**M5** Tier-2 mutuals, `/followers/{did}`, sortable/filterable table, `/settings`,
CSV export.

**M6** Institution matching (attested / domain / roster / claimed, with
past-employment, product and consumption rejection), issuer auto-discovery,
rosters via `listRecords`, the affinity index over band-selected sources, and
the verified-affinity index.

**M9** Session auth degrading to public paths, `ignored_at` hiding with
`ignore_locked` so human decisions outrank automation, skywatch.blue moderation
lists, and exact follow dates decoded from `viewer.followedBy` TIDs.

**M10** Three recent posts for the top 500 by influence plus every verified
follower; everyone else on demand from their page. Retires the lifetime-average
liveness proxy for accounts covered.

**M7** Wikidata joined in bulk on property `P12361` (Bluesky handle) — the
whole mapping in one query, then a local join, so there is no per-follower cost.
Split into two queries after the combined form returned a 504: three OPTIONAL
joins with GROUP_CONCAT over 10,536 entities is too much for the public
endpoint, so detail is requested only for the ~1% who are actually followers.
107 matched, with occupation, employer and position; 58 have Wikipedia pageview
counts. This activated `public_profile`, which had been contributing nothing
because no data existed for it.

**M7b** Signals derived from the links people put in their own bios — the most
consented source available, and free, since the text is already stored. 1,248
followers carry one: 60 newsletter platforms, 38 code hosts, 23 academic
domains, 27 LinkedIn profiles, plus self-declared personal domains. Feeds M11
grouping directly.

**Two of the three planned components were dropped after measuring them**, which
is recorded here rather than quietly abandoned:

- *LinkedIn:* **zero** of the 564 accounts in the enrichment set have a LinkedIn
  URL in their bio. The module was scoped to self-declared URLs only, so it
  could never have fired. Building it would have been ceremony.
- *GDELT:* returned **no data at all** for Naomi Klein and Jeff VanderMeer — the
  two most notable people in the set — while Wikipedia pageviews handled both
  (12,700 views for VanderMeer). An unreliable signal that duplicates a reliable
  one is not worth the calls.
- *Homepage fetching:* only 121 of 564 have any bio URL, and the commonest hosts
  are aggregators (linktr.ee, 16) and shorteners rather than personal sites with
  structured data. The **host itself** carries the signal, so it is read for
  free instead of fetched.

**M8** Affiliations are now a table with a *kind*, because the single
`institution_*` column set could not express more than one relationship at a
time. Leadership counts for more than employment, a former role for much less,
and an own publication is a claim no employer lookup would ever find. Evidence
comes from atproto (attested / domain / roster / bio claim), Wikidata employers
and positions, and self-declared platform links; best evidence wins rather than
accumulating. Organisation weight derives from Wikidata notability, so Signal
scores without anyone adding Signal, and a human-set weight is never moved by a
later pass. Measured on the real enrichment set: 103 affiliations across 60
people — 59 Wikidata employments, 12 attested, 12 own publications, 7
leadership, 2 academic.

**M11** Overlapping groups over the top 500 plus every verified follower, from
data already stored — no new API calls. 215 people in 325 memberships: 108
writers, 56 academics, 51 journalists, 26 newsletter writers, 26 technologists,
24 developers. Evidence is tiered (affiliation, Wikidata occupation, link
domain, bio and post text) and each membership records which tier decided it,
so a wrong answer can be argued with rather than only deleted. Removing someone
sticks across reclassification.

Two accuracy bugs found by checking real people rather than trusting the counts:
a *former* affiliation was conferring membership, which put Meredith Whittaker
in a Google group six years after she left; and occupation matching was exact,
so Wikidata's "artificial intelligence researcher" and "site reliability
engineer" matched nothing at all. T5 follow-graph propagation is still
outstanding — it is what should find civic tech and privacy people, who are
currently only caught by bio text.

**M15a/b** `/institutions` slices the enrichment set by organisation — Wired 6,
NYT 5, Washington Post 4 — with current and former shown separately and
organisation weight editable and locking. Discovery proposes groups nobody
named: 25 candidates from Wikidata occupations no group claims (blogger 9),
link kinds with nowhere to land (organisation 22), organisations big enough to
be groups, and bio phrases (human rights 9, trans rights 4). Nothing is created
automatically — accepting a candidate builds the group and fills it using the
rule that proposed it, and a rejection is permanent.

Phrase extraction needed two fixes found by looking at the first output: bio
URLs dominated the results ("Bsky Social", "App Profile", "Mastodon Social"),
and bigrams spanned punctuation, inventing "rights human" from "digital rights,
human dignity".

**M15c** Propagation over the real follow graph — 11,038 edges from 187 sources.
The naive form failed instructively: sources that follow many of a group's
members proposed 329 memberships, with 25 people in five or more groups and one
in all seven. Every index source comes from one person's follow graph, so the
accounts following journalists also follow developers and academics. A source
now has to follow a group *disproportionately* (lift against the baseline rate),
and nobody is proposed for more than their two best-matching groups. 194
proposals across 113 people, and it surfaces what rules cannot: Meredith
Whittaker for privacy, which no bio rule caught.

**M14** A relationship score, kept deliberately separate from influence:
influence asks whether someone matters, this asks whether we know each other.
Built from `listNotifications` (inbound) and my own author feed (outbound),
which is ~190x cheaper than reconstructing the same thing by walking 23,602
posts. Interactions are stored append-only as observed, because the
notification retention window is finite and undocumented — so the score
improves the longer it runs.

Weighted by what an interaction costs the giver, not by volume: a reply is worth
ten likes, a thread we both posted in more than once carries a bonus, everything
decays, reciprocity multiplies rather than adds, and interacting across many
separate days beats one argument.

**M16** The settings page had grown to sixteen buttons, several of which only
work in sequence — enrichment needs hydrated profiles, grouping needs
enrichment, propagation needs a fresh affinity index. That ordering lived in my
head and in release notes, which is the wrong place for it. Five batches now
encode it (refresh followers → enrich profiles → rebuild groups, plus
relationships and housekeeping), running their steps in order and stopping at
the first failure. Individual jobs stay behind a disclosure for the four that
sit outside a batch and for when you want exactly one thing.

**M12** Daily digest at 14:00 `America/Los_Angeles`, pinned to a real timezone
so it does not drift an hour twice a year with daylight saving — verified to
hold 14:00 local across both PDT and PST. Arrivals are ranked by influence
rather than arrival order. A quiet day sends nothing, because a daily "nothing
happened" email trains you to ignore the one that matters; but health problems
send regardless, because silence is otherwise ambiguous between "nothing
happened" and "the app died". Reports stale sweeps, held sweeps, failed runs and
an app password that is set but not authenticating.

### M19 — Mobile responsiveness

Requested 2026-07-27: the top menu does not work on a phone, and it is "one of a
few things".

**Audited every template before planning.** The damage is narrower than the
symptom suggests — the filter forms and the settings batches already carry
`flex-wrap`, and every `grid-cols-N` already has a 1- or 2-column base. Three
real defects:

1. **The nav, which is the reported bug.** Ten links plus the actor handle and
   build SHA, in a `flex` row with **no `flex-wrap`**. Text cannot compress
   below its intrinsic width, so the row overflows the viewport — and because
   nothing clips it, *the whole page* scrolls sideways. That is why several
   unrelated things feel broken: they are all being pushed off-screen by the
   nav, not broken themselves.
2. **Six tables with no `overflow-x-auto` wrapper** — `detail.html` ×4,
   `influential.html`, `institutions.html`. A wide table inside a wrapper
   scrolls itself; without one it widens the page. Twelve other tables are
   already wrapped, so this is drift from an established pattern rather than a
   missing decision.
3. **Tap targets.** Nav links and the sortable `<th>` arrows are `text-xs` and
   `text-[10px]` with no vertical padding — well under the ~44px that is
   reliably tappable.

**No `overflow-x: hidden` on the body.** It would make every one of these
invisible rather than fixed, and would defeat the test below by clipping the
evidence. The page must not overflow, not be prevented from showing that it did.

**The nav becomes a disclosure.** A native `<details>` "Menu" button below
`sm:`, expanding to full-width stacked links; the horizontal row returns at
`sm:` and up. No JavaScript, consistent with the rest of the app, and it keeps
chrome to one line on a phone — where `flex-wrap` alone would put three rows of
uppercase links above every page. The actor handle and build SHA are
`hidden sm:inline`: useful, not worth a row on a 320px screen.

**Testing, in two layers**, matching the split the repo already uses between
fast fakes in `tests/` and real-world checks in `evals/`:

- `tests/test_responsive.py` — renders every template with a realistic context
  and asserts structural invariants: every `<table>` sits inside an
  `overflow-x-auto` container, the nav collapses below `sm:`, no `grid-cols-N`
  ≥3 without a mobile fallback, the viewport meta is present. Fast, runs in the
  normal suite, and its job is catching regressions rather than proving
  correctness.
- `evals/mobile_check.py` — Playwright at **320 / 375 / 390 / 768**, walking
  every route and asserting `documentElement.scrollWidth <= innerWidth`. This
  is the only layer that can actually prove a page does not scroll sideways;
  the static test cannot, and saying so is the point of having both. Also
  measures nav tap-target heights. Run on demand, like the live-API evals.

**Acceptance:** no route scrolls horizontally at 320px, and every nav target is
at least 44px tall.

#### Measured, before and after

The eval was run against the pre-M19 nav to confirm it reproduces the report,
and it does — on **every page**:

    320px /followers: scrolls 767px sideways
      widest is span.ml-auto.font-mono.text-xs reaching 1087px

767px of overflow on a 320px screen, caused by one nav row. That is the whole
reported symptom, and it explains why unrelated pages felt broken: they were
being dragged off-screen by shared chrome, not broken themselves.

After: **56 page renders across 4 viewports, zero overflow, every nav target
≥44px.** The static suite is 45 checks, all of which were mutation-tested —
stripping a single `overflow-x-auto` from any of eight templates fails the run,
and reverting the nav fails it too.

One test defect found by that mutation pass and worth recording: the first
version of the table check looked for `overflow-x-auto` within the preceding 500
characters, which a *previous* table's wrapper satisfied. Deleting a real
wrapper left the suite green. It now walks the actual ancestor stack, and
`test_the_table_check_actually_detects_a_missing_wrapper` guards the guard.

The eval also found `/groups/discover` — the route list in the first draft said
`/discover`, which 404s. A test that checks the wrong URL passes for the wrong
reason.

### M22 — Who replies, reposts, quotes and likes the most

Requested 2026-07-28: rank followers by each interaction type separately.

The data is already there — `interactions` stores `did`, `direction`, `kind`,
`occurred_at` per event — so this is a reading problem, not a collection one.
Three things have to be right or the numbers mislead.

**Direction is the whole question.** "Who replies to me most" is *inbound*
replies. `Relationship.by_kind` today counts both directions into one bucket,
so a person I reply to constantly and who never answers looks identical to one
who replies to me constantly. That is a defect for this feature and a
misleading number on the profile page already. Counts become
`{kind: {inbound, outbound}}`, and the leaderboards default to inbound because
that is what was asked for, with a toggle for the other direction.

**Kinds are not comparable, so they never share a ranking.** A like costs
nothing and a reply costs real attention; a combined "interactions" table would
be a like-count leaderboard wearing a disguise. One tab per kind, each ranked on
its own — which is also exactly what was asked for.

**A count is only as old as the observation window.** `listNotifications` has a
finite, undocumented retention window, so these are counts of what sonde has
*seen*, not what happened. If it has been running a fortnight, "most replies"
means "most replies this fortnight". Every leaderboard states the window it is
drawn from — earliest event held, and total events — rather than presenting a
partial count as a total. This is the same discipline as showing 10,041 tracked
against 11,451 reported.

Not a new nav entry: the top row already needs 1,280px and adding a tenth link
would push the breakpoint further. It hangs off `/relationships`, which is where
someone looking for "who talks to me" will already be.

**Verification limit, stated plainly:** the interaction table is empty in every
local snapshot because it needs the app password, so this was built and tested
against synthetic data. The shapes are asserted; the volumes are not.

#### Built

`/interactions`, one tab per kind, each with a **Back** column — the same
interaction in the other direction, which is what distinguishes a correspondent
from an audience — plus direction and 30d/90d/1y window filters. The observation
window is printed under every table: how many events, how many accounts, and the
first and last dates they span.

The defect this turned up: `Relationship.by_kind` counted both directions into
one bucket, so the profile page could not tell somebody who replies to you
constantly from somebody you reply to who never answers. Counts are now
`{kind: {inbound, outbound}}`, and the profile shows the full grid.

### M21 — Latent groups

Requested 2026-07-28: the thirteen groups were supplied by hand; find the ones
nobody thought to name. Free sources only.

**The current discovery looks in the wrong place.** M15b ranks *vocabulary by
frequency* — bigrams and occupations that appear often. On the live data that
produced "Send Tips", "Personal Account", "Dog Lover", "Born Ppm" and "Rights
Human" (a mirror-pair bug fixed in code but still sitting in stored rows). Of 25
candidates, the only sound ones are organisations — Wired, NYT, WaPo — and those
already have a page at `/institutions`. **A phrase being common is not evidence
that a community exists.** Boilerplate is the most common text of all.

**The asset is the follow graph, and it is barely used.** 11,038 edges from 187
sources are already on disk, collected for the affinity score and read for
nothing else. A community is a shape in that graph, not a word in a bio.

**Prototyped end to end before planning.** Follower×follower cosine similarity
over IDF-weighted shared sources, a symmetric k-nearest-neighbour graph at K=5,
then label propagation:

| | |
|---|---|
| Placeable followers (≥3 sources) | 967 |
| Clusters of 4–120 | **79** |
| People placed in one | **927 of 967** |
| Largest cluster | 67 |

**It rediscovers a known-true group unprompted.** A 25-person cluster names
itself *narrative / game / games / designer* — the game industry, which M18
built independently from bio rules. Two methods with no shared inputs agreeing
is the strongest validation available here.

And it finds groups nobody seeded: **law professors** (37), **futures and
foresight** (14), **data journalism** (17, distinct from journalists), **comics**
(7), **science + queer** (8).

**Two failure modes, measured, recorded so they are not reintroduced:**

1. **Label propagation on a global top-weight edge list collapses.** The first
   run produced one 465-node hairball, a 67 and a 5 — useless. The kNN
   construction is what makes it work, because it stops hub followers from
   swallowing the graph. Not a tuning detail.
2. **Naming fails where clustering succeeds.** About 20 of 58 clusters have no
   distinctive vocabulary at all. The cluster may be real and the label absent.

**So naming gets its own tiers**, cheapest and most authoritative first:

| Tier | Source | Have it? |
|---|---|---|
| N1 | resolved affiliations and institutions of members | yes |
| N2 | Wikidata occupation, plus `P101` field of work and `P463` member of | 99 of 967 |
| N3 | link-signal kinds and hosts | 1,248 actors |
| N4 | bio and post vocabulary **by lift**, never frequency | yes |
| N5 | human-authored names — starter packs containing members | see below |

**Free external sources, assessed rather than assumed:**

- **Wikidata SPARQL** — already wired, extend the property set. Free, no key.
- **Wikipedia categories** — free API; "American science fiction writers" is a
  ready-made group name. Only the 107 matched actors.
- **Bluesky starter packs** — `getActorStarterPacks` verified working
  unauthenticated today. Human-curated *and* human-named: Molly White publishes
  "Indie publications" and "Independent writers". But measured hit rate is low —
  4 packs across 5 probes, 3 of them from one person — so this is a naming aid
  and a supplementary source, not a primary one. ~3 minutes for the top 500 plus
  verified; 56 minutes for all 10,042, which is not worth it yet.
- **Considered and rejected**: GitHub (60 unauthenticated requests/hour cannot
  cover 1,248 link holders), OpenAlex (academics only), anything paid.

**Coverage is the binding constraint, and the cheapest fix.** Only 967 of 10,042
followers carry ≥3 affinity sources, because only 187 of the configured 600
sources have been indexed. Finishing that is ~4,300 calls, about 24 minutes at
3 req/s, and should roughly triple the placeable population before any of this
is tuned further. **Do that first.**

**Guardrails, all learned the hard way earlier in this build:**

- Nothing auto-creates a group. Candidates are proposed for review, as M15b and
  M15c already do — a group nobody wanted makes every other count untrustworthy.
- Lift, never raw frequency (M15c proposed 329 memberships before this rule).
- Cap a person's clusters, as propagation caps at two groups.
- `log()` what was dropped: silent truncation reads as full coverage.

**Acceptance:** hand-check a sample of proposed clusters and report precision
before any of them can create memberships, exactly as M18 did — that pass killed
18 of the first 105 memberships and is the reason the group is trustworthy.

#### Built

`sonde/clustering.py` holds the algorithm as pure functions, so the graph
behaviour is testable without a database. `store.discover_latent_groups()` reads
`affinity_edges`, clusters, names, and writes `group_candidates` rows of kind
`cluster`. Registered as the `latent` job inside the **Rebuild groups** batch,
between `discover` and `propagate`.

A cluster candidate is unlike every other kind: there is no term to re-run, so
the member list *is* the proposal and is stored as JSON alongside it. Accepting
one inserts exactly those members.

**Each proposal says which existing group it most resembles.** That is not a
filter — a cluster matching a hand-built group is the strongest evidence the
method works, and on real data the game industry came back at **67% overlap
without the clusterer being told it exists**. Academics (43%), Journalists
(38%) and Writers also reappear. What is left over is the interesting part:
law professors (35), product design (30), a technology-journalism cluster (17),
cybersecurity-and-law (14).

Run against the 11,038-edge index: **75 clusters proposed, 966 placeable
followers of 2,462 reached.** Naming tiers: 35 text, 4 Wikidata occupation, 1
link, and **35 unnamed** — which are proposed anyway, because a real community
with no label is worth reviewing and a wrong label is worse than none.

Three defects the tests and the real-data runs caught, all fixed:

- **The share ceiling zeroed out small graphs.** Expressed only as a fraction,
  16 placeable followers gave a ceiling of 6 while every source had 8 members,
  so every source was discarded and the graph came back empty. It now has a
  floor as well as a share.
- **Bio links became vocabulary.** Communities were labelled "Org" and
  "Games · Linktr · Game" straight out of `linktr.ee` and `.org` URLs. URLs are
  stripped before tokenising, and boilerplate — "account", "personal",
  "senior", "guy" — is refused as a label at both tokenising and scoring.
- **Display names contributed surnames.** A cluster came back as
  "White · Queer · Science", where "white" was somebody's name. Naming reads
  bios only; a person's name describes no group.

### M20 — Institutions

Reported: clicking an organisation "simply leads to the institution index page",
and the numbers look off.

**The click did work.** The detail section rendered *after* the 76-row index
table, so the browser reloaded at scroll-top showing an identical page with the
answer far below the fold. It now renders above the table, with the member
count, a `clear` link, and a rule down the side so it cannot be mistaken for the
index. Nothing had ever tested the detail path.

**`?name=eventbrite` really did return nothing.** The lookup was
`org_name = ?`, case-sensitive, against a stored `Eventbrite`. Names are
identifiers here, not data, so both lookups are now `COLLATE NOCASE` and the
page shows the stored capitalisation. An unknown name now says so instead of
rendering an empty shell that looks like a loading failure.

**The counts included people who are no longer followers.** Neither the index
nor the detail filtered on `follower_state`, so a departed or hidden follower
kept padding a roster. Both do now, and a test asserts the two agree — they were
computed by different keys, the index by `org_id` and the detail by `org_name`,
which is exactly how a row can claim six people and its page show none.

Still open: Wikidata employers with no end date read as current. Simon Willison
is listed at Eventbrite, which he left years ago. That is the same defect M8
fixed for group membership, not yet applied to the institutions roster.

### M20b — tests were writing to the real database

Found while adding the tests above, and worth more than the feature.

`_db()` calls `connect()` with no argument, which resolves
`_override_path or settings.db_path`. The `db` fixture passed its tmp path
*positionally*, which set `_db_path` but not `_override_path` — so the first
`_db()` inside any test saw a different target, closed the tmp database, and
reopened `./sonde.db`. Every fixture-based test shared one file, tests collided
with each other through it, and nine rows of fixtures were sitting in the
working copy.

`connect(path)` now remembers an explicit path as the override, the fixture
clears it, and an autouse fixture fails any test that ends up connected to the
configured production database.

### M19b — the nav regression, and why the eval missed it

M19 shipped a nav that **removed the desktop menu**. Reported against desktop
Safari; it reproduced identically in Chromium, so it was never Safari-specific.

The cause: one shared markup block — a `<details>` whose panel was forced
visible at wide widths with `display: flex`. **A closed `<details>` does not
paint its slotted content, whatever `display` the child is given.** The links
were in the DOM with real bounding boxes and were simply never drawn; the nav
collapsed from 95px to 35px.

**Why the eval passed it.** Every check was rect-based — `scrollWidth`,
`getBoundingClientRect().height` — and a laid-out-but-unpainted element has a
perfectly good rect. It reported ten visible links on a page showing none. The
fix is `checkVisibility({checkVisibilityCSS: true})`, which asks whether the
element is actually painted. Everything in the eval now uses it.

Three further corrections, all found by re-running against the broken build
rather than by reasoning:

- **Desktop widths were not being tested at all.** The viewport list stopped at
  768. A *desktop* regression against a mobile-only eval was never going to be
  caught. Now 320–1440, seven widths, two engines, 196 renders.
- **The reachability check hardcoded the breakpoint** — "≥768px means a full
  row is showing" — which is a guess about the CSS, not a question about the
  app. It broke the moment the split moved. It now asks the same thing at every
  width: is every section visible, or reachable by opening a menu?
- **A blind `click()` on the menu hung for 30 seconds** instead of reporting
  that the button was itself invisible. It checks `is_visible` first.

**The fix.** Two separate elements, neither overriding the other's native
behaviour: the pre-M19 row at `xl:` and up, a real disclosure below it. The
split is at `xl:`, not `sm:`, because measurement says the row needs 1,280px —
below that it overflowed its own container by 121–505px, which **it also did
before M19**. At ≥1280 the markup is byte-identical to the nav that worked.

Two attempts to improve the row while fixing it were measured and discarded:
`flex-wrap` plus `ml-auto` sends the actor span to a second line, giving 115px
of nav against the original 95.

Verified both directions: the eval now reports 196 failures against the shipped
build ("9 sections not visible and no usable menu to reach them" at 1024, 1280
and 1440) and zero against the fix. The static suite gained two nav checks,
which also fail against the shipped build.

### M18 — Game industry

Requested 2026-07-27, with [Cat Manning](https://bsky.app/profile/catacalypto.bsky.social)
— "narrative director at Firaxis" — as the worked example.

Adding a thirteenth entry to `GROUPS` is a five-line change. It would also find
**eleven people and miss a hundred and eighty-seven**, so almost none of this
plan is about the group definition.

**Finding 1 — the target set is the problem, not the rules.** Groups run over
the top 500 by influence plus every verified follower: 559 people. Across the
full 10,041 followers, 198 bios carry a game-industry term. Only **11 of those
198 (6%) are inside the target set.** Cat Manning is not: influence 10.4 against
a cutoff of 15.9, unverified, never hydrated.

That is not bad luck. Game industry people are not prominent the way journalists
and academics are — a senior designer at a major studio has a few thousand
followers, and every component of the influence score reads reach. Any group
whose members are systematically less famous than the corpus average is invisible
to a top-N target set, so this is a **general defect** that game developers
happen to expose. Other likely victims: civic tech, librarians, translators.

The fix is general, not a game-specific escape hatch: extend the target set to
`top 500 ∪ verified ∪ anyone carrying strong (T1–T3) group evidence`. Detection
costs nothing — 7,974 of 10,041 followers already have a `description` on disk,
because `getFollowers` returns it with the sweep. No new API calls to *find*
anyone; only ranking them needs hydration.

**Finding 2 — fans outnumber practitioners two to one, so precision is the
whole job.** Splitting the matches by what they actually claim:

| Signal | People | Example |
|---|---|---|
| Studio named | 14 | "principal ux @ Riot Games" |
| Role stated | 70 | "Senior Writer @ Failbetter Games" |
| Tool / platform | 8 | an `itch.io` link |
| **Plays games only** | **164** | "I read a lot. I play videogames." |

"Gamer", "video games", "board game", "TTRPG" and "roguelike" are *fandom*.
164 people would join a game-industry group on those terms and none of them
belong. **Making games is the criterion; playing them is not.** Fan vocabulary
is excluded outright rather than given a low tier, because at 164-vs-92 even a
weak tier would make the group majority-wrong.

**Finding 3 — `unity` is a trap.** Fourteen bios contain the string and **every
single one** is `community` or `impunity`. Not one refers to the engine. Only
`unity3d` and `unity engine` may match; bare `unity` never does, word boundary
or not. Godot and Unreal have no such collision.

**Tiers**, mapped onto the existing machinery:

| Tier | Source | Evidence | Confidence |
|---|---|---|---|
| T1 | affiliation | a resolved studio org | 0.95 |
| T2 | wikidata | `video game developer`, `game designer`, `game artist` (2 people today) | 0.95 |
| T3 | domain | `itch.io`, `gamejolt`, `store.steampowered.com` in link signals | 0.9 |
| T4a | studio name | a named studio in the bio | 0.9 |
| T4b | role text | "game designer", "narrative director", "level designer", "technical artist", "game writer/artist/producer/audio" | 0.65 |
| T5 | propagation | overlap with confirmed seeds in the follow graph | 0.5 |

T4a is given its own confidence above ordinary text because a studio name is a
checkable fact, not a self-description. T4b keeps M6a's rejection rules — "I want
to get into games" and "ex-Ubisoft" are not current jobs. T5 should be
productive here: game developers follow each other densely, and it is how the
people whose bios say only "she/her • cats • currently shipping something" get
found at all.

**Naming.** Proposed slug `game-industry`, name **Game industry**, not "Game
developers". The measured members are a narrative director, a producer on Diablo
IV, a localization writer at Nintendo and a principal UX designer at Riot — none
is a developer in the sense the word normally carries, and all four are exactly
who was meant. One line to change if "Game developers" is preferred.

**Cost**: zero API calls for detection. T5 reuses the affinity index already
built. Hydrating the newly-in-scope people for ranking is ~4 calls at
`getProfiles`' batch size of 25.

#### What the validation pass caught

Hand-checking the full list before letting it create memberships was the most
valuable hour of this milestone. The first run produced **105 members, 18 of
them wrong**, in three distinct failure modes. Final: **84 members** — 11
studio, 69 text, 3 Wikidata, 4 link — from a target set of 637.

1. **A studio name is not an employer.** "Into Marvel, Riot Games properties"
   (a fan), "Partnered with Epic Games" (a creator programme) and a bare
   PlayStation in a list of hobbies all scored 0.9. Studio matches now require
   an employment preposition — `at` or `@`, and *only* those two, because
   allowing "with" readmitted the Epic Games partnership.
2. **`systems design` is not game design.** Six false positives, among them
   "Design systems designer" and "Service & Systems Design". It is service- and
   UX-design vocabulary and has been dropped from the pattern; `narrative`,
   `level`, `combat`, `encounter` and `quest` design stay.
3. **The negation rule was too tight.** It required the negative word directly
   before the match, so "Former art daddy at Obsidian Entertainment" and
   "Previously: Adobe/Substance, Rockstar Games" were both credited as current
   employers. Studio matches now look back 56 characters — but stop at a
   sentence boundary, so "ex-Twitter. Now at Bungie" is correctly current.

Two studios were removed outright for being ordinary English: **Valve** matched
a digital-ethics consultant and **Telltale** matched a comic studio's use of the
adjective. Rare, King, DICE and Sega were never added for the same reason.
`hobbyist` and `amateur` joined the negation list after "Hobbyist Game
Developer" turned up.

Residual known imprecision, all at the 0.65 text tier and all reviewable: bios
that *list an interest* rather than a job ("Sciency Games & Game Design",
"likes … Gamedev, RTS, FPS"). Roughly 3 of 69.

Note that a `classify` run deletes and rebuilds derived memberships, so
propagation rows disappear until `propagate` runs. The "Rebuild groups" batch
already orders `groups → discover → propagate`, so this only shows if the steps
are run individually.

### M17 — Attention scarcity

Requested 2026-07-27: *if an account has tens or hundreds of thousands of
followers but follows only a few hundred, and mine is one of them, that says my
account is high signal to them.*

That is right, and worth measuring. But the first three versions of this plan
were all wrong in instructive ways, so the reasoning is recorded here rather
than just the answer.

**v1 — "add followers ÷ follows as an influence component."** Already exists.
`selectivity` in the influence score is exactly that ratio. Shipping it again
would have double-counted a number already on every row.

**v2 — "so the idea is redundant."** Also wrong, and this is the substantive
finding. The *ratio* is not the *hypothesis*. Two accounts:

| | followers | follows | ratio | selectivity | attention |
|---|---|---|---|---|---|
| A | 1,000 | 10 | 100 | **1.00** | 0.00 |
| B | 500,000 | 5,000 | 100 | **1.00** | 0.00 |

A ratio cannot tell these apart — it divides two facts into one — so selectivity
awards both its maximum. And *neither is the thing being asked about*: A is a
tiny account whose reading habits say nothing about me, B follows more accounts
than anyone reads. The ratio's top score is this component's zero, which is the
sharpest available demonstration that they are not the same measurement.

The hypothesis needs **both** terms held separately, which means it cannot be
expressed as the ratio at all.

So: `attention = scarcity(follows) × standing(followers)`, multiplied so that
*both* conditions must hold — the way the request was actually phrased.

**v3 — "put it in the influence score."** Wrong place. Selectivity asks whether
*they* are discriminating: a property of them, which is why it belongs to
influence. This asks what *my slot in their attention budget* is worth: a
property of us, which is the relationship score's question. It also fixes a real
gap there — today the relationship score needs interactions, so someone who
follows 200 people including me but has never replied scores exactly zero.
A deliberate, scarce choice to follow me is relationship evidence on its own.

**Calibration, measured not guessed** — over the 1,500 hydrated followers that
have both counts:

- `scarcity = log10(5000 / clamp(follows, 50, ·)) / log10(100)`. Above ~5,000
  follows, following is not a curated act — nobody reads 5,000 accounts, so the
  term is zero. Below 50 it stops meaning more.
- `standing = clamp((log10(followers) − 3) / 2, 0, 1)`. **Gated at 1,000
  followers.** The first draft used the ungated `log10(followers)/5` shared with
  `reach`, and it put a 434-follower account above Cory Doctorow — a small
  account following 45 people is an ordinary small account, not scarce
  attention. The gate is what makes the component match the request.

Result on the 1,500 hydrated followers: **388 score non-zero**, and the top is
alt18f (59,637 ÷ 126), Matt Bors, Kevin Beaumont, Meredith Whittaker
(150,735 ÷ 1,037), Maggie Appleton, Cory Doctorow, Mike Masnick. That is the
list the request describes.

Worth noting it does **not** rank by the ratio, which is the clearest evidence
the two are different measurements: Whittaker at 145× ranks *below* Bors at
110×, because Bors' 510 follows make a slot in his list much scarcer even though
his reach is smaller.

**Scaled** against the most attention-scarce follower actually present rather
than a constant, and **capped at 30 of the 100 relationship points** so it can
lift a silent follower into view without ever outranking a conversation.

The scale started as a p99, matching the interaction scale and the affinity
index, and that was wrong here. With ~400 non-zero values a p99 leaves four
people above the line and every one of them lands on the cap: alt18f,
doublepulsar, edoggthered and mattbors all scored exactly 30.0, destroying the
ordering at precisely the top of the list this exists to rank. Dividing by the
maximum keeps every rank distinct. The trade — one extreme account compresses
everyone below it — is fine because `raw` is bounded at 1.0.

Two limits, recorded because they are permanent:

- `follows_count` is *current*, not what it was when they followed me. Someone
  who followed at 100 follows may now be at 5,000. This cuts the right way —
  what my slot is worth now is the question — but it is not a historical claim.
- It is gameable in principle by an account with bought followers and few
  follows. The moderation lists catch the cheap version, and six-figure follower
  counts are not cheap, but the component is evidence and not proof.

Correlation with the influence score is r = 0.64 — related, as expected, since
both read `followers_count`, but far from a restatement.

### M14 — Relationship score (planned)

Requested 2026-07-27: rank the top 1,000 by influence plus every verified
follower by how much I have actually *interacted* with them — conversations,
reposts, quote posts — rather than how prominent they are.

This is a different question from influence, and deliberately a separate score.
Influence asks "does this person matter"; relationship asks "do we actually know
each other". A 700k-follower columnist I have never spoken to should rank low
here, and a 400-follower friend I talk to weekly should rank high.

**Source: notifications, not posts.** The obvious approach — walk my 23,602
posts asking who liked, reposted and quoted each — costs ~94,000 calls and about
9 hours. `app.bsky.notification.listNotifications` returns every inbound
interaction with `author`, `reason` (like / repost / reply / quote / mention),
`reasonSubject` (which of my posts) and `indexedAt`, 100 per call. Fifty
thousand notifications is 500 calls, about 3 minutes. **Roughly 190× cheaper for
strictly more information.**

Outbound is nearly free too: my own author feed already carries my replies (the
reply parent names who I replied to), my reposts and my quote posts, at ~236
calls for my entire history. `getActorLikes` covers my likes.

**Store what we observe, permanently.** The notification API's retention window
is unknown and probably finite. Interactions therefore go into an append-only
table as they are seen, so sonde accumulates its own history and the score gets
better the longer it runs — the same reasoning that makes `follow_events`
irreplaceable. First run captures whatever the API still holds; incremental runs
walk back only to the newest interaction already stored.

**What the score should weigh.** Not raw counts — a bot that likes everything
would win. The signals, in order of what they cost the giver:

| Signal | Why it counts |
|---|---|
| **Conversation** — a thread where we both posted, more than once | The strongest evidence of a relationship, and the hardest to fake |
| **Quote post** | They engaged with the substance, attributed, to their own audience |
| **Reply** | Directed attention, but one turn |
| **Repost** | Endorsement without commentary |
| **Like** | Cheapest possible signal; counted, weighted near zero |
| **Mention** | Context-dependent; low weight |

Three modifiers matter more than the raw weights:

- **Reciprocity.** Interactions in both directions mean a relationship;
  inbound-only means an audience. A multiplier on `min(in, out) / max(in, out)`
  rather than a sum, so 50 likes from someone I have never replied to does not
  outrank three exchanges with someone I talk to.
- **Recency decay.** A conversation last week beats one in 2023. Same
  exponential form the liveness component already uses.
- **Breadth over time.** Interacting across many separate days beats a single
  burst — one argument is not a relationship.

**Presentation.** A separate `/relationships` ranking and a column on the
follower table, never folded into the influence score. They answer different
questions and averaging them would destroy both. The detail page shows the
interaction history that produced the number, as every other score already does.

**Sequencing.** 14a interactions table and notification ingest; 14b outbound
from my own feed and likes; 14c conversation detection by thread; 14d scoring
with reciprocity and decay; 14e `/relationships` and the detail panel; 14f
recalibrate against a hand-checked sample of people I know I talk to.

Depends on nothing except auth, which exists. Estimated ~500 calls on first run,
then a few dozen a day incrementally.

### M15 — Discovering groups, and slicing by institution (planned)

Requested 2026-07-27. Two related asks: find groups nobody thought to name, and
slice the enrichment set by where people work. They are related because **an
organisation with several people in it already is a group** — it just has not
been called one.

Measured against the real enrichment set before planning, so these are counts
rather than guesses.

#### 15a — Institution slices

Organisations with two or more current affiliations in the top 500 plus verified:

| Wired | NYT | Washington Post | buttondown.com | The Verge | FT | EFF | Bloomberg |
|---|---|---|---|---|---|---|---|
| 6 | 5 | 4 | 3 | 2 | 2 | 2 | 2 |

An `/institutions` view listing every organisation with its people, sortable on
the same shared macros the follower and group tables now use, filterable by
organisation kind (news / tech / nonprofit / academic / government). Each row
already carries the affiliation kind, so "who leads things at the EFF" and "who
merely used to work at Google" stay distinguishable.

**Former affiliations are shown but separated.** They are genuinely interesting —
"used to be at Google" is worth knowing — and they must never be counted as
current, which is the bug M11 shipped with and had to fix.

This costs nothing: `affiliations` and `organisations` already hold it.

#### 15b — Group discovery

The twelve groups were written by hand, which means the interesting ones are
whatever nobody thought of. Three discovery mechanisms, all free, all producing
**candidates for review rather than groups asserted into existence**:

**Uncovered Wikidata occupations.** Occupations held by two or more people that
no group claims. Measured right now:

| blogger | orator | entrepreneur | video game developer | wikipedian | technologist | literary critic | podcaster |
|---|---|---|---|---|---|---|---|
| 9 | 3 | 2 | 2 | 2 | 2 | 2 | 2 |

"Bloggers" at nine and "podcasters" are obvious groups nobody wrote down.

**Uncovered link kinds.** `organisation` (22), `supported` (3), `video`,
`writing` — signals already extracted with no group to land in.

**Organisation clusters.** Any organisation crossing a threshold becomes a
proposed group automatically, which is what makes 15a and 15b the same feature.

**Bio and post phrases.** Bigrams and trigrams common across the enrichment set
but absent from every existing group definition, ranked by how concentrated they
are — a phrase in 8 bios out of 560 is a community; one in 300 is filler.

Each candidate is presented with its count and the people it would cover, and
becomes a real group only when accepted. **Nothing is auto-created**: a group
that nobody wanted is worse than a missing one, because it makes every other
count untrustworthy.

#### 15c — T5 follow-graph propagation

Still the outstanding item from M11, and it belongs here. Rules cannot find
"civic tech" (currently 1 member) or "privacy activist" (9, all from bio text),
because those are communities rather than job titles. The affinity index already
records which of ~600 sampled accounts follow each follower; two people with
similar source-sets are similar people. Seeding each group from its confident
members and propagating outward would fill exactly the groups that rules cannot
reach — and it is also how genuinely unnamed communities would surface, by
clustering first and labelling afterwards.

#### Sequencing

| Step | Work | Cost |
|---|---|---|
| 15a | `/institutions` view, sortable, kind filter, former separated | none |
| 15b | Candidate discovery from occupations, link kinds, org clusters, phrases | none |
| 15c | Follow-graph propagation and unsupervised clusters | none |
| 15d | Review queue for candidates; accepted ones become real groups | none |

No new API calls anywhere in this milestone.

### Still outstanding

**M8 — Affiliations.** Wikidata employer and position data now exists for
matched followers, which is the input this needed. The remaining work is
resolving *organisations* by notability so "President of Signal" scores without
anyone hand-adding Signal.

**M11 — Groups.** Depends on M10 post text (done) and benefits from M7.

**M13 — Extras.** Writing the top N to a real Bluesky list (`ENABLE_LIST_WRITE`,
off by default), an RSS feed of notable arrivals, and testing whether
authenticated reads are metered per-DID rather than per-IP — which, if true,
would relieve contention with BlueBirdNET and atproto-labeler outright.

---

## Follow dates — answered

*"Is there any way of recording when a follower started following me?"* Yes, and
it costs nothing extra — but only with the app password.

**What was verified on 2026-07-27**

| Claim | Status |
|---|---|
| Unauthenticated `getFollowers` carries viewer state | **No** — no `viewer` key at all |
| Follow records are public in each follower's own repo | **Yes** — `app.bsky.graph.follow` with `subject` and `createdAt` |
| The rkey is a TID encoding write time | **Yes** — decoded three real rkeys to within 0.15s of their `createdAt` |
| `profileView.viewer.followedBy` exists in the lexicon | **Yes** — an `at-uri`, and `getFollowers` returns `profileView` |

**The mechanism.** Authenticated, the follower sweep already returns
`viewer.followedBy` for every follower: the AT-URI of *their* follow of me. The
rkey in that URI is a TID, and a TID decodes locally to a timestamp. So exact
follow dates arrive with the **existing 115-call sweep** — no extra requests,
just arithmetic on a string we already have.

**Why the TID beats the record's own `createdAt`.** `createdAt` is written by
whatever client made the follow and can be wrong or backdated. The TID is
stamped by the PDS at write time. Store both; prefer the TID for ordering.

**Why not do it unauthenticated.** Follow records are public, but `listRecords`
pages through *all* of an account's follows with no filter by subject. Finding
one follower's follow of me could take dozens of calls, times 10,042 followers.
Not viable.

**Until then**, `list_rank` already recovers relative arrival order for the
whole backfilled cohort, and `first_seen_at` records when sonde noticed — which
is not the same thing and is labelled as such on the detail page.

**Caveat worth stating:** this recovers the date of the follow record that
exists *now*. Someone who unfollowed and refollowed carries the later date, and
the earlier one is unrecoverable. `follow_events` remains the only record of
that history, which is another reason it is the one table that matters.

Implementation sits in **M8** alongside the affiliation work, since both turn on
enabling the app password.

---

## M9–M11 — auth, posts, ignoring, and groups

Requested 2026-07-27. Four things, planned together because they share
machinery: the app password unlocks follow dates, posts feed both liveness and
grouping, and the group-target set is the same ~600 accounts worth fetching
posts for most often.

---

### M9 — Authenticate, and stop showing accounts I don't care about

**9a — Session auth.** `BLUESKY_APP_PASSWORD` in `.env`.
`com.atproto.server.createSession` on startup, `refreshSession` before expiry,
held in memory so a restart just re-authenticates. Every call stays a **read**;
`ENABLE_LIST_WRITE` remains off.

What it buys, now that the public affinity index has replaced the reason it was
originally wanted:

- **Exact follow dates, free.** An authenticated sweep returns
  `viewer.followedBy` per follower — the AT-URI of their follow of me. The rkey
  is a TID that decodes locally to a timestamp (verified to within 0.15s). No
  extra calls; the data is already in the response we pay for.
- Exact `knownFollowers` as a cross-check on the sampled index.

If the session fails, **every tier degrades to its unauthenticated path** rather
than the sweep failing. Auth is an enhancement, not a dependency.

**9b — Ignore accounts.** `ignored_at` on `follower_state`. Ignored accounts are
excluded from listings, rankings, the group target set and CSV export — but
**never deleted**, still swept, still counted in totals, and their history is
untouched. An `/ignored` page lists them with a one-click restore. Ignoring is
a display preference, so it must not silently corrupt the record: totals keep
saying 10,042 with "N ignored" alongside.

---

### M10 — Recent posts

**The ask was three recent posts for every follower on every run. That is 10,042
calls per run** — no bulk endpoint exists, `getAuthorFeed` is one call per
actor. At a 6-hour cadence that is 40,168 calls/day and 3.7 hours of continuous
traffic on an IP shared with BlueBirdNET and atproto-labeler. Under quota,
over the line on manners.

Tiered instead, which delivers the same thing with fresher data where it counts:

| Set | Size | Cadence | Cost |
|---|---|---|---|
| Top 500 by influence + all verified + new arrivals | ~600 | every full sweep (6h) | ~600 calls |
| Everyone else | ~9,400 | rolling, once daily | ~9,400 calls |

**~12,400 calls/day, 1.4% of the daily budget**, spread out rather than in one
burst. Every follower still gets three recent posts refreshed daily; the ones
that matter refresh four times as often.

Stored per post: URI, text, `indexedAt`, like/repost/reply counts, and whether
it is a repost. Three kept per follower, replaced wholesale on each fetch.

**This retires the liveness proxy.** Liveness currently falls back to a lifetime
posts-per-day average — flattering to accounts that died in 2024. Real
`indexedAt` from the newest post replaces it for everyone covered, which is
everyone.

---

### M11 — Groups

Group the **top 500 by influence plus all 147 verified** (~600 distinct), into
overlapping groups: journalists, writers, novelists, newsletter writers,
technologists, designers, developers, Apple, Google, Microsoft, civic tech,
privacy activists, academics, politicians. Membership is many-to-many.

The requirement was *efficient and accurate*. Efficiency is easy — every signal
below is either already stored or arrives with M10. Accuracy is the real
problem, and the groups differ enormously in how hard they are.

**Tiers, strongest evidence first. Each membership records its tier, its
evidence, and a confidence — same discipline as affiliations.**

| Tier | Method | Cost | Precision | Best for |
|---|---|---|---|---|
| **T1** | Affiliation → group (org kind and identity) | free | very high | Apple, Google, Microsoft, journalists |
| **T2** | Wikidata `P106` occupation, `P39` position | free (bulk) | very high, ~1% coverage | novelists, politicians, academics |
| **T3** | Handle domain (`.edu`, `substack.com`, `.gov`) | free | high | academics, newsletter writers |
| **T4** | Bio + post-text rules, reusing M6a's past/product/consumption rejections | free | moderate | developers, designers, journalists |
| **T5** | Label propagation over the existing affinity index | free | moderate | the fuzzy ones |
| **T6** | Human confirm / reject | — | exact | everything |

**T5 is the interesting one, and it costs nothing.** The affinity index already
records, for each follower, which of ~600 sampled accounts follow them. Two
followers with similar source-sets are similar people — that is a real
similarity signal sitting in a table we already built. Seed each group from
T1–T3 members, then propagate: a follower whose source-set overlaps heavily
with a group's seeds is a candidate for that group. It is guilt-by-association,
so it is proposed-not-asserted and always lands in the review queue.

This matters because rules will not find "civic tech" or "privacy activist".
Those are communities, not job titles, and they are legible in *who follows
whom* long before they are legible in bio text.

**What is deliberately not proposed:** LLM classification. It would be the most
accurate approach for the fuzzy groups and it is not free, not deterministic,
and not something to add to this app's dependencies without being asked. If the
review queue proves tedious, that is the moment to reconsider — not before.

**Measuring accuracy rather than assuming it.** After the first pass, hand-check
a random 50 per group and record precision in the eval. A group that cannot
reach 80% precision gets its rules tightened or is demoted to
propagation-only-with-review. Numbers, not vibes.

---

### Sequencing

| Step | Work | New API cost |
|---|---|---|
| 9a | Session auth, graceful degradation | none |
| 9b | Ignore / restore | none |
| 9c | Follow dates from `viewer.followedBy` TIDs | none |
| 10a | `posts` table, tiered fetch, scheduler wiring | ~12.4k/day |
| 10b | Real liveness replaces the lifetime-average proxy | none |
| 11a | `groups` + `group_members`, seeded definitions | none |
| 11b | T1–T4 classification | none |
| 11c | T5 label propagation | none |
| 11d | Review queue and group pages | none |
| 11e | Precision eval on a hand-checked sample | none |

Only 10a costs anything. Everything else reuses data already paid for.

### Improvements made to this plan before starting

| First draft | Problem | Revised |
|---|---|---|
| Posts for all followers every run | 40k calls/day on a shared IP | Tiered: ~600 every 6h, the rest daily |
| Auth as a prerequisite | One bad credential breaks all syncing | Every tier degrades to its public path |
| Ignore = exclude everywhere | Silently changes totals and history | Excluded from *listings*; totals show "N ignored" |
| Groups from bio rules alone | Cannot find civic tech or privacy activists | T5 propagation over the follow graph already built |
| Trust the classifier | No idea if it works | Hand-checked precision per group, 80% floor |
| Store posts forever | Unbounded growth for a display feature | Three per follower, replaced wholesale |

---

## Testing

`pytest` + `pytest-asyncio`, matching the buywanderbot layout. Priority goes to
bugs that are *silent* — every one of these was found by probing the real API, so
the fixtures are real captured responses, not hand-written ones.

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
  boundary; sampled-vs-exact affinity selection; the "affinity absent → out of
  75" path.
- **Client parsing** — captured fixtures of the awkward real cases, all present
  in live data: `handle.invalid` (37), a profile with **no `verification` key at
  all** (the common case — omitted, not `"none"`), a `!no-unauthenticated` label,
  a verified account, a short page.
- **Backup** — `VACUUM INTO` produces a readable DB while the app is writing;
  retention prunes to 14; the target path is the bind mount, not the volume.
- **Routes** — 200 against an empty DB and a seeded one, including empty states.

---

## Open questions

| # | Question | Status |
|---|---|---|
| 1 | Head sweep at 15 min, full sweep at 6h — right balance? Full-sweep cadence sets unfollow latency at 2× | Default 15 min / 6h |
| 2 | Show the 1,838 `!no-unauthenticated` followers, or withhold them? | Default: show, marked "private" |
| 3 | ~~App password?~~ | **Answered 2026-07-26 — yes, read-only by default** |
| 4 | Email digest for notable arrivals? | Not built; M7 |
| 5 | ~~Off-box backup?~~ | **Closed 2026-07-27 — not a priority, and not via Syncthing. Snapshots remain on-box; revisit only if asked** |
| 6 | Affinity band and source cap (150–2,000 follows, 600 sources) — set by pilot, not theory. Tier 3b's exact counts are the check | Start at 600, revisit at 6d |
| 7 | Track a second account too? | Single account; schema is DID-keyed, so additive later |

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
- `graph.getFollows` and `feed.getAuthorFeed` are **public**.
- `graph.getKnownFollowers` returns **401 `AuthMissing`** unauthenticated.
- Rate limit: 3,000 requests / 5 min / IP, shared across everything on ubuntuplex.
