# sonde — influence scoring design

How sonde decides which followers are influential, why each signal is weighted
the way it is, and what each one can honestly claim.

→ **[PLAN.md](PLAN.md)** — the app around it · **[README.md](README.md)** — what it is

---

## The problem with follower count

Follower count answers *"who is famous"*. That is not the question. Two accounts
from the real data, both with **741,720 followers**:

- **Jamelle Bouie** — verified, New York Times opinion columnist. Should rank at
  the very top.
- An **engagement farm** that follows 600,000 people back. Should rank nowhere
  near it.

Raw reach cannot separate them. Neither can a blue check on its own — the 147
verified followers here run from national columnists to people with a few hundred
followers. So the score is built from eight signals, each covering a different
way reach lies.

---

## The signals

| Component | Weight | Source | Marginal cost |
|---|---|---|---|
| **Reach** | 18 | `followersCount` | Already fetched |
| **Institution** | 18 | Verification issuer, handle domain, bio text, Wikipedia | **Free** + ~3 calls/institution/month |
| **Affinity** | 16 | Inverted follow-graph index | ~4,300 calls/month |
| **Verified affinity** | 13 | Same index, verified sources only | ~2,200 calls/month |
| **Public profile** | 12 | Wikidata, Wikipedia pageviews, news volume | **~1 query/month** + ~120 calls/week |
| **Selectivity** | 11 | `followersCount ÷ followsCount` | Already fetched |
| **Liveness** | 7 | Last post date | 1 call/actor, top 1,000 |
| **Verification** | 5 | `verifiedStatus` | **Free** — rides on the sweep |

Every component is stored decomposed in `score_components`, and every row in the
UI expands to show what produced its number, from which source, at what
confidence. A score nobody can interrogate is a score nobody should trust.

---

## Institution

The requirement: *an NYT opinion columnist should score very high*. That means
knowing where someone works, which Bluesky does not expose as a field.

### What the data actually looks like

Measured across all 147 verified followers:

| Path | Coverage | Tells us |
|---|---|---|
| Verified **by the institution** | **14 of 147 (10%)** | Employer, cryptographically |
| Verified **by Bluesky itself** | 134 of 147 (91%) | Nothing about employer |

The 14 came from 6 outlets — Wired (6), Washington Post (3), Financial Times (2),
NBC News, ms.now, NYT.

**Jamelle Bouie is in the 91%.** His verification issuer is `bsky.app`; his handle
`jamellebouie.net` is his own domain. Within Bluesky, the only place "New York
Times" appears is his bio: *"Columnist for the New York Times Opinion section."*

So the free cryptographic path covers a tenth of the verified set and misses the
case that motivated the feature.

### Five paths, ranked by what they prove

| # | Path | Evidence | Confidence |
|---|---|---|---|
| 1 | **Attested** | Verification issuer is the institution | 1.0 |
| 2 | **Domain** | Handle is a subdomain of the institution | 0.95 |
| 3 | **Roster** | Listed in the institution's verification records | 0.95 |
| 4 | **Corroborated** | Bio claim **and** an external source agree | **0.95** |
| 5 | **Claimed** | Bio text alone names the institution | 0.4–0.85 |

Path 4 is what external data buys here. Bouie's Wikipedia entry opens *"Jamelle
Antoine Bouie is an American columnist for The New York Times"* — an independent
source stating the same employer his bio claims. Two sources that agree are worth
far more than either alone, and it lifts him from a hedged claim to near-attested
without anyone touching a keyboard.

For path 5 on its own, confidence is conditional: **0.85** if the account is
verified (Bluesky verification already involved an identity check, so a verified
account claiming an employer is unlikely to be lying), **0.4** if not.

### Rosters are enumerable

Verification records live in the **verifier's** repo, so an institution's roster
is one paginated public call:

```
com.atproto.repo.listRecords?repo=<institution-did>&collection=app.bsky.graph.verification
```

Against NYT this returns 92 records on page one with a cursor for more. One call
per institution per month keeps a complete staff list for every outlet that
verifies its people — and every new issuer seen during a sweep is a candidate for
the table, so it grows itself as Bluesky's verifier programme expands.

### Weights are editorial

What a masthead is worth is a judgement, not a fact. Institutions live in a
user-edited table seeded from discovered issuers plus a starter list. Institution
score is `max(weight × confidence)` across matches — best evidence wins rather
than accumulating. A short editable seniority list then multiplies it:
*columnist, editor, op-ed, professor* 1.0; *reporter, correspondent, producer*
0.85; *intern, fellow, freelance* 0.6; no match 0.85 (an absent job title is not
evidence of juniority).

---

## Verified affinity

The second request: *how many verified followers does this account have?* Here
the honest answer differs sharply from the obvious one.

