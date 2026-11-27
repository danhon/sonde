# A designed HTML alert email, with pictures — design

**Date:** 2026-11-26
**Status:** implemented, not yet deployed
**Issue:** sonde#16

Both alert emails are plain text. This gives them an HTML alternative with
avatars, and makes the like/repost alert and the arrival alert look like one
family. The plain-text part is unchanged, because it is what a watch, a lock
screen and a text-only client will show.

## What exists

`sonde/notify/alerts.py` (a notable arrival) and
`sonde/notify/interaction_alerts.py` (every notable like or repost) each build
an `EmailMessage` with `set_content(text)` and nothing else. `sonde/notify/digest.py`
is the only email with `add_alternative(..., subtype="html")`; its HTML is bare
`<p>`/`<ul>` with no styling, so it is prior art for *where* the alternative
goes, not for what it should look like.

Both alert emails hang off `scheduler._alert_after_sweep()`, which runs after
every head sweep (15 minutes) and every full sweep. Each kind records a run of
its own (`registry.run("alerts", …)`, `registry.run("like-alerts", …)`), so the
database already knows when each last attempted a send. Claims are written
before the send (`verified_alerts`, `interaction_alerts`); sending runs outside
`sync/runner.py` so SMTP latency can't extend a sweep; nothing is sent
23:00–07:00 Pacific.

The arrivals cap is `settings.verified_alert_max_listed` (10); the
like/repost renderer has no cap, because it reports every event rather than a
sample.

### Measured, 2026-11-26, against the production database and CDN

- **13,137 stored avatar URLs, every one unsuffixed** (`…/plain/<did>/<cid>`).
  The CDN serves **WebP** by default: 6.6 KB to 177 KB, median around 40–50 KB.
  `@jpeg` yields a real JPEG of similar size. There is **no resize**: `?width=`
  and `?w=` are ignored and `@s96` is a 400.
- **Traffic:** 16 like/repost emails in two days, median 1 person, maximum 4.
  Five arrival emails in ten days, always one person. A 30-minute window should
  therefore hold at most about 8.
- **Gmail clips a message over 102 KB** (measured by every ESP; most say the
  HTML part counts and externally-hosted images don't, but CID parts are
  ambiguous). Four unshrunk avatars would be 200–700 KB.
- **Re-encoded, an avatar is small:** the same six at 64×64 JPEG quality 80
  come to 0.7–1.9 KB each, so twelve are about 20 KB once base64'd. The 60 KB
  message budget holds with room to spare.
- **Neither alert query selects `avatar_url`** — `alert_candidates` and
  `interaction_alert_candidates` both select `a.description` and not the
  avatar; `events_since` and `recent_changes` do carry it.
- **MIME:** `add_related()` must be called on the **HTML part**, not on the
  message (`Cannot convert alternative to related`), and passing `filename=`
  sets `Content-Disposition: attachment` — the paperclip. Called without a
  filename, the part comes out `inline`, which is what renders.
- **Dark mode:** `prefers-color-scheme` works in Apple Mail; Gmail ignores it
  and force-inverts instead. There is no design that gives Gmail a dark mode.

## Approaches considered

