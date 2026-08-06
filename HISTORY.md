# sonde — history

What was built, in the order it was built, and why. Split out of PLAN.md on
2026-07-29 when the milestone narrative had grown to roughly three times the
design it was appended to.

The reasoning is the point. Anyone can read the diff; what does not survive in
`git log` is why a threshold is 0.9 rather than 0.8, which of two obvious designs
was tried first and abandoned, and which features were killed by measuring
before building. Numbers here were measured against the live API or a production
snapshot at the time of writing, and are dated where it matters.

See [PLAN.md](PLAN.md) for the design as it now stands, [BUGS.md](BUGS.md) for
what is currently wrong, and [SCORING.md](SCORING.md) for the influence score.

---

## Overview

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


---

## Milestone by milestone

M14 and M15 were written as plans and are kept in that voice; both shipped.

### M9–M11 — auth, posts, ignoring, and groups
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

### M14 — Relationship score

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

### M15 — Discovering groups, and slicing by institution

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

### M26–M32 — previews, merging, and the rename

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


---

## Questions that got answered

### Follow dates

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

### Byline directories for journalists — investigated, nothing to buy

*"Is there an open directory of professional journalists' bylines we could
enrich profiles from?"* Investigated 2026-07-29. **No, and the best available
signal is already in the database.**

**Muck Rack** is the obvious candidate and is closed. `robots.txt` is itself
behind a Cloudflare managed challenge, a profile URL returns 403 to a non-browser
client, and there is no public API — it is a paid product sold to PR teams.
Getting past the challenge would be circumventing an access control, so it is
not on the table regardless of what the data is worth.

**MuckRock** — the FOIA platform, a different organisation with a confusingly
similar name — was checked first by mistake and is also closed: every `api_v1`
endpoint returns 401, including `foia`, `news` and `jurisdiction`. Its
addressable population here is four people, all of whom already say so in their
bio, so it would add nothing even with credentials.

Everything else open is the wrong population. OpenAlex, ORCID and Crossref index
academic authorship, not journalism. Authory, Contently and Clippings host
self-serve portfolios with no directory to query. **Wikidata** is the one real
open source and is already integrated — it accounts for 29 of the 70 people in
the journalists circle, which is exactly the famous tail it is good at.

Two things the investigation turned up that matter more than the answer:

**Verification issuers are an unused employer attestation.** Of 148 verified
followers, 135 are verified by `bsky.app` and **14 by news organisations** —
wired.com (6), washingtonpost.com (3), financialtimes.com (2), nbcnews.com,
ms.now, nytimes.com. That is an employer asserting employment, recorded in the
follower's own profile, already fetched at zero API cost, and currently used
only to draw a badge and group the /verified page. Promoting it into
`affiliations` would give 14 people an attributed outlet from data already on
disk — better evidence than any third-party directory, because the outlet said
it rather than an aggregator inferring it.

**Handle domains are not a byline source on this list.** 1,736 followers have a
custom-domain handle and 1,595 of those domains are unique. The shared ones are
hosting providers — eurosky.social (41), brid.gy (20), myatproto.social (16),
blacksky.app (10) — not employers. Exactly one follower has an outlet-domain
handle: `tomhannen.ft.com`. The mechanism works and there is almost nobody to
apply it to.

The residual gap is real but small and unserved: 41 people are in the
journalists circle on bio text alone, with no employer known. No open directory
covers them, because the ones that would are commercial.

## 2026-08-04 — a rail on the follow list

A security review found that `replace_my_follows` would empty the table given a
`getFollows` response of `{"follows": []}` with no cursor — an API blip, a
deactivated or renamed actor, a mistyped `BLUESKY_ACTOR`. Reproduced before
being believed:

```
BEFORE: [{'did': 'did:plc:alice', 'follow_uri': 'at://…/follow/abc123'}]
AFTER : []
```

Wholesale replacement is correct and stays — its docstring records the two
timestamp-diffing versions that were not. What was missing was any check that
the list handed to it is plausible. Departures have had that check since M12;
the follow list had none, which is the whole finding: the safety rail existed,
and had simply never been applied to the other list.

