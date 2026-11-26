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

Re-scanned 2026-08-31 against the OWASP Top 10, concentrating on the surfaces
added since that review — the token-guarded API, which had never been reviewed,
and the page the operator's browser actually runs. Six findings came from it;
two — the API's 500 on a non-ASCII bearer token, and the uncapped container log
that made it expensive — were fixed the same day and are gone from this list,
leaving BUG-20, BUG-21, BUG-23 and BUG-24. BUG-20 was fixed on 2026-11-22
(sonde#6), along with all of BUG-21 except `Cache-Control`, which followed
the same evening with BUG-15, BUG-17 and BUG-18. Every finding was reproduced against
running code before being written here, and one candidate was checked and
withdrawn; HISTORY.md says which, and what the scan covered and found clean.

Re-checked 2026-11-26 against the production database and `main`, and the ones
that are ready to be worked were mirrored into the issue tracker, each naming
its issue below. Two entries did not survive the check. BUG-24's test had already been fixed in `a9bcf7b`, so it is
gone. BUG-11's cause was wrong — the institution tables were seeded four days
after the snapshot it measured — and it is rewritten here with what the data
actually shows.

Everything below is **reproduced, not suspected**, except where an entry says
it is preventive. Where a number appears it was measured against the 2026-07-29
snapshot unless the entry says otherwise.

Ranked by what it costs to leave alone.

### P1 — wrong behaviour, silently

**BUG-01 · Typing an archived circle's name on a profile does nothing, and says
nothing.** `_apply_tags` gets `status: "archived"` from `create_group` and
redirects with no message, because a profile has no notice surface — `/circles`
has one, which is why it was never noticed there. The operator believes they
tagged someone and did not. Reproduced: `POST /followers/{did}/tags` with an
archived name returns 303, writes nothing, and the page says nothing.
*Fix:* give the profile the `_notices.html` treatment, or pass the outcome back
as a query parameter the way `/circles` does. **sonde#17.**

### P2 — misleading, or friction the design meant to remove

**BUG-11 · Handle-domain institution matching matches nobody, because followers
use their own domains.** Rewritten 2026-11-26; the original cause — "seeding
never ran" — was wrong, and its fix would have changed nothing. Measured against
the production database: `institutions` holds 22 rows and `institution_roster`
810, seeded 2026-08-02, four days *after* the 2026-07-29 snapshot the entry
measured. `institution_name` is set for 59 current followers — 38 `claimed`, 6
`claimed (verified)`, 15 `attested` — so the path contributes. What contributes
nothing is the `domain` method: zero actors, in any state, out of 13,581.

That is not a seeding gap, it is the population. Across every seeded domain,
exactly **one** current follower's handle sits on an institution domain
(`tomhannen.ft.com`), and `attested` at 1.0 outranks `domain` at 0.95 for it.
1,876 followers have custom handles; they are their own domains, not their
employer's. `_handle_matches_domain` encodes an assumption the data does not
support, and the 0.95 signal described as "the strongest employer signal sonde
has" has almost nothing to match.
*Fix (needs a decision):* take the domain from the actor's bio links rather than
the handle, where employer domains actually appear, or drop the `domain` method
and stop calling it the strongest signal. This one stays here rather than in
the tracker: choosing between those is a design call, not a piece of work.

**BUG-02 · Batch tagging still bounces you to the top of the list.** M32 fixed
this for profiles by redirecting to `#circles`; the batch bar still redirects to
`#tagged`, and nothing on any page has that id. Confirmed by grep. Less painful
than the profile case only because the bar is `position: sticky`.
*Fix:* give the batch tables an anchor and pass it, exactly as the profile
route now does. **sonde#18.**

**BUG-03 · Restoring a merged circle leaves it claiming it was merged.**
`archive_group(slug, archived=False)` clears `archived_at` but not
`merged_into`, so a restored circle is live again while still recording that its
people went elsewhere — and they are now in both. Reproduced. Nothing warns
before restoring a merged circle.
*Fix (decided 2026-11-24, see GLOSSARY.md "Merged circle"):* a merged circle
cannot be restored. Refuse the restore and show where its people went; undoing
a merge means making a new circle. **sonde#19.**

**BUG-04 · Dead phrase candidates accumulate forever.** Discovery only inserts
and updates; nothing prunes. 146 candidates are undecided on 2026-11-26 and 68
of those have `member_count = 0` — up from the 18 measured on 2026-07-29, so the
number grows with every discovery run. Re-running "Find new circles" will not
remove them: the corrected rules stop them being *proposed again*, but the rows
stay. They have to be rejected by hand.
*Fix:* a job that rejects undecided candidates matching nobody under current
rules. Bounded and auditable, and it must leave decided ones alone. **sonde#20.**

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
**sonde#21.**

### P3 — cosmetic, dead, or preventive

**BUG-23 · The SMTP password and the GitHub token are fields on the object
passed into every template render.** `apikey.py` states the invariant in its own
docstring — the API secret is deliberately kept off `Settings` because
"`Settings` is passed into every template render, so a field on it is one
`{{ settings }}` away from being displayed" — and `Authenticator.status()` goes
to the same trouble for the app password. `settings.smtp_password` and
`settings.github_token` are ordinary fields on that frozen dataclass anyway, and
that dataclass is in the context of every page. Nothing renders them today:
all eighteen `settings.` references in the templates name a specific attribute,
twelve distinct ones, every one benign. So this is preventive — and it is one
`{{ settings }}`, one debug dump or one `settings.__dict__` loop away from not
being.
*Fix:* read both from `os.environ` where they are used, the way `apikey.clients()`
does. Or keep the fields and add the test that no template renders `settings`
whole — but the first is the rule the codebase already wrote down. **sonde#22.**

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