**Images.** *Inline CID* (no third-party load, renders without "Display
images", message grows with each person unless shrunk). *Remote from
`cdn.bsky.app`* (tiny message, but Gmail hides it for an untrusted sender and
every open tells Bluesky's CDN something was read). *Remote through sonde*
(a cache we control, no third-party ping, but a second unauthenticated route on
an app whose only guard for `/api/v1` is `PathPrefix`). *Initials only* (safe
everywhere, no pictures).

Chosen: **inline CID**, shrunk with Pillow. Remote-through-sonde was ruled out
because it reopens the containment rule; remote-from-Bluesky is the fallback we
would take if Pillow were refused, and initials are what that fallback degrades
to.

**Look.** *Replicate sonde* (slate background, dark variant for Apple Mail),
*email-native light* (white, system sans, no dark variant), *sonde-light*
(sonde's palette and mono headings on white, no dark variant).

Chosen: **sonde-light**. A pale slate background reads as a grey box under
Gmail's inversion, and maintaining two designs for one reader is cost without
payoff.

**Cadence.** *A gate on the send* (one schedule, the sweeps, still drive
everything) or *a cron at :00/:30* (a second schedule, with its own misfires and
overlaps, that can no longer fire straight after the sweep that found the
event).

Chosen: **the gate**. Not more than one email every 30 minutes.

## Decisions

**Avatars are embedded, 64×64 JPEG, initials when there is no picture.** The
fetched bytes are decoded and re-encoded by Pillow, never passed through: the
re-encode is also what strips metadata from an image anyone can upload. A
missing URL, a timeout, a failed decode or a row past the avatar budget renders
initials instead, and the email still goes.

**No dark variant.** Gmail will invert what we send and there is nothing to do
about it, so the design is one light layout that survives inversion: no pure
white, no pure black, red used for text and rules rather than fills.

**One module, two shapes.** Cards with a post and its people for likes and
reposts; a flat list of people under a heading for arrivals. They share the
person row, the footer, the plain-text part and the sender.

**Self-sufficient.** Everything is readable in place: name, handle, badge, verb,
Pacific time, first line of the bio. Links go to the post, the person on
Bluesky and the person in sonde, but nothing needs following to make sense.

**The plain-text part and the subject are unchanged.** The subject stays
counts-only, because it shows on a lock screen.

**Caps match the text, and the HTML hides nobody the text wouldn't.** Arrivals
cap at `verified_alert_max_listed`, now 12 to match the avatar budget;
like/repost alerts stay uncapped, because capping would silently drop events
the email exists to report. Both are bounded by the window in practice: about 8
at 30 minutes.

**One gate, both kinds, 30 minutes.** `ALERT_INTERVAL_MINUTES`, default 30,
beside the sweep settings. The gate keys on when an email actually left — the
newest `sent_at` across both claim tables — so a failed send is retried on the
next sweep and a restart cannot reset it. The age is measured when a sweep
begins, always a few seconds short of the interval, so the comparison allows
five minutes of slack; without it every email would wait a third sweep. Both kinds share the one gate: a like
email therefore holds a notable arrival back for up to 30 minutes, which the
digest backstops. The notification *read* inside `run_interaction_alerts` is not
gated: the digest's notable-likes section needs those rows at 15 minutes, and it
costs no email.

**Retries stay at three attempts.** A failed send is not an email, so the gate
does not hold it back: the next head sweep tries again, and three attempts span
about 45 minutes. The digest is the backstop and already reports the failure.

**No caching of avatar bytes.** At most a handful of new people per email, and
a cache means a table, storage growth and invalidation to keep right.

**Any https avatar host, 2 MB cap.** `cdn.bsky.app` is where all 13,137 stored
avatars are served today, but that was measured over followers; a notable
stranger on another PDS can carry a different host. The fetcher accepts any
https URL, stops at 2 MB, and treats anything else — http, oversized, a
redirect to neither — as no picture. In the email this still means nothing
loads from a third party: the bytes are ours by then.

**The two `send()` functions stay as they are.** A shared renderer is the point;
merging the senders would touch the SMTP paths this change isn't about.

**Nothing loads from a third party.** No remote `src`, no tracking pixel, no
webfont. The HTML references only `cid:` and sonde's own URLs.

## Budgets, asserted by tests

- Every `<img>` is a `cid:` reference; every reference has exactly one part and
  every part is referenced.
- The HTML part stays under 24 KB and the whole message under 60 KB, so the
  102 KB clip cannot come back through a template that grows. The overnight
  batch — likes held through quiet hours, up to 07:00 — is not capped, so a
  separate test keeps 30 events under the clip as well.
- At most 12 avatars; past that, initials.
- 5 s per avatar, 8 s total, and the send proceeds regardless.

## What this does not do

Redesign the digest (its bare HTML stays; adopting the new renderer is a
separate change). Add a dark mode Gmail can use. Serve or proxy avatars from
sonde. Cache images. Change sweep cadence, the digest's 14:00 slot, the
quiet-hours window, the watermarks or the claim-before-send order. Change the
subject lines or the plain-text parts.

## Left to the builder

The markup itself: table-based layout versus inline-block, the exact row grid,
how initials are drawn (a coloured circle with a letter, and where the colour
comes from). The subject wording for arrivals. Whether the badge text reuses
`describe()`'s `why_notable()` wording verbatim. The module split under
`sonde/notify/`, and the flags on `make email-preview`.

`make email-preview` draws its avatars with Pillow rather than fetching any:
the preview must contain nobody real, so initials and generated circles are the
only pictures in it.

## Verification

- Tests: the existing like-alert run and arrival run with `send` captured —
  HTML part exists, every event escaped, the plain-text part byte-identical to
  today, no `http` image source, every `cid:` matched, budgets held, the gate
  skips a second attempt inside 30 minutes and allows one after.
- `make email-preview` renders both shapes from made-up data into a mode-700
  temp file for the operator to read on a phone. Nothing committed (SHOT-2).
- One live send before the scheduler is left to it, then a real alert.