**Why 25%, against 2% for departures.** They are guarding different populations.
`MASS_DEPARTURE_PCT` watches ~10,000 followers, where 2% is 200 people leaving
at once and never innocent. The follow list is two orders of magnitude smaller
and the operator unfollows people by hand, so a few in an afternoon is normal
and a 2% rail would fire constantly. 25% is sized to catch a sweep that returned
nothing or nearly nothing — the failure that actually happened — rather than to
police ordinary unfollowing.

**Why a floor of 20, and why it is not configurable.** The first version of the
rail was a bare percentage, and it broke two existing tests in `test_depth.py`
that replace a one- or two-name list wholesale to check unfollow detection.
Those tests are right and the rail was wrong: a quarter of two people is one
person, and no threshold can tell a faulty sweep from a real unfollow at that
size. Below 20 known follows only the empty-sweep guard applies. That is a claim
about when a ratio starts carrying information, not a policy choice, so it is a
module constant rather than another environment variable.

**The URI turned out to be recoverable, which changed the fix.**
`record_my_follow` has always written the created URI into
`follow_events.detail`, and `follow_events` is the one table that cannot be
rebuilt from Bluesky — the reason the nightly snapshot exists. So the loss was
never permanent in the database, only on the path that reads it: the undo button
died and `already` flipped to 0, so the follow-back button offered to follow
someone sonde already followed and would have written a second record.

`replace_my_follows` now falls back to the event log for any URI missing from
the live row, taking the later of `followed_back` / `unfollowed_back` by id so an
undone follow is not resurrected. That repairs a database this bug has already
damaged, on the next sweep, with no migration.

## 2026-08-04 — two failure domains in the follow button, and a logger that never existed

`follow_back` ran the Bluesky write and the bookkeeping inside one `try`, so a
store error *after* a successful `createRecord` was handled as though the write
had failed: `follow_events` recorded `follow_failed` for a follow that exists
publicly on the operator's account, and the returned URI — the only handle on
undoing it — was discarded. The two stages are now separate, because they are
separate failure domains: before the write, a failure means nothing happened and
saying so is correct; after it, the record is real and cannot be taken back.

When the second stage fails there is nothing left to write to, so the URI goes to
the log at ERROR, deliberately shouting, because at that point the log is the
only surviving copy.

**The test written for that found a worse bug underneath it.** `log` was
referenced in the follow-back handler and never defined anywhere in `app.py` —
no `import logging`, no module-level logger. So the first line of the except
block raised `NameError` *inside the except block*: the operator got a 500, and
`record_follow_failure` never ran. Every failed follow-back since M23 was
silently unrecorded, which is the exact opposite of the guarantee in
`record_follow_failure`'s own docstring ("A failed write is logged, not
swallowed") and in `follows.py`'s module docstring ("**Logged.** Every follow and
unfollow is written to `follow_events`").

It survived because every test of that path called `store.record_follow_failure`
directly. The store function was always correct; nothing ever reached it. The new
tests drive the HTTP route instead, which is the only way this was ever going to
show up — and the reason the three of them were checked against the old code
before being kept.

## 2026-08-04 — writes must come from sonde

The third finding of the security review, and the one with the largest blast
radius. sonde has no CSRF tokens and nothing checked that a write came from
sonde at all. The Authelia cookie looked like it was covering that and was not:
it is scoped to `domain: sgc.rayandhon.com` with Authelia's default
`same_site: lax`, and Lax stops cross-**site** requests. Subdomains of one
registrable domain are same-site.

So every other service on ubuntuplex — calibre-web, which serves user-supplied
ebook content, plus homepage, docs, scrypted, birdnet-go and the watchdog —
could POST here and the browser would attach the operator's session. An XSS in
any of them was write access to sonde: follow, unfollow, archive a circle, merge
two, start a batch job. Nothing in this repository could tell, because the
weakness is in the shape of the fleet's single sign-on rather than in sonde.

The guard is a middleware over every unsafe method, and the load-bearing line is
the one that refuses `Sec-Fetch-Site: same-site`. Most CSRF guidance treats
same-site as friendly; here it is precisely the attack, because the cookie is
shared across the whole domain. `Origin` is the fallback for browsers too old to
send `Sec-Fetch-Site`, compared including port so local dev works.

