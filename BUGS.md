# sonde — known bugs

When one is fixed, delete it — `git log` is the record of what was wrong, and a
list of struck-through entries is a list nobody reads.

Triaged 2026-07-29 by auditing the codebase rather than recalling it: every
template variable cross-checked against the context its route passes, every
link and form action resolved against the route table (30 targets, all valid),
every route hit against a production snapshot including hostile parameters (no
500s), job wiring and store references checked, and the chip interaction driven
in a real browser.

Everything below is **reproduced, not suspected**. Where a number appears it was
measured against the 2026-07-29 snapshot.

Ranked by what it costs to leave alone.

BUG-12 to BUG-14 were found in a security review on 2026-08-04 and reproduced
with throwaway tests before being written down. The reproductions are quoted
below rather than described, because two of them look survivable until you see
the output.

### P0 — permanent data loss, and a redirect that leaves the site

**BUG-14 · Open redirect on notice dismissal.** `dismiss_notice`
(`app.py:913`) validates its `back` parameter with `back.startswith("/")`,
which passes scheme-relative URLs. Reproduced:

```
POST /notices/backup_failing/abc/dismiss?back=//evil.example/phish
→ 303  Location: //evil.example/phish
```

Starlette's quoting leaves `//` intact, and a browser resolves that off-site.
This is the exact bypass `_safe_back`'s docstring documents at length — the
route simply does not call it. Behind Authelia today, so the reachable harm is
small; it is P0 because the fix is one line and the same mistake is already
written down as fixed.
*Fix:* call `_safe_back`.

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

**BUG-11 · Institution matching is switched off in production.** `institutions`
and `institution_roster` are both empty in the 2026-07-29 snapshot, so
`institution_name` is NULL for all 10,038 followers and the M6/M15a/M20 path
contributes nothing. Handle-domain matching — worth 0.95 confidence, the
strongest employer signal sonde has — therefore never fires. Employer coverage
is currently 41 people, all of it Wikidata `affiliations`. Found while
investigating byline sources: the question "which outlet does this journalist
write for" is unanswered mostly because a feature that answers it is dormant.
*Fix:* find out why seeding never ran — `seed_institutions()` exists — then run
it and the matcher. Check whether this is a fresh-deploy gap or something that
wiped.

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

