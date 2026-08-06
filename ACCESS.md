# ACCESS — who can see what

> **Status as of 2026-08-06: NOT IMPLEMENTED. There is no public URL.**
>
> This is a design, not a description. Nothing in it has been built: no second
> hostname, no read-only mode, no allowlist middleware, no redaction, no
> `robots.txt`. The deployed container has two Traefik routers, both on
> `sonde.sgc.rayandhon.com`, and the main one carries `authelia@file`.
> `sonde-public.sgc.rayandhon.com` is a name in this document that resolves to
> nothing.
>
> The one piece that *does* exist is L0 in §3, the same-origin write guard —
> and it was built for the CSRF finding in the 2026-08-04 security review, not
> for this plan. It happens to compose with it.
>
> Two questions in §6 and §1 are still unanswered by the operator and block
> phase 2. See §7 for per-phase status.

sonde is currently one thing: a private site behind Authelia where every page
reads and a handful of buttons write. This document specifies the split into
**two audiences on one container**: an anonymous reader who gets an
allowlisted, redacted, read-only view, and the operator, who gets what exists
today, unchanged, behind Authelia.

The design rule this whole document serves:

> **The public app must never be given the data it must not show.**
> Not hidden in a template — never loaded.

That is not fastidiousness. `CLAUDE.md` already records the Jinja failure that
motivates it: `{% from "_sort.html" import who %}` without `with context`
silently dropped the follow button, and nothing failed. A template-level
`{% if not public %}` has exactly that failure mode, and the thing it would
leak is a named person's private data rather than a missing button.

---

## 1. What is public

Chosen scope: **aggregates plus public-safe person pages**. Anonymous visitors
see the shape of the follower graph and the people in it, using only fields
that are already public on Bluesky. They see nothing the operator wrote, nothing
derived from the operator's authenticated session, and nobody who opted out of
being visible logged-out.

| Route | Public | Note |
|---|---|---|
| `/` | yes | dashboard tiles, growth, arrivals chart, top 10 |
| `/followers` | yes | table + sort + filters; redacted columns |
| `/followers/{did}` | yes | profile, posts, score breakdown |
| `/influential` | yes | |
| `/verified` | yes | |
| `/changes` | yes | arrivals/departures |
| `/healthz` | yes | already unguarded; liveness only |
| everything else | **no** | 404 |

`/` and `/changes` are public *after redaction*, not as they render today —
see §1.1. And note `/openapi.json`: `create_app()` passes `docs_url=None,
redoc_url=None` but **not** `openapi_url=None`, so a complete machine-readable
map of every admin route is currently live. Harmless behind Authelia, a
directory of the write surface in public. Set `openapi_url=None`; the
default-deny middleware would also catch it, and both should hold.

Explicitly not public, each for its own reason:

| Route | Why not |
|---|---|
| `/export.csv` | Bulk enumeration of ~10k people is a different artefact from a page you can read |
| `/settings` | Job control, SMTP, weights, backup state, run history |
| `/circles`, `/circles/*` | The operator's private classification of named people |
| `/relationships`, `/interactions` | Derived from the operator's own notification history |
| `/institutions` | Roster inference plus a weight control |
| `/ignored` | Publishing "sonde hid this person, because a moderation list names them" republishes an accusation |
| `/build` | Commit subjects from a **private** repo |
| `/api/status` | Scheduler internals, job names, sync ages |
| `/notices/*` | Operational faults |
| every `POST` | Writes, all of them |

### 1.1 Two allowlisted pages are not safe as they stand

Route-level allowlisting is necessary and not sufficient. Two of the six public
routes leak through their *contents*, and one through its *query string*.

**`/` leaks operations.** `dashboard_stats()` returns `recent_syncs` (job run
history — kinds, timings, outcomes) and `needs_review`, and `index.html` renders
both. Everything §1 excludes by keeping `/settings` private walks straight onto
the homepage. The public dashboard builds its own dict: tiles, growth,
`recent_changes`, `top`. Not `recent_syncs`, not `needs_review`.

