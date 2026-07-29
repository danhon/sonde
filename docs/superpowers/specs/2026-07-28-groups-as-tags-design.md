# Groups as tags — design

**Date:** 2026-07-28
**Status:** approved, not yet implemented
**Milestone:** M25

Groups today are something the machine decides and a human may veto. This makes
them something a human decides and the machine helps with. The rules keep
running; hand decisions outrank them and are never overwritten.

## What exists

`groups` is `(id, slug, name, description, created_at)`. `group_members` is
`(group_id, did)` keyed, carrying `tier`, `confidence`, `evidence`,
`source_url`, `confirmed`, `created_at`. Eight tier values are written today —
`affiliation`, `wikidata`, `domain`, `studio`, `text`, `propagation`, `cluster`
and `discovered` — and `manual` is documented in the schema as a ninth that
nothing produces.

Groups are born two ways: seeded from the `GROUPS` list in `sonde/groups.py`, or
by accepting a discovery candidate. There is no rename, no delete, no manual
add, and no batch anything. The only human control is a "remove" button that
sets `confirmed = 0`, and it renders only on unreviewed rows — so a group the
machine got *right* cannot be corrected at all.

`classify_groups` already protects manual rows twice: it deletes only
`WHERE confirmed IS NULL AND tier != 'manual'`, and its upsert updates only
`WHERE group_members.confirmed IS NULL`. Both guards are correct. Neither has
ever protected a row, because nothing can create one.

## Decisions

**Rules stay; manual is a peer tier.** Classification keeps running and keeps
assigning. A hand decision always wins and is never re-litigated by a job. Every
membership the rules have already found is kept.

**Tag** depends on what is already there, and in all three cases stamps
`decided_at`:

| Existing row | Effect | Why |
|---|---|---|
| none | insert `tier='manual'`, `confidence=1.0`, `confirmed=1`, evidence `"tagged by hand"` | the machine never proposed this; the human is the whole reason |
| derived, `confirmed IS NULL` | set `confirmed = 1`, leave `tier`/`evidence`/`confidence` alone | this is confirming the machine, not replacing it — erasing the evidence would throw away the *why* |
| any, `confirmed = 0` | set `confirmed = 1` | undoing a previous untag |

**Untag** is `UPDATE confirmed = 0`, whatever the row's origin — one rule, no
special cases. It is durable by construction, because every job writes only to
rows where `confirmed IS NULL`. The machine's `tier`, `evidence` and
`confidence` survive the override untouched, so the record shows both what it
thought and that it was overruled.

Untag touches existing rows only. Removing a tag from a hundred people when
thirty have it is a no-op for the other seventy — it does not write pre-emptive
tombstones, so a rule may legitimately tag one of those seventy later. Blocking
a future match is not what "remove" means.

**Delete is archive.** A new `groups.archived_at` column. The tag vanishes from
every listing, filter and count; memberships are preserved; it can be restored.
Nothing is destroyed.

`seed_groups()` needs no change. Its `ON CONFLICT (slug) DO NOTHING` finds the
archived row still present and does nothing — resurrection is prevented by the
row continuing to exist.

**The jobs must skip archived groups too, not only the reads.** Found during
review: `classify_groups`, `propagate_groups`, `_apply_cluster_group` and
`_apply_discovered_group` all resolve groups by id or slug with no archive
check, so an archived tag would go on quietly accumulating members that nobody
can see. Archiving has to mean the tag stops receiving new members as well as
stops being displayed — otherwise restoring it a month later produces a group
full of people who were never reviewed.

**Rename changes `name` only, never `slug`.** Forced, not chosen: `slug` is both
the URL key and the seeding key. Change it and the next classify run re-inserts
the original slug under its original name, and there are two of everything. A
slug is permanent from birth. Renaming "Privacy & security" to "Infosec" leaves
`/groups?slug=privacy` in the URL bar forever, and that is the correct trade.

**Merge is not offered**, because rename cannot produce it and nothing has asked
for it.

**Manual tags apply to anyone**, not only the top-500-plus-verified grouping
set. A tag applied by hand is a fact about a person; withholding it because they
are insufficiently influential would be absurd. Consequence accepted
deliberately: the composition chart's `in_scope` denominator must include anyone
carrying a manual tag, or its "N of M in scope" line starts lying the first time
someone outside the set is tagged.

