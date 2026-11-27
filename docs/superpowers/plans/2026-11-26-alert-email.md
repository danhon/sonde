# Alert emails as designed HTML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Both alert emails gain an HTML alternative with embedded 64 px avatars, in one designed layout, and no more than one of them is sent every 30 minutes.

**Architecture:** A fetcher (`sonde/notify/avatars.py`) turns each row's `avatar_url` into small JPEG bytes; a renderer (`sonde/notify/email_html.py`) turns rows into HTML plus `cid:` references and attaches the parts with `add_related()` called on the HTML part; each alert module keeps its existing `send()` seam and gains the alternative. The 30-minute cadence is one gate in `scheduler._alert_after_sweep()`, reading when an alert email last actually left.

**Tech Stack:** Python 3.12, stdlib `email.message.EmailMessage`, httpx, Pillow, pytest.

**Spec:** `docs/superpowers/specs/2026-11-26-alert-email-design.md`

## Global Constraints

- Pillow is added with `uv add`, at a release at least three days old (IMG-2), and `uv.lock` is committed in the same commit.
- The plain-text part of both emails stays **byte-identical** to today's `render_text()` output. The subject is unchanged.
- Nothing in the HTML loads from a third party: every `<img src>` is `cid:`.
- HTML part under 20 KB; whole message under 60 KB; at most 12 avatars.
- 5 s per avatar fetch, 8 s total; any failure, timeout, oversize or non-https URL means initials, and the email still sends.
- `add_related()` on the message raises; it must be called on the HTML part. Passing `filename=` produces `Content-Disposition: attachment`, which is the paperclip — don't.
- No test may reach the network. Stub `httpx` transports and the avatar fetcher.
- Run tests as `nice -n 19 uv run pytest -q ...` on ubuntuplex (decisions/009).
- Every commit ends with `Host: ubuntuplex` as its trailer.

## Review Focus

- An avatar URL that hangs or is huge: the send still goes, with initials for that person.
- A display name, handle or bio containing HTML, quotes, or a literal `cid:`: escaped, and the injectable text never becomes markup.
- A person whose picture could not be fetched: initials rendered, never a broken image box or an attachment.
- More people than the cap: "… and N more" with the same link, never a silent drop.
- A second email inside 30 minutes, including one triggered by the *other* kind having just sent.

---

### Task 1: Fetch and shrink avatars

**Files:**
- Create: `sonde/notify/avatars.py`
- Test: `tests/test_avatars.py`

**Interfaces:**
- Consumes: rows from `store.alert_candidates()` / `store.interaction_alert_candidates()`; each row has `did` and (after Task 3) `avatar_url`.
- Produces:
  - `shrink(raw: bytes, size: int = SIZE) -> bytes | None`
  - `async def avatars(rows: Sequence[dict], *, limit: int = MAX_AVATARS, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, bytes]` — keyed by `did`, JPEG bytes, in row order, capped.
  - Constants: `SIZE = 64`, `QUALITY = 80`, `MAX_AVATARS = 12`, `MAX_BYTES = 2_000_000`, `PER_IMAGE_SECONDS = 5.0`, `TOTAL_SECONDS = 8.0`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_avatars.py
import io
import httpx
import pytest
from PIL import Image

from sonde.notify import avatars as av

def png(size=(200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 40)).save(buf, "PNG")
    return buf.getvalue()

def transport_for(body: bytes, *, delay: float = 0.0, status: int = 200):
    async def handler(request):
        import asyncio
        await asyncio.sleep(delay)
        return httpx.Response(status, content=body)
    return httpx.MockTransport(handler)

def test_shrink_returns_a_64px_jpeg():
    out = av.shrink(png())
    assert out is not None and out[:2] == b"\xff\xd8"
    assert Image.open(io.BytesIO(out)).size == (64, 64)

def test_shrink_rejects_what_is_not_an_image():
    assert av.shrink(b"not an image") is None

async def test_rows_without_an_avatar_are_skipped_without_a_request():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, content=png())

    rows = [{"did": "did:plc:a", "avatar_url": None}, {"did": "did:plc:b"}]
    assert await av.avatars(rows, transport=httpx.MockTransport(handler)) == {}
    assert requests == []

async def test_http_urls_are_refused():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, content=png())

    rows = [{"did": "did:plc:a", "avatar_url": "http://example.com/a.jpg"}]
    assert await av.avatars(rows, transport=httpx.MockTransport(handler)) == {}
    assert requests == []