**`/changes` leaks reasons.** `changes.html` renders `e.reason` and `e.detail`.
Reasons include moderation outcomes — "hidden, because a list names them" — which
§1 keeps private by excluding `/ignored`, and which arrive here by another door.
Public `/changes` shows the event and the date; not the reason, not the detail.

**`/followers?tag=<slug>` is a read oracle for private data.** The chips are
gone from the public render, but `ranked_followers(tag=...)` still filters, so
a visitor guessing or enumerating slugs recovers the operator's private
classification of named people — the single most sensitive thing in the
database — without a single circle route being public. **In public mode `tag`
is ignored, and `all_tags` is never loaded.**

This is the general lesson, and it deserves to outlive this document: *a
redacted page and a redacted query string are two different jobs.* Any public
route taking a parameter that reaches the store needs its parameters
allowlisted the same way its routes are. `q`, `order`, `direction`, `verified`,
`mutual`, `min_followers`, `page` are fine — they filter on public fields.
`tag` is not.

### Field-level redaction on the public person page

`follower_detail` is assembled by the route, not the store, so the public route
builds a different context — it does not build the full one and filter.

| Loaded today | Public | Reason |
|---|---|---|
| profile, counts, score, `components` | keep | public on Bluesky, or derived from public data |
| `posts` | keep | public posts |
| `affiliations` | keep | inferred from public bio/verification |
| `circles` | **drop** | operator's private annotation |
| `moderation_lists` | **drop** | an accusation about a named person |
| `shared` | **drop** | `getKnownFollowers`, visible only to the authenticated operator |
| `interactions`, `breakdown` | **drop** | the operator's notification history |
| `all_tags` | **drop** | the vocabulary itself is private |

Same treatment on `/followers`: the tag/circle chips, the batch checkboxes and
the `+follow` control never enter the public render.

---

## 2. Accounts that opted out of logged-out visibility

Followers carrying `!no-unauthenticated` set a preference on Bluesky:
*do not show me to logged-out viewers*. Today sonde shows them, marked `◍`,
because `RESPECT_NO_UNAUTHENTICATED=false` and the only viewer is the operator,
who is logged in — to Bluesky as well as to Authelia.

A public sonde is exactly the logged-out viewer they excluded. So:

**Rule: private-flagged accounts are omitted from every public surface, and
their public detail page 404s.** Omitting them from lists but leaving
`/followers/{did}` reachable is not a redaction; it is an unindexed leak.

They remain fully visible to the operator, and they still count: the dashboard
may continue to report *N private* as a withheld total. That is the option
chosen — exclusion from pages, not erasure from arithmetic.

### Mechanism

`settings.respect_no_unauthenticated` is consulted in four places inside the
store (`store.py` around lines 849, 1089, 1249, 1262). It is process-wide;
public mode is per-request. Replace each read with:

```python
def hide_private() -> bool:
    return settings.respect_no_unauthenticated or public_view.get()
```

where `public_view` is a `ContextVar[bool]` set explicitly by the web
middleware on **every** request, and defaulting to `False` so background jobs,
the CLI and the digest mailer are untouched.

The default is deliberately not fail-safe here, and that is a considered
trade: a fail-safe default of `True` would silently strip private accounts from
the operator's own sweeps and digests, which is a correctness bug rather than a
privacy one. The safety lives in the middleware setting it unconditionally.

---

## 3. Enforcement — three independent layers

Any one of these alone would work most of the time. The point is that a mistake
in one is caught by another.

**L0 — already in place.** A same-origin middleware refuses every unsafe method
that does not come from sonde's own pages (`sonde/web/origin.py`). It was added
for the sibling-subdomain CSRF path, not for this plan, but it composes: a
public page cannot be induced to write, whatever else goes wrong below.

**L1 — Traefik.** The public router matches `Host(public) && Method(GET|HEAD)`.
No Authelia middleware. A `POST` to the public host never reaches the app.