**One new membership column: `group_members.decided_at`**, nullable TEXT, set
only by hand actions. `created_at` is stamped by whichever job inserted the row
first, so without this there is no way to ask what was tagged this week.

Both new columns go through the existing `MIGRATIONS` dict that drives
`_migrate` (`sonde/db/store.py:128`), which adds columns idempotently on
startup, as well as into `schema.sql` for fresh databases.

**Vocabulary is unchanged.** The UI keeps saying "Groups" and the URLs stay
`/groups`. Renaming to "Tags" is cosmetic churn that breaks bookmarks and digest
links and changes no behaviour.

### Approaches rejected

A separate `tag_overrides` table keeps derived and human data apart, at the cost
of turning every group read into a two-source join and leaving `confirmed`
vestigial but undroppable. Collapsing `group_members` into plain membership plus
an append-only provenance table describes "groups are tags" most cleanly, and
rewrites `classify_groups`, `propagate_groups` and both discovery paths to
deliver no behaviour that was asked for.

## Interface

**The core needs no JavaScript.** sonde has Tailwind by CDN and one inline
script (the activity poller); every write is a plain form and a 303. Batch
tagging fits without exception:

- one `<form method="post">` wraps the table; checkboxes are all `name="did"`,
  which posts as a list and arrives as `dids: list[str] = Form([])`
- the tag field is `<input list="all-tags">` over a `<datalist>` — native
  autocomplete over existing tags; an unknown name creates one
- the action bar is `position: sticky; bottom: 0` — sticky by CSS, not script
- roughly fifteen lines of progressive enhancement give a live "N selected"
  count and a select-all box. With JS off, everything still works.

### Surfaces

| Where | What |
|---|---|
| `/followers/{did}` | group chips gain an `×`; a `+ tag` datalist input adds one |
| `/followers` | checkbox column, sticky batch bar, and a `tag` filter |
| `/groups` | checkbox column and batch bar on the member table; rename, archive and new-tag controls; an archived section offering restore |

### Routes

| Route | Effect |
|---|---|
| `POST /groups/new` | create from a name; slug generated once, reusing `decide_candidate`'s slugify |
| `POST /groups/{slug}/rename` | `name` only |
| `POST /groups/{slug}/archive?undo=` | set or clear `archived_at` |
| `POST /groups/apply` | batch: repeated `did`, plus `tag` and `action=add\|remove` |
| `POST /followers/{did}/tags` | single add or remove from the detail page |

`POST /groups/{slug}/{did}/review` stays as-is for review-queue semantics. The
member table's "remove" button moves to the tag endpoint so untagging works on
kept and manual rows, not only unreviewed ones.

### Downstream

- **`/followers` tag filter** — joined through `group_members` and `groups` with
  `COALESCE(confirmed,1) = 1 AND archived_at IS NULL`, combinable with the
  existing verified / mutual / min-followers filters. It must be carried in the
  `filters` dict or paginating and re-sorting silently drop it.
- **`export.csv`** — one `tags` column, semicolon-joined slugs.
- **Daily digest** — gated on measurement, see below.

### Refactors taken while in there

The referer sanitiser inside `follow_back` becomes a shared helper rather than
being copy-pasted into five new write routes.

## Measurement gate: tags in the digest — **measured, and dropped**

Measured 2026-07-29 against a production snapshot (10,038 current followers).
The rule set before running it was: below half, the line is mostly blank and the
change dies.

| cohort | n | carry a group |
|---|---|---|
| all current followers | 10,038 | 417 (4.2%) |
| 90-day arrivals | 547 | 22 (4.0%) |
| 30-day arrivals | 146 | **7 (4.8%)** |

4.8% against a 50% bar. **The digest change is dropped.**

The reasoning that motivated the gate turned out to be right for the wrong
reason. The design guessed arrivals would be *specially* ungrouped — no
hand-tags yet, too new to be classified. They are not: 4.8% against a 4.2%
base rate is no different from anyone else. Nor is it a hydration artefact,
which was the other candidate explanation: 145 of those 146 arrivals were
hydrated, and only 1 was verified.

The real cause is coverage. The grouping set is the top 500 by influence plus
verified accounts plus sweep matches, and that is roughly 4% of the list — so
*any* line reading group membership is blank about 95% of the time, on the
digest or anywhere else. Building it would have added a mostly-empty field and
taught nobody anything.

The measurement query, for whoever revisits this:

