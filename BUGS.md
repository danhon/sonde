# sonde — known bugs

When one is fixed, delete it — `git log` is the record of what was wrong, and a
list of struck-through entries is a list nobody reads.

Triaged 2026-07-29 by auditing the codebase rather than recalling it: every
template variable cross-checked against the context its route passes, every
link and form action resolved against the route table (30 targets, all valid),
every route hit against a production snapshot including hostile parameters (no
500s), job wiring and store references checked, and the chip interaction driven
in a real browser.

Re-triaged 2026-08-04 by a security review of the web, API and store layers.
Three defects from it (BUG-12 to BUG-14) were fixed the same day and are gone
from this list; what that review found and deliberately left is in HISTORY.md,
including the things checked and found clean, so they are not re-audited.

Everything below is **reproduced, not suspected**, except where an entry says
it is preventive. Where a number appears it was measured against the 2026-07-29
snapshot.

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

**BUG-16 · `/export.csv` writes formulas straight through.** `handle` and
`display_name` are chosen by the follower, and a leading `=`, `+`, `-` or `@`
makes a formula when the file is opened in Excel or Sheets. Reproduced — a
follower handle of `=cmd|'/C calc'!A1` lands in the CSV verbatim:

```
handle,display_name
=cmd|'/C calc'!A1,@SUM(1+1)
```

The export is operator-only, so this is someone else choosing what runs on your
machine, not a remote attack. It is the only route where a follower controls a
byte sequence that another program executes.
*Fix:* prefix a cell beginning `= + - @` or a control character with `'`. Do it
in the writer, not the query, or the same text leaks through the next exporter.

### P3 — cosmetic, dead, or preventive

**BUG-15 · `iter_follows` has no page cap** where `iter_followers` has one.
Both terminate only on a missing cursor, which is right (see the module
docstring). But `head_sweep_max_pages` exists because a cursor that never
advances would otherwise walk forever, and `iter_follows` has no equivalent —
so the same fault on `getFollows` spins indefinitely against an IP budget shared
house-wide with BlueBirdNET and atproto-labeler. Preventive: not observed, and
the reasoning for the cap on the other iterator is already written down in
`config.py`.
*Fix:* the same page cap, defaulted well above any real follow list.

**BUG-17 · `name` is interpolated into a redirect unencoded.**
`set_org_weight` returns `f"/institutions?name={name}"`, so an organisation
whose name contains `&`, `=` or `#` breaks out of its own query parameter.
Reproduced: `A&b=1#x` yields `Location: /institutions?name=A&b=1#x`, which the
page then reads as `name=A` plus a stray `b=1`. Not an open redirect — the path
is rooted, and `_safe_path` governs the routes that take a caller-supplied
target. Organisation names come from seeding and verification issuers rather
than free text, which is why nothing has hit it.
*Fix:* `urlencode` the parameter.

**BUG-18 · A weight of `nan` becomes the maximum weight.**
`set_organisation_weight` clamps with `max(0.0, min(1.0, weight))`, which
handles `inf`, `-5` and `1e308` correctly — all three were checked and stored
0.0 or 1.0. `nan` compares false against everything, so `min(1.0, nan)` returns
`1.0` and the clamp passes it through: garbage silently becomes the strongest
possible institution weight rather than being rejected. Reproduced.
*Fix:* reject a non-finite weight rather than clamping it.


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

### Open, but not fixable in this repository

**The Authelia cookie is shared by every service on the fleet.** It is scoped to
`domain: sgc.rayandhon.com` with the default `same_site: lax`, so every other
service on ubuntuplex is *same-site* with sonde. Writes are now refused unless
they come from sonde's own pages, but **reads are not** — an XSS in calibre-web,
homepage, docs, scrypted, birdnet-go or the watchdog can still fetch sonde's
pages with the operator's session and exfiltrate the follower data, the circles
and the hidden list.

Narrowing that means per-service cookie domains in Authelia, which lives in
`~/dev/reverse-proxy`, not here. Recorded so it is not mistaken for something
the same-origin middleware closed.

### Not a bug, but the biggest risk here

**M26 onwards had no independent review.** Twelve commits — previews, the
discovery repair, merging, the rename, bios, the build widget, weekly arrivals
and the chips — were written and reviewed by the same context, after a spend
limit ended subagent review partway through M25. The tests are real and the
load-bearing ones were deliberately falsified to check they fail. That is not
the same as another reader. Three bugs in that stretch reached `main` before
being caught here, two of them user-visible for several commits. A whole-branch
review over `661cf68..HEAD` is the single highest-value thing left.