**L2 — Application.** Middleware resolves the mode from the `Host` header, then:
non-`GET`/`HEAD` → `403`; path not on the public allowlist → `404`. The
allowlist is a positive list of route names; **default deny**.

**L3 — Data.** Public routes call a different context builder, and the store
excludes private accounts via the ContextVar. Nothing to hide because nothing
was fetched.

### Mode resolution fails safe

```
public = host_without_port(request.headers.get("host", "")) not in ADMIN_HOSTS
```

Strip the port before comparing, or local dev on `localhost:8090` never matches
`localhost` and the operator's own machine silently serves the read-only site —
a confusing failure that looks like the feature not working.

`ADMIN_HOSTS` defaults to `SERVICE_HOST` plus loopback (for local dev). An
**unrecognised** host is public. This deliberately composes with the trap
already documented in `CLAUDE.md`: bypass the Makefile, `SERVICE_HOST` is empty,
the Traefik rule becomes `Host(``)` — and now the failure mode is *the site is
read-only*, not *the admin surface is unauthenticated*.

The app trusts `Host` because only Traefik can reach it: the container
publishes no ports and sits on `traefik_default`. **That is a load-bearing
property.** If a port is ever published, forging `Host: sonde.sgc...` restores
the write surface to anyone on that network.

### The write surface in templates

`_sort.html`'s `follow_back()` already gates on `settings.enable_follow_write`.
Rather than teaching every macro a new variable — and rediscovering the
`with context` bug one macro at a time — public routes render with

```python
public_settings = dataclasses.replace(settings, enable_follow_write=False)
```

`Settings` is a frozen dataclass whose fields are all `init=True`, so this
works, and every macro that already reads `settings` gets the right answer with
no edit. Template edits are then limited to the things templates genuinely own:
the nav list, the activity strip, the build panel.

### base.html

Three shared elements leak and must be gated on mode, in the layout rather than
in macros:

- **NAV** — a public list: `Followers, Influential, Verified, Changes`.
- **The activity strip** — job names, progress, scheduler state, sync ages. It
  polls `/api/status`, which is 404 publicly, so leaving it in means a broken
  widget as well as a leak. The markup *and* its polling script go.
- **The build panel** — commit subjects from a private repo. Gone; the bare
  SHA may stay.

---

## 4. Deployment

`compose.yml` gains a public router pair, injected the same way and with the
same discipline as `SERVICE_HOST`:

```
traefik.http.routers.${COMPOSE_PROJECT_NAME}-public.rule=Host(`${PUBLIC_HOST}`) && (Method(`GET`) || Method(`HEAD`))
traefik.http.routers.${COMPOSE_PROJECT_NAME}-public.entrypoints=websecure
traefik.http.routers.${COMPOSE_PROJECT_NAME}-public.tls.certresolver=le
traefik.http.routers.${COMPOSE_PROJECT_NAME}-public.service=${COMPOSE_PROJECT_NAME}
```

`Makefile` injects `PUBLIC_HOST ?= $(APP)-public.$(DOMAIN)` alongside
`SERVICE_HOST` in `deploy` **and** `preview`. `.env.example` documents it as
documentation-only, as `SERVICE_HOST` already is.

No Authelia change is required — the public host is simply never given the
middleware, and `default_policy: deny` is irrelevant when Authelia is not in
the path. Confirm the TLS cert: `le` must be able to issue for the new name,
and DNS must resolve it (verify whether the zone is wildcard before assuming).

`make verify` gains four assertions, which is the point of it existing:

```
admin  /            302   (Authelia)
public /            200
public /settings    404   (the app's default-deny)
public /  -X POST   404   (Traefik: no router matches — see below)
```

That last one is **404, not 405**. A `Method()` guard in the rule means a
`POST` matches no router at all, and Traefik answers unmatched with 404. Worth
writing down because 405 is the intuitive expectation and a `verify` target
asserting it would fail against a perfectly correct deployment. It also means
Traefik's refusal is indistinguishable from the app's — which is why L2 must be
independently correct and independently tested, rather than trusted to be
redundant.