**The global version is impossible.** Counting an account's verified followers
means walking its entire follower list. For Bouie that is 741,720 followers —
**7,418 API calls for one person**, before scoring anyone else.

**What is affordable is sharper anyway.** sonde already builds an inverted
follow-graph index (see [Choosing index sources](#choosing-index-sources)).
Restricting it to verified sources — the 147 verified accounts that follow me,
plus verified accounts I follow — covers all 10,041 followers at once rather
than a top-N slice. It answers:

> Of the verified accounts in my network, how many follow this person?

Deliberately **network-scoped, not global**, and the UI says so: *"18 of the 147
verified accounts in your network follow this person."* A real, checkable number
instead of an estimate dressed as a fact.

**The sampled shortcut is rejected.** Reading the first page of someone's
followers gives a density estimate for one call, but the list is newest-first, so
it samples *recent* followers — and the bias runs the wrong way, since
established accounts collected their prestigious followers years ago.

---

## Choosing index sources

Both affinity components depend on which accounts seed the inverted index. An
earlier version of this document got that badly wrong, and a pilot caught it.

### The mistake

The original rule was "sample the most selective accounts I follow", justified
by: an account following 200 people makes a stronger endorsement than one
following 50,000, so the cheapest lists to fetch are also the most meaningful.

The first half is true. The second does not follow. **Signal per follow is not
total signal.** Sorting my 4,433 follows by fewest-follows-first selects accounts
that follow 0–15 people — cheap precisely because they endorse almost nobody.

### What the pilot measured

| Source selection | Sources | Calls | Followers reached | Max hits |
|---|---|---|---|---|
| Fewest follows (0–15 each) | 200 | 200 | **77 (0.8%)** | 7 |
| Mid-band (150–1,500 each) | **60** | 430 | **1,408 (14.0%)** | 33 |

Sixty mid-band sources reached eighteen times as many followers as two hundred
of the cheapest ones. The top of the cheap run was *me*; the top of the mid-band
run was Anil Dash, Mike Masnick, Meredith Whittaker, Molly White, Charlie Jane
Anders, Karen Hao, Taylor Lorenz and Eva Galperin — which is exactly the answer
the component exists to produce.

The signal separates sharply: median follower count is **18,909** for anyone
with ≥3 hits versus **526** for those with none. And several of the top-affinity
accounts currently score near zero on reach alone, so affinity surfaces people
the other components miss — the whole point.

### The corrected design

Sources are accounts I follow whose own `followsCount` falls in a **band**,
because both ends are useless: below the floor there is no coverage, above the
ceiling the endorsement means little and the list is expensive to fetch.

```
AFFINITY_MIN_FOLLOWS = 150     # below this a source contributes almost nothing
AFFINITY_MAX_FOLLOWS = 2000    # above this the endorsement is diluted and the fetch is dear
AFFINITY_MAX_SOURCES = 600     # budget cap, cheapest-first within the band
```

Rather than discard the selectivity insight, it moves where it belongs — onto
the **hit**, not the source list. Each hit is weighted by how selective its
source is, so a wide-following source can still contribute coverage without its
endorsements counting as much as a discriminating one's:

```
weight = clamp(AFFINITY_MIN_FOLLOWS / followsCount, 0.1, 1.0)
```

A source following 150 contributes 1.0 per hit; one following 1,500 contributes
0.1. Affinity is the sum of weights, not a raw count.

### Cost

Measured at **7.2 calls per source** across the band. 600 sources is roughly
4,300 calls, run monthly — about 145 calls/day amortised, against a steady state
of ~900. The full 2,737-source band would cost ~19,600 calls; that is affordable
monthly but the marginal coverage per call falls off, so the cap stays at 600
until the recalibration step says otherwise.

---

## Public profile — reputation from outside Bluesky

Standing in the world, not just on one network. The governing principle is
**join, don't search**: pull a whole dataset once, then match locally, rather
than making a network call per follower.

### Wikidata: one query for the entire mapping

Wikidata has property **`P12361` — Bluesky handle**. So the complete
Bluesky↔Wikidata mapping is a single SPARQL query:

```sparql
SELECT ?h ?item ?itemLabel ?sl WHERE {
  ?item wdt:P12361 ?h .
  ?item wikibase:sitelinks ?sl .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

Measured: **10,563 rows in 5.8 seconds, one HTTP call.** Refreshed monthly, it
becomes a local join — zero per-follower cost, forever.

Joined against the follower list:

| | |
|---|---|
| Followers checked | 10,042 |
| **Matched in Wikidata by handle** | **107 (1.07%)** |
| Of which verified | 40 |

Low coverage, perfect precision, and it surfaces exactly the right people —
Naomi Klein (67 language editions), Bruce Sterling (38), Miguel de Icaza (30),
Jeff VanderMeer (29), Meredith Whittaker (13), Molly White (12), Emily M. Bender
(10).

**Sitelink count is the notability measure**: how many language Wikipedias
consider someone worth an article. It is hard to game, editorially reviewed, and
already computed.

From the matched entity, also free: occupation (`P106`), employer (`P108`, a
direct feed into the Institution component), and awards (`P166`).

### Wikipedia pageviews: attention, not just existence

Having an article says you were notable once. Pageviews say whether anyone is
reading it now. The Wikimedia REST API is free and needs no key:

- Naomi Klein — **29,920 views in 30 days** (997/day)
- Jamelle Bouie — **6,726 views in 30 days** (224/day)

One call per matched person per week — about 107 calls, or **15 a day**. Trivial.

### GDELT: news presence

GDELT indexes global news and is free with no key. One call returns a mention
volume timeline for a name, giving "is this person being written about, and how
much" without touching a publisher's site.

The risk is name collision — "Naomi Klein" is safely unique, "John Smith" is not.
So GDELT runs **only where a disambiguating token exists**: a Wikidata match, or
an institution to pair with the name in the query. Otherwise it is skipped rather
than guessed at. 14-day TTL, top-N only.

### Self-declared links

Many bios contain a personal site. That URL was published by its owner, on a
public profile, pointing at their own page — the most consentful external source
available. Fetch it once, parse JSON-LD `schema.org/Person` for `jobTitle`,
`worksFor`, `affiliation`, plus `og:` tags. This often yields a cleaner
institution string than bio prose.

Rules: honour `robots.txt`, one request per host at a time, 90-day TTL,
descriptive User-Agent with a contact address, hard timeout, HTML only, and skip
anything requiring a login.

### LinkedIn

Requested, and included as an **opt-in module that is off by default** — but the
honest assessment belongs in the plan rather than a footnote:

- LinkedIn's User Agreement **prohibits automated scraping**, and its
  `robots.txt` disallows nearly all crawling.
- Public profiles sit behind an intermittent auth wall; unauthenticated fetches
  get redirected or served a stub, and repeat requests are blocked quickly.
- *hiQ v. LinkedIn* found that scraping public data likely isn't a CFAA
  violation, but the case ended badly for hiQ on **breach-of-contract** grounds.
  The exposure is contractual, not criminal — and still real.
- Matching a Bluesky account to the right LinkedIn profile is unreliable without
  a self-declared URL.

Given all four, the yield is poor and the risk is asymmetric. So the module is
scoped to the defensible slice: **only LinkedIn URLs a follower published in
their own Bluesky bio**, fetched once, at low rate, with a 180-day TTL, and never
crawled beyond that single self-declared page. `ENABLE_LINKEDIN=false` ships as
the default, with the caveats printed next to the toggle rather than buried here.

Everything the component needs is obtainable from Wikidata, Wikipedia, GDELT and
self-declared homepages, all of which are free, licensed for reuse, and designed
to be consumed programmatically. **LinkedIn is expected to be a rounding error.**

### Extending coverage beyond the 1.07%

Exact handle matching covers 107 followers. Name-based matching would cover more
but risks attaching the wrong person's reputation — a far worse failure than no
match at all.

So fuzzy matching runs **only for top-N unmatched followers**, and requires a
name match **plus** a corroborating token: bio institution equals Wikidata
`P108`, or bio keywords overlap `P106` occupation. Every match stores
`match_method` and `match_confidence`; anything below 0.8 is recorded but
contributes nothing to the score until confirmed. Unmatched results are
**negative-cached for 180 days** so the same fruitless lookups don't repeat.

### Efficiency summary

| Source | Pattern | Cost | TTL |
|---|---|---|---|
| Wikidata mapping | One SPARQL query, then local join | **1 call/month** | 30d |
| Wikidata entities | Batch 50/call, matched only | ~3 calls/month | 30d |
| Wikipedia pageviews | Matched only | ~15/day | 7d |
| GDELT | Top-N with a disambiguating token | ~20/day | 14d |
| Self-declared sites | Top-N, robots-respecting | ~10/day | 90d |
| LinkedIn | Self-declared URLs only, opt-in | ~0 | 180d |
| **Total added** | | **~45 calls/day** | |

Against a Bluesky steady state of ~917/day, external enrichment adds roughly 5%.
Every source is gated on internal score, so the 9,000 followers who will never
appear in a ranking are never looked up at all.

---

## The other four

**Reach — 18.** `clamp(log10(followers) / 5, 0, 1)`. Log-scaled; the distribution
is a power law. Demoted as sharper signals took over.

**Affinity — 16.** Overlap with the general follow-graph index. See
[PLAN.md finding 6](PLAN.md#finding-6--the-affinity-signal-is-better-computed-from-public-data).

**Selectivity — 11.** `clamp(log10(followers / max(follows,1)) / 2, 0, 1)`,
**scoring 0 below 500 followers**. The gate is load-bearing: ungated, an account
with 5 followers that follows 1 out-scores a working journalist with 20k
followers who follows 8k.

**Liveness — 7.** Days since last post for the enriched top 1,000; lifetime
posts-per-day for everyone else, which is a *lifetime average* and flatters
accounts that died years ago. The UI marks which measurement a row used.

**Verification — 5.** Trusted verifier 1.0, verified 0.7, none 0. Low because
Institution, Verified affinity and Public profile now extract far more from the
same underlying fact.

---

## Worked example: Jamelle Bouie

Every input below is real, measured on 2026-07-26.

| Component | Input | Score | Weighted |
|---|---|---|---|
| Reach | 741,720 followers → capped | 1.00 | **18.0** |
| Institution | NYT × corroborated by Wikipedia (0.95) × columnist | 0.95 | **17.1** |
| Affinity | High overlap with selective accounts followed | ~0.80 | **12.8** |
| Verified affinity | Many verified journalists in-network follow him | ~0.80 | **10.4** |
| Public profile | Wikidata `jamellebouie.net` → 3 sitelinks; 6,726 views/30d | ~0.60 | **7.2** |
| Selectivity | Huge follower count, low follow count → capped | 1.00 | **11.0** |
| Liveness | Posts regularly | ~0.90 | **6.3** |
| Verification | `valid`, issuer `bsky.app` | 0.70 | **3.5** |
| | | **Total** | **≈ 86 / 100** |

The engagement farm, same 741,720 followers: reach 18.0, selectivity 0.5,
liveness 6.7 (farms post constantly — activity is not virtue), everything else 0.
**≈ 25.** A 61-point gap on identical follower counts. That gap is the whole
exercise.

---

## What this deliberately does not do

- **No global verified-follower count.** 7,418 calls for one large account. The
  network-scoped figure is reported instead, labelled as such.
- **No inferring seniority from follower count.** Circular — it folds Reach into
  Institution and counts it twice.
- **No per-follower external lookups.** Everything external is either a bulk join
  or gated on internal score. The 9,000 followers who will never rank are never
  looked up.
- **No fuzzy match without corroboration.** Attaching the wrong person's
  reputation is worse than attaching none.
- **No automatic institution weighting.** What a masthead is worth is the user's
  call, exposed and editable.

---

## Honest limitations

**Bio text is self-reported, and there is no exit signal.** Someone who left the
NYT in 2023 and never updated their bio still scores as NYT. Wikipedia
corroboration helps for the notable few and does nothing for everyone else.

**Institution coverage is uneven.** All 7 verification issuers discovered here
are news outlets — Bluesky's verifier programme skews heavily to journalism.
Academics, civil servants, engineers and artists have far weaker institutional
signal, and the editable alias list is a manual mitigation rather than a fix.

**Wikidata coverage is 1.07%.** Excellent for the people it covers, absent for
the other 99%. Public profile will be 0 for almost everyone, which is correct —
most people don't have a Wikipedia article — but it means the component only ever
discriminates at the very top of the ranking.

**External sources have their own biases.** Wikipedia under-covers women and the
global south; GDELT over-indexes English-language and US news. A score leaning on
them inherits those gaps, and they compound with Bluesky's own skew rather than
cancelling it.

**Verified affinity inherits the network's biases.** It measures standing in one
corner of Bluesky. That is the intent, and it is never presented as general.

**Weights are opinions.** All eight are defaults, all editable, all shown on
`/settings`, and changing any triggers a rescore. The point of full decomposition
is that disagreeing is easy and acting on the disagreement is one edit away.

---

## Build order

Slots into [PLAN.md](PLAN.md#milestones) at M6 (atproto-only) and M7 (external).

| Step | Work | New API cost |
|---|---|---|
| **6a** | Institution table, issuer auto-discovery, attested + domain + claimed matching, seniority | None |
| **6b** | Roster enumeration per institution | ~3/institution/month |
| **6c** | Verified-affinity index | ~2,200/month |
| **7a** | Wikidata bulk join + entity fetch; sitelinks, `P106`, `P108`, `P166`; institution corroboration | ~4/month |
| **7b** | Wikipedia pageviews for matched followers | ~15/day |
| **7c** | GDELT, gated on a disambiguating token | ~20/day |
| **7d** | Self-declared homepage parsing, robots-respecting | ~10/day |
| **7e** | LinkedIn module, self-declared URLs only, **off by default** | ~0 |
| **7f** | Recalibrate weights against real output; top 200 reviewed by hand | None |

**6a and 7a are the two highest-yield steps** and between them cost about five
API calls a month — 6a re-reads data already stored, and 7a is a single bulk
query. Bouie landing near the top of the leaderboard is the acceptance test
for 7f.