```sql
SELECT COUNT(*) AS arrivals_30d,
       SUM(CASE WHEN EXISTS (SELECT 1 FROM group_members m
                              WHERE m.did = fs.did
                                AND COALESCE(m.confirmed,1) = 1) THEN 1 ELSE 0 END)
         AS with_a_group
  FROM follower_state fs
 WHERE COALESCE(fs.followed_at, fs.first_seen_at) >= date('now','-30 day');
```

Worth revisiting only if group coverage rises a long way above 4%, which
hand-tagging — the point of this milestone — is the most likely way to achieve.

## Testing

**The guards that have never fired.** Tag by hand, run `classify_groups`, assert
the row survives with `tier` still `manual`. Untag a rule-derived row, run
`classify_groups`, assert it stays untagged. The failure this prevents is silent
and delayed: hand work vanishes on the next six-hourly job and is noticed days
later. `propagate_groups`, `_apply_cluster_group` and `_apply_discovered_group`
were checked and all use `ON CONFLICT DO NOTHING`; tests pin that too, because
safety by accident becomes unsafe under editing.

**Creating a tag whose slug matches an archived one.** `ON CONFLICT DO NOTHING`
would do nothing, redirect happily, and leave the operator looking at a page
with no new tag and no error. It must detect the archived row and offer restore.
Neighbours: a name that slugifies to empty (`"🎮"` under the existing slugify)
is rejected, and a collision with a live tag says so rather than silently
suffixing.

**Archive applies everywhere or nowhere.** The read paths that expose a group
are `group_summary`, `groups_for`, `group_members` (reachable by hand-typing
`/groups?slug=`), the composition chart, the `/groups` chip row, the datalist,
and the new followers filter. Missing one surfaces an archived tag in exactly
one place. Table-driven over the surfaces rather than written out as separate
assertions, so a new surface added later fails the test by omission.

**Archived groups stop receiving members.** Run `classify_groups` and
`propagate_groups` with a group archived, and assert its membership count is
unchanged. This is the hole review found; without the test it reopens the first
time someone edits a job.

Also: archive and restore preserves memberships; `seed_groups` does not
resurrect an archived seeded slug; rename changes `name` and leaves `slug`
resolvable; batch add and remove across ~100 dids including rows with existing
derived memberships; the tag filter combining with other filters and surviving
pagination; `export.csv` carrying the column; routes returning 200 against empty
and seeded databases; and every new macro imported `with context` —
`tests/test_follow_back.py:234` enforces that repo-wide and needs extending.

**Mobile.** A checkbox column is being added to two of the widest tables on the
site. Static tests cannot prove a page fits; the eval must run against a
populated database:

```
DB_PATH=/path/to/real.db uv run --with playwright python -m evals.mobile_check
```

## Not in scope

Select-all across pages — selection is the visible hundred. Merging tags.
Renaming the `/groups` URL space. Per-tag colours. Tag hierarchies.

## A win that falls out for free

`propagate_groups` seeds from `COALESCE(confirmed,1) = 1 AND tier !=
'propagation'`, so manual tags automatically become propagation seeds.
Hand-tagging six privacy people teaches the follow-graph job to find more like
them, with no new code.

## Improvements made to this design before starting

| First instinct | Problem | Now |
|---|---|---|
| Delete removes the tag | Seeded slugs resurrect empty on the next classify run | Archive; the surviving row is what blocks reseeding |
| Rename changes the slug too | `seed_groups` recreates the old slug — two of everything | `name` only; slugs are permanent |
| Build the digest piece | An arrival has no hand-tags by definition and may have no rule hits | Gated on a measurement against production |
| Measure that gate locally | The only local DB is an empty gitignored snapshot; it would have "proved" 0% | Deferred to ubuntuplex, stated as unknown |
| Untag deletes the row | Loses the machine's reasoning, and the rule re-adds the person next run | `confirmed = 0`; evidence survives the disagreement |
| Copy the referer sanitiser | Five new write routes, five copies | Extract the helper out of `follow_back` |
| Batch UI needs JavaScript | A build step sonde does not have | Repeated checkbox names and a `datalist`; JS only enhances |
| Archive only hides the tag | The jobs resolve groups by id and would keep adding members to it invisibly | Archived groups are skipped by classify, propagate and both discovery paths |
| Hand-tagging always writes `tier='manual'` | Overwrites the machine's evidence on a row it got right — confirming is not replacing | Only inserts are `manual`; confirming a derived row keeps its `tier` and `evidence` |