---

## 5. Tests

The load-bearing test is not any single route's behaviour. It is this:

```python
def test_every_route_is_explicitly_public_or_denied():
    for route in app.routes:
        assert route.name in PUBLIC_ROUTES or public_get(route.path).status_code == 404
```

Default-deny is only worth anything if adding a route without thinking about it
fails the suite. Alongside it:

- `POST` to each public path on the public host → 403 (app) — Traefik's 405 is
  not testable in-process, so the app layer must be independently correct.
- A private-flagged follower is absent from public `/followers` **and** their
  `/followers/{did}` 404s.
- The public detail context contains none of `circles`, `shared`,
  `moderation_lists`, `interactions`, `breakdown`, `all_tags`.
- Public HTML contains no `+follow`, no `/settings` href, no `activity-dot`,
  no `data-build-body`.
- Public and admin renders of the same page differ — a guard against the mode
  never being applied at all.
- `/openapi.json` 404s publicly.
- Public `/` context has no `recent_syncs` and no `needs_review`; public
  `/changes` HTML contains no reason or detail strings.
- **`/followers?tag=<slug>` returns the same rows as `/followers` in public
  mode.** Not "renders no chips" — the same rows. A test asserting the chips
  are absent passes while the oracle is wide open.
- `compose.yml` declares a public router with no `authelia@file` in its
  middlewares (cheap, and it catches the copy-paste that would matter most).

---

## 6. Load, indexing, rate

Public traffic against SQLite is new. Mitigations, in order of value:

1. `Cache-Control: public, max-age=300` on public responses. The sweep cadence
   is 15 minutes, so five is an honest number; do not claim more freshness than
   exists.
2. A Traefik `rateLimit` middleware on the public router only.
3. Cap the public `/followers` page size, whatever the query string says.

Pagination still lets a determined visitor walk the roster. That is inherent in
publishing person pages and is accepted, not mitigated — `/export.csv` staying
private removes the convenience, not the possibility.

**Open question for the operator:** should the public site be indexable?
Default taken here: **`robots.txt` disallowing everything**, on the grounds that
"anyone who has the link" and "the first Google result for a follower's handle"
are materially different exposures for the people listed, and only one of them
was asked for. Easy to reverse; hard to un-index.

---

## 7. Phases

Each phase is independently shippable and the site is correct after each.

| Phase | Work | Ships | Status |
|---|---|---|---|
| 0 | Mode plumbing, ContextVar, default-deny middleware, full test suite. No public router deployed. | Nothing visible; the mechanism is provably correct first | **not started** |
| 1 | Public context builders, private exclusion in the store, `base.html` gating, `public_settings`. | Still nothing visible | **not started** |
| 2 | `compose.yml` + `Makefile` + `make verify` + DNS/TLS. | The public site | **not started**, and blocked below |
| 3 | Cache headers, rate limit, `robots.txt`. | Hardening | **not started** |

Phase 0 before Phase 2 is the whole discipline: the enforcement is testable
without ever exposing anything, so it gets tested before anything is exposed.

**Phase 2 is blocked on two decisions**, both of which change what gets built
rather than merely how, and neither of which is the author's to make:

1. **Should the public site be indexable?** §6 takes a default of `robots.txt`
   disallowing everything. "Anyone who has the link" and "the first Google
   result for a follower's handle" are materially different exposures for the
   people listed, and only one of them was asked for.
2. **Should departures appear on public `/changes`?** They are in scope as
   chosen, and §1.1 already strips the reason and detail. Even so, "these 12
   people unfollowed you last week" is a pointed thing to publish about named
   individuals, and counts-only is a defensible alternative.

---

## 8. What this design does not do

- **It does not isolate credentials.** One container holds the Bluesky app
  password, `GITHUB_TOKEN` and the SMTP credentials, and serves the public host.
  A compromise of the app is a compromise of the Bluesky account, exactly as
  today — but the attack surface now includes anonymous traffic. See §9 for why
  a second container is not the fix it looks like, and what is.
