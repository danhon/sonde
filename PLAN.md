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
| Docker volumes are **not** backed up on ubuntuplex | 2026-07-26 | The event history needs an off-box path of its own — Syncthing, see [§9](#9-the-history-is-the-only-irreplaceable-data-and-nothing-on-the-box-backs-it-up) |

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
| All 4,429 accounts I follow | **62,552** | 5.8 hours |
| The 2,000 most selective | 6,015 | 33 min |
| **The 1,000 most selective** | **1,587** | **9 min** |
| The 500 most selective | 500 | 3 min |

Full coverage is out — 62k calls on a shared IP is antisocial regardless of the
rate limit. But sampling by *selectivity* is both the cheap option and the
**statistically better one**: an account that follows 200 people is making a far
stronger endorsement than one following 50,000. The cheapest lists to fetch are
precisely the ones whose follows mean the most.

**So both get used, for different jobs.** The public index (1,587 calls monthly)
ranks all 10,041. The authenticated exact count (1,000 calls monthly) refines and
displays the top slice, and doubles as a validation check on the sample — if the
sampled ranking and the exact numbers disagree badly, the sample size is wrong
and the settings page will show it.

The sampled figure is labelled honestly wherever it appears: "how many of the
1,000 most selective accounts you follow also follow this person", never
presented as the true `knownFollowers` count.

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
        └─→ nightly snapshot → /backup (host bind mount, Syncthing-replicated)
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
| **3a** | Affinity index | `graph.getFollows` × 1,000 selective accounts | 1,587 calls | Monthly | Ranks **all** 10,041 followers |
| **3b** | Exact affinity *(authenticated)* | `graph.getKnownFollowers` | 1 / actor | Top 1,000, monthly | Exact count + validates the 3a sample |
| **3c** | Liveness | `feed.getAuthorFeed` | 1 / actor | Top 1,000, 14-day TTL | Real last-post date |

| Sync kind | Calls | Wall time at 3 req/s |
|---|---|---|
| Head sweep | 1–2 | <1 s |
| Full sweep | 116 | ~39 s |
| Affinity index rebuild | 1,587 | ~9 min, monthly |
| Cold start (everything) | ~4,150 | ~23 min |
| **Steady state, per day** | **~917** | ~5 min of traffic, spread out |

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

### 9. The history is the only irreplaceable data, and nothing on the box backs it up

Every other table can be re-fetched from Bluesky. `follow_events` cannot — once a
departure is missed, it is gone. **Docker volumes on ubuntuplex are not backed
up**, so a volume loss means losing the entire point of the app.

Syncthing already runs on ubuntuplex as a native systemd service, currently
replicating the Calibre library Mac → ubuntuplex. Adding a second folder in the
reverse direction gives an off-box copy with no new infrastructure:

| | ubuntuplex | Mac |
|---|---|---|
| Role | Send Only | Receive Only |
| Path | `~/sonde-backups` | `~/Backups/sonde` |

Nightly `VACUUM INTO ~/sonde-backups/sonde-YYYY-MM-DD.db`, keeping 14. `VACUUM
INTO` is the right call over a file copy: it takes a consistent snapshot of a
live WAL database without stopping the app.

**This requires a host bind mount, not a named volume** — Syncthing runs natively
and cannot see Docker's internal volume storage. So `compose.yml` mounts
`~/sonde-backups:/backup` for snapshots while the live DB stays in the named
volume. Getting this wrong produces backups that are dutifully written and never
replicated, which is worse than none because it looks fine.

The Syncthing runbook is at
[docs-site/docs/services/syncthing.md](../reverse-proxy/docs-site/docs/services/syncthing.md);
this second folder should be documented there too when M4 lands.

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

Each ends at something deployable and useful on its own.

**M0 — Scaffold.** `Dockerfile`, `compose.yml` (both routers, backup bind mount,
`mem_limit: 512m`), `Makefile`, `.env.example`, `pyproject.toml`, `CLAUDE.md`,
`/healthz`. Deploy it empty; confirm `sonde.sgc.rayandhon.com` 302s to Authelia
while `/healthz` returns 200. Do this *first* — routing gotchas are cheaper
against a hello-world.

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
Affinity contributes 0 at this stage and scores show "of 75". **Answers goal 2.**

**M4 — Change over time, and the backup.** `daily_snapshots`, `/changes`,
dashboard growth chart, `needs_review` banner with its override, nightly `VACUUM
INTO` to the bind mount, and the Syncthing folder configured and documented.
**Answers goal 3.** Don't let the backup slip past this milestone — every day
after M1 is history that can't be reconstructed.

**M5 — Depth.** Tier-2 mutuals, `/followers/{did}`, search and filters, CSV export.

**M6 — Affinity, reputation, and enrichment.** The scoring work, sequenced in
[SCORING.md](SCORING.md#build-order):

- **6a** — institution matching from data already stored (attested, domain,
  claimed, role seniority). Zero new API calls, largest single improvement.
- **6b** — institution rosters via `listRecords`, monthly.
- **6c** — tier 3a affinity index, 3b exact counts, 3c liveness, and the
  verified-affinity index.
- **6d** — recalibration against real output; Bouie ranking near the top is the
  acceptance test.

Also the place to test whether authenticated reads are metered per-DID rather
than per-IP.

**M7 — Optional extras.** Independent, none required:
- Email digest of notable arrivals via Fastmail SMTP (buywanderbot pattern) —
  worth more now that arrivals surface within 15 minutes
- Write the top N to a real Bluesky list (`ENABLE_LIST_WRITE`, off by default)
- RSS feed of notable arrivals

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
| 5 | ~~Off-box backup?~~ | **Answered 2026-07-26 — none exists; Syncthing folder added in M4** |
| 6 | Affinity sample size — 1,000 selective accounts is a judgement call. Tier 3b will show whether it's enough | Start at 1,000, revisit with real data |
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