async def test_an_oversized_body_is_dropped():
    rows = [{"did": "did:plc:a", "avatar_url": "https://cdn.example/a.jpg"}]
    big = b"x" * (av.MAX_BYTES + 1)
    assert await av.avatars(rows, transport=transport_for(big)) == {}

async def test_a_slow_fetch_does_not_hold_the_send():
    rows = [{"did": "did:plc:a", "avatar_url": "https://cdn.example/a.jpg"}]
    got = await av.avatars(rows, transport=transport_for(png(), delay=av.TOTAL_SECONDS + 1))
    assert got == {}

async def test_the_cap_is_twelve_and_order_is_kept():
    rows = [{"did": f"did:plc:{i:02d}", "avatar_url": f"https://cdn.example/{i}.jpg"}
            for i in range(14)]
    got = await av.avatars(rows, transport=transport_for(png()))
    assert list(got) == [r["did"] for r in rows[:12]]

async def test_a_failed_fetch_does_not_cost_the_others():
    def handler(request):
        if request.url.path.endswith("/bad.jpg"):
            return httpx.Response(500)
        return httpx.Response(200, content=png())

    rows = [{"did": "did:plc:bad", "avatar_url": "https://cdn.example/bad.jpg"},
            {"did": "did:plc:good", "avatar_url": "https://cdn.example/good.jpg"}]
    got = await av.avatars(rows, transport=httpx.MockTransport(handler))
    assert list(got) == ["did:plc:good"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `nice -n 19 uv run pytest -q tests/test_avatars.py`
Expected: collection error, `No module named 'sonde.notify.avatars'`.

- [ ] **Step 3: Add Pillow, at a release older than three days**

Run `uv add pillow`, then check the resolved version's release date on PyPI and move to the newest release that is at least three days old (IMG-2). Commit `pyproject.toml` and `uv.lock`.

- [ ] **Step 4: Implement `sonde/notify/avatars.py`**

`shrink` opens the bytes with `Image.open`, `convert("RGB")`, `thumbnail((size, size), Image.LANCZOS)`, saves JPEG `quality=QUALITY, optimize=True`, returns `None` on any `PIL.UnidentifiedImageError`/`OSError`, and sets `Image.MAX_IMAGE_PIXELS` from the module so a decompression bomb raises instead of eating the box. `avatars` builds one `httpx.AsyncClient(timeout=PER_IMAGE_SECONDS, transport=transport, follow_redirects=True)`, takes the first `limit` rows with an https `avatar_url`, fetches them with `asyncio.gather(..., return_exceptions=True)` wrapped in `asyncio.wait_for(..., TOTAL_SECONDS)`; on `TimeoutError` it cancels what is pending and keeps finished results. A body over `MAX_BYTES` is dropped. Module docstring: why inline (no third-party load), why shrink (Gmail's clip), why re-encode (anyone can upload an avatar, so the bytes we embed are our own).

- [ ] **Step 5: Run the tests**

Run: `nice -n 19 uv run pytest -q tests/test_avatars.py`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock sonde/notify/avatars.py tests/test_avatars.py
git commit -m "Fetch alert avatars and shrink them to 64px JPEG (#16)"
```

### Task 2: The renderer

**Files:**
- Create: `sonde/notify/email_html.py`
- Test: `tests/test_email_html.py`

**Interfaces:**
- Produces:
  - `cid_for(did: str) -> str` → `"<{sha1(did).hexdigest()[:16]}@sonde>"`
  - `render_arrivals(rows: Sequence[dict], images: dict[str, bytes], *, limit: int | None = None) -> str`
  - `render_interactions(rows: Sequence[dict], previews: dict[str, str | None], images: dict[str, bytes]) -> str`
  - `attach(message: EmailMessage, html: str, images: dict[str, bytes]) -> EmailMessage` — the HTML alternative plus one `inline` part per referenced cid, added with `get_payload()[-1].add_related(...)`, no `filename=`.
  - `MARKUP_BUDGET = 20_000`
- Consumes: `alerts.why_notable()`, `alerts.issuers()`, `alerts._footer_link()` (arrivals); `interaction_alerts.describe()`, `.post_link()`, `._when()` (interactions) — import them, don't copy them.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_email_html.py
import pytest
from email.message import EmailMessage

from sonde.notify import email_html as h

ROW = {"did": "did:plc:a", "handle": "ana.example.com",
       "display_name": "Ana Example", "description": "Writes about trains.",
       "followers_count": 41_000, "verified_status": "valid",
       "trusted_verifier_status": "none", "influence_score": 12.0,
       "event": "followed", "detected_at": "2026-11-26T17:09:53.000Z"}
IMG = b"\xff\xd8\xff\xd9"

def test_a_person_with_a_picture_gets_a_cid_reference():
    html = h.render_arrivals([ROW], {ROW["did"]: IMG})
    assert f'src="cid:{h.cid_for(ROW["did"])}"' in html

def test_a_person_without_a_picture_gets_initials_not_an_image():
    html = h.render_arrivals([ROW], {})
    assert "<img" not in html and "A" in html

def test_injectable_text_is_escaped():
    nasty = {**ROW, "display_name": '<script>alert(1)</script>',
             "description": '"><img src="http://evil/x">'}
    html = h.render_arrivals([nasty], {})
    assert "<script" not in html and "http://evil" not in html

def test_every_image_is_a_cid_reference():
    html = h.render_arrivals([ROW], {ROW["did"]: IMG})
    for tag in html.split("<img")[1:]:
        assert tag.split(">")[0].count('src="cid:') == 1

def test_the_arrival_cap_hides_nobody():
    rows = [{**ROW, "did": f"did:plc:{i}"} for i in range(14)]
    html = h.render_arrivals(rows, {}, limit=12)
    assert "and 2 more" in html

def test_the_markup_budget_holds_at_twelve_people():
    rows = [{**ROW, "did": f"did:plc:{i}"} for i in range(12)]
    html = h.render_arrivals(rows, {r["did"]: IMG for r in rows},
                             limit=12)
    assert len(html.encode()) < h.MARKUP_BUDGET

def test_interactions_group_by_post_and_link_to_it():
    rows = [
        {"did": "did:plc:a", "kind": "like", "subject": "at://x/app.bsky.feed.post/1",
         "occurred_at": "2026-11-26T17:09:53.000Z", **{k: ROW[k] for k in
         ("handle", "display_name", "description", "followers_count",
          "verified_status", "trusted_verifier_status")}},
    ]
    html = h.render_interactions(
        rows, {"at://x/app.bsky.feed.post/1": "The bus timetable is a love letter"},
        {ROW["did"]: IMG})
    assert "The bus timetable" in html and "bsky.app/profile/ana.example.com" in html

def test_attach_adds_inline_parts_with_no_filename():
    message = EmailMessage()
    message.set_content("plain")
    h.attach(message, h.render_arrivals([ROW], {ROW["did"]: IMG}), {ROW["did"]: IMG})
    images = [p for p in message.walk() if p.get_content_type() == "image/jpeg"]
    assert len(images) == 1
    assert images[0]["Content-Disposition"].startswith("inline")
    assert "filename" not in images[0]["Content-Disposition"]
    assert message.get_body(("html",)) is not None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `nice -n 19 uv run pytest -q tests/test_email_html.py`
Expected: `No module named 'sonde.notify.email_html'`.

- [ ] **Step 3: Implement `sonde/notify/email_html.py`**

Table-based layout with inline CSS only. Shared pieces: `_person_row(row, images)`, `_initials(row)` (first letters of display name, else handle; a background colour from a hash of the did), `_footer(rows)`. `render_arrivals` uses `alerts.describe` terms but renders them as markup, applying `limit` with the text's own "… and N more" and `alerts._footer_link(rows)`. `render_interactions` groups rows by `subject` in first-seen order, one card per post with `interaction_alerts.post_link(uri)` and the escaped preview truncated to 160 characters. Module docstring: the add_related/`filename`/Gmail-clip facts from the spec, so they aren't rediscovered.

- [ ] **Step 4: Run the tests**

Run: `nice -n 19 uv run pytest -q tests/test_email_html.py`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add sonde/notify/email_html.py tests/test_email_html.py
git commit -m "Render both alert emails as designed HTML with initials fallback (#16)"
```

### Task 3: Wire the HTML part into both emails

**Files:**
- Modify: `sonde/notify/alerts.py` (`build_message`, `send`)
- Modify: `sonde/notify/interaction_alerts.py` (`_alert`, `build_message`)
- Modify: `sonde/db/store.py` (`alert_candidates` ~4285, `interaction_alert_candidates` ~4407: add `a.avatar_url` to both SELECTs)
- Test: `tests/test_alerts.py`, `tests/test_interaction_alerts.py`

**Interfaces:**
- `alerts.build_message(rows, images: dict[str, bytes] | None = None) -> EmailMessage`
- `interaction_alerts.build_message(rows, previews, images: dict[str, bytes] | None = None) -> EmailMessage`
- Both keep `async def send(...)` as the test seam: arrivals `send(rows)` fetches, builds, sends; `_alert` in interactions fetches before building and then calls the unchanged `send(message)`.
- Module-level name `avatars` in both modules (`from sonde.notify.avatars import avatars`) so tests can patch it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_alerts.py — add below `arrival()`
IMG = b"\xff\xd8\xff\xd9"

def rows_for(did: str = "did:plc:v") -> list[dict]:
    return [{"event_id": 1, "did": did, "event": "followed",
             "detected_at": "2026-11-26T17:09:53.000Z", "handle": "v.bsky.social",
             "display_name": None, "description": None, "followers_count": 10,
             "influence_score": 0.0, "verified_status": "valid",
             "trusted_verifier_status": "none", "verifications": "[]",
             "attempts": 0, "avatar_url": "https://cdn.example/a.jpg"}]

async def test_candidates_carry_the_avatar_url(outbox):
    await started()
    await arrival("did:plc:v", avatar="https://cdn.example/a.jpg")
    rows = await store.alert_candidates(
        since_iso="2000-01-01T00:00:00+00:00", after_event_id=0)
    assert rows[0]["avatar_url"] == "https://cdn.example/a.jpg"

def test_the_email_carries_an_html_alternative():
    message = alerts.build_message(rows_for(), {"did:plc:v": IMG})
    assert message.get_body(("html",)) is not None

def test_the_plain_text_part_is_unchanged():
    message = alerts.build_message(rows_for(), {})
    assert message.get_body(("plain",)).get_content() == alerts.render_text(rows_for())

def test_every_avatar_reference_has_its_part_and_vice_versa():
    message = alerts.build_message(rows_for(), {"did:plc:v": IMG})
    html = message.get_body(("html",)).get_content()
    parts = {p["Content-ID"].strip("<>")
             for p in message.walk() if p.get_content_type() == "image/jpeg"}
    referenced = {piece.split('"')[0] for piece in html.split("cid:")[1:]}
    assert parts == referenced

def test_the_message_stays_under_sixty_kb_with_twelve_avatars():
    rows = [{**rows_for()[0], "did": f"did:plc:{i}"} for i in range(12)]
    message = alerts.build_message(rows, {r["did"]: IMG for r in rows})
    images = [p for p in message.walk() if p.get_content_type() == "image/jpeg"]
    assert len(images) == 12
    assert len(message.as_bytes()) < 60_000
```

In `tests/test_interaction_alerts.py`, the same three shapes: `test_the_like_email_carries_an_html_alternative`, `test_the_plain_text_part_is_unchanged` (`message.get_body(("plain",)).get_content() == ia.render_text(rows, previews)`), and `test_a_run_attaches_an_avatar` — patch `ia.avatars` to `async def fake(rows): return {did: IMG}`, run the alert, and assert the captured message walks to one `image/jpeg` with an `inline` disposition. Add that same `monkeypatch.setattr(ia, "avatars", fake)` to the existing `outbox` fixture (and the equivalent to `test_alerts.py`'s) so no existing test can reach the network.

- [ ] **Step 2: Run them and watch them fail**

Run: `nice -n 19 uv run pytest -q tests/test_alerts.py tests/test_interaction_alerts.py`
Expected: FAIL — `build_message() takes 1 positional argument` / no html body.

- [ ] **Step 3: Implement**

In both modules, `build_message` keeps `set_content(render_text(...))` untouched, then `email_html.attach(message, html, images or {})`. In `alerts.send`, `images = await avatars(rows)` before building. In `interaction_alerts._alert`, `images = await avatars(rows)` before `build_message(rows, previews, images)`. Add `a.avatar_url` to the two SELECTs. Update the module docstrings: the plain-text part is unchanged because a watch and a lock screen read it.

- [ ] **Step 4: Run the suites**

Run: `nice -n 19 uv run pytest -q tests/test_alerts.py tests/test_interaction_alerts.py tests/test_digest.py`
Expected: all pass, including the existing untouched tests.

- [ ] **Step 5: Commit**

```bash
git add sonde/notify/alerts.py sonde/notify/interaction_alerts.py sonde/db/store.py tests/test_alerts.py tests/test_interaction_alerts.py
git commit -m "Both alert emails send an HTML part with their avatars (#16)"
```

### Task 4: One email every 30 minutes

**Files:**
- Modify: `sonde/config.py` (beside `head_sweep_minutes` and `verified_alert_max_listed`)
- Modify: `sonde/db/store.py` (new function beside `last_run_ages`)
- Modify: `sonde/scheduler.py` (`_alert_after_sweep`)
- Test: `tests/test_scheduler_alerts.py`

**Interfaces:**
- `settings.alert_interval_minutes: int` from `ALERT_INTERVAL_MINUTES`, default 30.
- `async def last_alert_email_at() -> datetime | None` — the newest `sent_at` across `verified_alerts` and `interaction_alerts`, timezone-aware UTC, or `None`.
- `_alert_after_sweep()` returns early when `now - last_alert_email_at() < alert_interval_minutes`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scheduler_alerts.py — the db fixture is test_alerts.py's, copied
import pytest
from datetime import datetime, timedelta, timezone

from sonde import scheduler
from sonde.config import settings
from sonde.db import store

@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "gate.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)

@pytest.fixture
def calls(monkeypatch):
    """Record which alert jobs the sweep would have run."""
    seen = []

    async def arrivals():
        seen.append("arrivals")

    async def likes():
        seen.append("likes")

    monkeypatch.setattr(scheduler, "run_alerts", arrivals)
    monkeypatch.setattr(scheduler, "run_interaction_alerts", likes)
    return seen

async def sent_arrival(minutes_ago: int) -> None:
    """One arrival that really was emailed, then aged."""
    await store.upsert_actor({"did": "did:plc:v", "handle": "v.bsky.social",
                              "verification": {"verifiedStatus": "valid",
                                               "trustedVerifierStatus": "none",
                                               "verifications": []}})
    await store.mark_seen("did:plc:v", 0)
    await store.add_event("did:plc:v", "followed")
    await store.commit()
    rows = await store.alert_candidates(since_iso="2000-01-01T00:00:00+00:00",
                                        after_event_id=0)
    await store.claim_alerts(rows)
    await store.mark_alerts_sent([r["event_id"] for r in rows])
    db = await store._db()
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    await db.execute("UPDATE verified_alerts SET sent_at = ?", (when,))
    await store.commit()

async def test_the_first_email_goes(calls):
    await scheduler._alert_after_sweep()
    assert calls == ["arrivals", "likes"]

async def test_a_second_email_inside_thirty_minutes_is_skipped(calls):
    await sent_arrival(minutes_ago=5)
    await scheduler._alert_after_sweep()
    assert calls == []

async def test_an_email_after_thirty_minutes_goes(calls):
    await sent_arrival(minutes_ago=31)
    await scheduler._alert_after_sweep()
    assert calls == ["arrivals", "likes"]

async def test_the_interval_is_a_setting(calls, monkeypatch):
    monkeypatch.setitem(settings.__dict__, "alert_interval_minutes", 60)
    await sent_arrival(minutes_ago=45)
    await scheduler._alert_after_sweep()
    assert calls == []

async def test_a_failed_send_does_not_hold_the_next_one(calls):
    await sent_arrival(minutes_ago=2)
    db = await store._db()
    await db.execute("UPDATE verified_alerts SET sent_at = NULL, error = 'boom'")
    await store.commit()
    await scheduler._alert_after_sweep()
    assert calls == ["arrivals", "likes"]
```

One kind holding the other back is what `sent_arrival` + `calls == []` already proves; add a likes row with `sent_at` 5 minutes ago for the reverse direction if a reviewer asks.

- [ ] **Step 2: Run them and watch them fail**

Run: `nice -n 19 uv run pytest -q tests/test_scheduler_alerts.py`
Expected: FAIL — sends happen every time.

- [ ] **Step 3: Implement**

`config.py` gets `alert_interval_minutes` in the same style as `head_sweep_minutes`. `store.last_alert_email_at` is one query: `SELECT MAX(at) FROM (SELECT MAX(sent_at) AS at FROM verified_alerts UNION ALL SELECT MAX(sent_at) FROM interaction_alerts)`. `_alert_after_sweep` reads it, computes `(datetime.now(timezone.utc) - last).total_seconds() < settings.alert_interval_minutes * 60`, logs at debug and returns. Update the docstring: the gate is "not more than one email every N minutes", keyed on when an email actually left, so a failed send retries on the next sweep.

- [ ] **Step 4: Run the tests**

Run: `nice -n 19 uv run pytest -q tests/test_scheduler_alerts.py tests/test_alerts.py tests/test_interaction_alerts.py`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sonde/config.py sonde/db/store.py sonde/scheduler.py tests/test_scheduler_alerts.py
git commit -m "Send at most one alert email every 30 minutes (#16)"
```

### Task 5: Raise the cap, and a preview you can look at

**Files:**
- Modify: `sonde/config.py` (`verified_alert_max_listed` default 10 → 12)
- Create: `sonde/notify/preview.py`
- Modify: `Makefile` (`email-preview` target, `.PHONY` line)
- Test: `tests/test_preview.py`

**Interfaces:**
- `python -m sonde.notify.preview --out DIR` writes `interactions.html` and `arrivals.html`, each a standalone page with `cid:` rewritten to `data:image/jpeg;base64,…`, prints the two paths.
- `def main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preview.py
def test_the_preview_is_standalone_and_contains_nobody_real(tmp_path):
    out = tmp_path / "preview"
    assert preview.main(["--out", str(out)]) == 0
    for name in ("interactions.html", "arrivals.html"):
        text = (out / name).read_text()
        assert "cid:" not in text and "data:image/jpeg;base64," in text
        assert "did:plc:" not in text
    assert (out.stat().st_mode & 0o777) == 0o700
    assert all((out / n).stat().st_mode & 0o077 == 0 for n in
               ("interactions.html", "arrivals.html"))
```

- [ ] **Step 2: Run it and watch it fail**

Run: `nice -n 19 uv run pytest -q tests/test_preview.py`
Expected: `No module named 'sonde.notify.preview'`.

- [ ] **Step 3: Implement**

Change the config default to 12. `preview.py` builds made-up rows (invented names, handles, bios, two fake posts), draws an avatar per person with Pillow (a filled circle and an initial), renders both shapes through `email_html`, swaps each `cid:` for a `data:` URI, creates `--out` with `mode=0o700`, writes both files `mode=0o600`. Makefile:

```make
.PHONY: test run css css-check email-preview

email-preview:
	uv run python -m sonde.notify.preview --out $$(mktemp -d -t sonde-email-preview-XXXXXX)
```

Module docstring: the preview contains nobody real (SHOT-2), and what it does and does not prove — it shows layout on a phone, not how Gmail renders it.

- [ ] **Step 4: Run the test, then render it yourself**

Run: `nice -n 19 uv run pytest -q tests/test_preview.py`, then `make email-preview` and open the two paths.

- [ ] **Step 5: Commit**

```bash
git add sonde/config.py sonde/notify/preview.py Makefile tests/test_preview.py
git commit -m "Twelve avatars an email, and make email-preview to look at it (#16)"
```

### Task 6: Write down what was learned

**Files:**
- Modify: `README.md` (`ENABLE_VERIFIED_ALERTS` line, the alert bullets, the env table)
- Modify: `CLAUDE.md` (a section beside the alerting invariants)
- Modify: `docs/superpowers/specs/2026-11-26-alert-email-design.md` (Status → implemented, and the gate wording if it changed)

- [ ] **Step 1: Fix the "verified" drift and document the new knobs**

README's `ENABLE_VERIFIED_ALERTS=true # instant email when a verified account follows` becomes notable, matching GLOSSARY and `store.notable_rule`; add `ALERT_INTERVAL_MINUTES=30`. CLAUDE.md gains the four facts a future change needs: `add_related` on the HTML part, `filename=` makes an attachment, Gmail clips at 102 KB so the budget is asserted, and the gate keys on when an email actually left.

- [ ] **Step 2: Check the suite and the committed CSS**

Run: `nice -n 19 make test && make css-check`
Expected: all pass (no template classes changed, so the CSS is expected to be unchanged).

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-11-26-alert-email-design.md
git commit -m "README says notable, not verified; the email facts move to CLAUDE.md (#16)"
```

### Task 7: Look, then let it run

- [ ] **Step 1: Look at the preview before anything is deployed** — `make email-preview`, open both on a phone. Layout, initials, colours, the bio line, the footer.
- [ ] **Step 2: Deploy** (`make deploy` on ubuntuplex, under `box-gate` and with a clean checkout per IMG-4).
- [ ] **Step 3: Watch one real alert go out**, and confirm in the mail client that the avatars render, the plain-text part is intact, and the next email is at least 30 minutes later.
- [ ] **Step 4: Close sonde#16** with a line saying which client showed what.