- **It does not make the data non-personal.** Everything published is public on
  Bluesky, but sonde aggregates, ranks and scores it, and an aggregate is a new
  artefact. Someone may reasonably object to being *ranked* even where every
  input was public. There is no in-app way to honour that request today. If one
  is wanted, the smallest version is a per-DID `public_hidden` flag respected by
  L3 — worth having before, not after, the first request arrives.
- **It does not stop the page phoning home for the visitor.** `base.html` loads
  Tailwind from `cdn.tailwindcss.com` and every avatar comes from Bluesky's CDN.
  Behind Authelia that is one known browser. Publicly, every anonymous reader's
  IP goes to two third parties on every page load. Not a blocker, and not worth
  a build step today — but it is a claim not to make if the site is ever
  described as private for its readers.
- **It does not protect `/healthz`,** which was already public and already
  audited (`tests/test_web.py::test_healthz_leaks_no_follower_data`). Public
  mode adds no new obligation there, but that test is now guarding a busier
  road.

---

## 9. Why not a separate read-only container

The obvious stronger design — a second container serving the public host, with
no Bluesky credentials and a read-only DB mount — was declined. An earlier draft
of this document blamed "operational cost", which was wrong and is corrected
here, because the right reason points somewhere more useful.

**Resources are not the objection.** Measured on ubuntuplex, 2026-08-04:

| | |
|---|---|
| Host | 4 cores, 11.8 GB RAM, 6.3 GB available, 272 GB free, 15 containers, load 0.5–1.5 |
| `sonde-app-1` | 154 MiB / 512 MiB limit, 0.20% CPU |
| Image / volume | 155 MB `sonde-app:latest`, 118 MB `sonde_sonde-data` |

A twin runs the same image and the same volume — zero extra disk — and without
the scheduler, job registry and HTTP client pool it would land near 90–120 MB.
That is ~2% of available RAM. `scrypted` alone uses 2.5 GB. Cost is not the
reason for anything here.

**The objection is that it does not isolate what it appears to.**
`schema.sql` sets `journal_mode = WAL`, and **a `:ro` mount cannot read a WAL
database**: SQLite must create and write `-shm` and `-wal` to read one, and
fails `SQLITE_CANTOPEN`. The escape hatches are worse — `immutable=1` asserts
the file never changes while a live writer is changing it, which buys silently
corrupt reads. The workable shape is a *shared writable mount* plus
`PRAGMA query_only`: software isolation, not filesystem isolation. Most of the
security argument evaporates at that point.

It also adds a failure mode the single container does not have. A long-running
reader in a second process holds back WAL checkpointing and `-wal` grows
unbounded. One container is one process and one connection pool, with no
cross-process WAL contention at all — a real advantage of the chosen design,
not merely an absence of cost.

The remaining costs are small but quiet, which is the dangerous kind:

- **Two schedulers.** Prevented by running the twin as `sonde`, without
  `--schedule` — but forgotten, every sweep runs twice against an API budget of
  3000 req/5 min *shared house-wide* with BlueBirdNET and atproto-labeler. The
  nightly `VACUUM INTO` would likewise have two containers writing into one
  `/backup`.
- **Monitoring surface.** docker-watchdog alerts on per-container memory and
  CPU; a twin is one more thing that can page the operator.
- **Deploy.** Genuinely near zero, as a second *service* in this `compose.yml`
  rather than a second project: one build, shared layers, `make deploy` covers
  both.

**What it does buy is credential isolation**, via a separate `env_file` — and
that is worth more than this section's other paragraphs concede. It is the only
clean answer to §8's first bullet.

**So if credential isolation becomes the priority, do not split public from
admin — split jobs from web.** The credentials are used by scheduled sweeps,
not by page renders. A jobs container that holds them and a web container that
does not gives the same benefit, along the grain of what actually needs them,
and leaves exactly one process writing to SQLite.