**A middleware rather than a per-route dependency**, because the failure mode of
a per-route check is forgetting it on the route added next month — and the
routes that most need it are the ones that write to Bluesky. The test suite
enumerates every state-changing route from `app.routes` and asserts each refuses
a sibling subdomain, then asserts the same set is *not* refused from sonde: a
middleware that returned 403 unconditionally would satisfy the first test alone
and take the application down.

**Requests carrying neither header are allowed, on purpose.** Every current
browser sends at least one on a form POST and a hostile page cannot strip them,
so the only clients this admits are curl, the test suite and scripts on the box
— none of which a cross-origin attacker can become. Refusing them would have
meant rewriting every existing POST test to prove nothing.

Verified in a real browser rather than only in-process, because this sits in
front of every form in the application and a wrong guess about what browsers
send would have 403'd all of them. Chromium, against a local server: a
same-origin `fetch()` POST as `base.html` makes them reached its 303, a
same-origin `<form>` POST landed on its redirect, and a genuine cross-origin
`<form>` POST — not subject to CORS, which is what makes CSRF work — came back
403 with `Sec-Fetch-Site: cross-site` in the log.

What this does not fix: the cookie is still shared fleet-wide, so any other
service can still *read* sonde's pages with the operator's session. Narrowing
that is an Authelia change in `reverse-proxy`, not one here.

## 2026-08-06 — what the security review covered, and what it left

The three preceding entries each record one fix. This is the record of the pass
that produced them, written because the most perishable output of a review is
not the bugs — those become commits — but the list of things that were examined
and found sound. Without it the next reader re-audits them, or worse, assumes
they were never looked at.

**Fixed, in their own commits:** the follows-sweep wipe, the follow/bookkeeping
split and the undefined logger under it, the scheme-relative open redirect, and
the fleet-wide CSRF path. Four commits, 29 new tests, every one of them checked
against the old code before being kept.

**Checked and found clean.** Not "looked at" — traced:

* **SQL injection.** Every dynamic `ORDER BY` in the store resolves through an
  allowlist dict with a safe default (`SORTABLE`, `REL_SORTABLE`, `IX_SORTABLE`,
  `CANDIDATE_SORTABLE`, `ORG_SORTABLE`, `MEMBER_SORTABLE`). The remaining
  f-string SQL is placeholder counts (`IN ({placeholders})`) or internal column
  names from `_migrate`. No user-controlled string reaches a query body.
* **XSS.** No `|safe`, no `Markup`, no `autoescape` override anywhere in the
  templates; `charts.py` emits numeric coordinates, not markup.
* **`_safe_back`.** Correct, including the scheme-relative case its docstring
  describes. It was the *other* validator that was wrong (BUG-14), which is why
  there is now only one.
* **Credential handling.** `Authenticator.status()` excludes both token and
  password; nothing logs either; `GITHUB_TOKEN` and the SMTP password are read
  from the environment and never rendered.
* **Both documented API hazards are honoured in code.** `get_profiles` maps by
  DID rather than position, and `iter_followers`/`iter_follows` terminate only
  on a missing cursor.

**One finding was wrong and is withdrawn.** The review reported the institution
weight as unvalidated, taking `inf` and negatives into the score. It does not:
`set_organisation_weight` clamps with `max(0.0, min(1.0, weight))`, and `inf`,
`-5` and `1e308` were all checked and store 0.0 or 1.0. The real defect is
narrower and is BUG-18 — `nan` compares false against everything, so the clamp
passes it through as 1.0 and garbage becomes the maximum weight. Written down
because a review that only ever adds to the pile is not being read carefully.

**Left, deliberately:** BUG-15 to BUG-18, all P2/P3, all in BUGS.md with
reproductions. And one thing that cannot be fixed here at all — the Authelia
cookie is scoped to the whole of `sgc.rayandhon.com`, so while writes are now
refused unless they come from sonde's own pages, **reads are not**. An XSS in
any sibling service can still read sonde as the operator. That is a
`reverse-proxy` change, and it is recorded in BUGS.md so it is not mistaken for
something the middleware closed.

**Deployed** the same day: `origin/main` at `28a3af1`. The one check that needed
a human was a real write through the browser, since the same-origin guard sits
in front of every form and had only been verified locally over plain HTTP —
following someone through Traefik and TLS confirmed it.

**Not deployed, and not built:** everything in ACCESS.md. There is no public
URL. That document now says so at the top, because it reads like a description
of a running system and is a design for one that does not exist.
