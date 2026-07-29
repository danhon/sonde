# Groups as Tags Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make groups hand-editable — tag and untag one person or a hundred, create, rename and archive tags — while the existing classification rules keep running underneath as advisers whose output a human always outranks.

**Architecture:** No new tables. `group_members.tier = 'manual'` and `confirmed = 1` mark a hand decision; `confirmed = 0` marks a hand removal; every existing job already writes only to rows where `confirmed IS NULL`, so hand decisions survive by construction. Two new columns: `groups.archived_at` (delete-is-archive) and `group_members.decided_at` (when a human touched it). Batch UI is plain HTML forms — repeated checkbox names post as a list — with JavaScript only for a live selection count.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite, Jinja2, Tailwind via CDN, pytest + pytest-asyncio (`asyncio_mode = "auto"`), uv.

**Design doc:** `docs/superpowers/specs/2026-07-28-groups-as-tags-design.md`. Read it before starting — it records *why* archive rather than delete, and why rename cannot change a slug.

## Global Constraints

- **Run tests with `uv run pytest`.** Never bare `pytest`.
- **Never write to the real database.** `tests/conftest.py` has an autouse fixture that fails any test which opens `./sonde.db`. Use the `db` fixture.
- **Every Jinja import of a macro file must carry `with context`**, or `settings` is invisible inside the macro and controls silently vanish. `tests/test_follow_back.py:234` enforces this repo-wide.
- **Slugs are immutable.** `groups.slug` is both the URL key and the key `seed_groups()` upserts on. No task may write to it after creation.
- **Existing writers must not be loosened.** `classify_groups` deletes only `WHERE confirmed IS NULL AND tier != 'manual'` and updates only `WHERE group_members.confirmed IS NULL`; `propagate_groups`, `_apply_cluster_group` and `_apply_discovered_group` all use `ON CONFLICT DO NOTHING`. These are the guards the whole feature rests on.
- **Commit messages follow the repo, not conventional commits.** Look at `git log` — subjects are sentences like `M23: one-click follow-back, and sortable interaction columns`. No `feat:` prefixes. Every commit ends with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```
- **Milestone number is M25.**

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `sonde/db/schema.sql` | fresh-database shape | add two columns |
| `sonde/db/store.py` | all SQL | slugify helper, 6 new functions, 4 reads made archive-aware, 4 jobs made archive-aware, export column, tag filter |
| `sonde/groups.py` | classification rules and confidences | add `MANUAL = 1.0` |
| `sonde/web/app.py` | routes | `_safe_back` helper, 5 new routes, 2 routes extended |
| `sonde/web/templates/_tags.html` | **new** — tag chip, tag input, batch bar macros | created |
| `sonde/web/templates/detail.html` | one person | chips become editable |
| `sonde/web/templates/groups.html` | tag admin + members | batch bar, rename/archive/new, archived section |
| `sonde/web/templates/followers.html` | the main list | checkbox column, batch bar, tag filter |
| `sonde/web/templates/base.html` | shared JS | selection-count enhancement |
| `tests/test_tags.py` | **new** — everything in this plan | created |
| `tests/test_groups.py` | existing group behaviour | extended with the reclassify-survival pair |
| `pyproject.toml` | dependencies | add `python-multipart` |

Why a new `tests/test_tags.py` rather than growing `test_groups.py`: the existing file is about whether the *rules* classify correctly. This is about whether *hand decisions* stick. Different subject, and `test_groups.py` is already 160 lines.

---

### Task 1: The two new columns

New columns must go in **both** `schema.sql` (for fresh databases) **and** the `MIGRATIONS` dict (for production's existing one). Only doing the first is the classic version of this bug: tests pass on a fresh DB and ubuntuplex throws `OperationalError: no such column` on the first request after deploy.

**Files:**
- Modify: `sonde/db/schema.sql:172-193`
- Modify: `sonde/db/store.py:70-126` (the `MIGRATIONS` dict)
- Test: `tests/test_tags.py` (create)

**Interfaces:**
- Produces: `groups.archived_at TEXT` (NULL = live), `group_members.decided_at TEXT` (NULL = no human has touched this row)

- [ ] **Step 1: Write the failing test**

Create `tests/test_tags.py`:

```python
"""M25 — groups as tags: hand decisions outrank every job."""

from sonde.db import store


async def columns(table: str) -> set[str]:
    conn = await store._db()
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


async def test_a_fresh_database_has_the_new_columns(db):
    assert "archived_at" in await columns("groups")
    assert "decided_at" in await columns("group_members")


def test_the_new_columns_are_also_in_the_migration_list():
    """schema.sql only shapes a *new* database. Production's already exists, so
    a column missing from MIGRATIONS is an OperationalError on the first
    request after deploy — passing tests and a broken site."""
    assert ("archived_at", "TEXT") in store.MIGRATIONS["groups"]
    assert ("decided_at", "TEXT") in store.MIGRATIONS["group_members"]
```

- [ ] **Step 2: Run it and watch both fail**

Run: `uv run pytest tests/test_tags.py -v`
Expected: FAIL — `assert 'archived_at' in {...}` and `KeyError: 'groups'`

- [ ] **Step 3: Add the columns to schema.sql**

In `sonde/db/schema.sql`, the `groups` table becomes:

```sql
CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT,
    -- Delete is archive. The row surviving is exactly what stops seed_groups()
    -- re-inserting a seeded slug on the next classify run.
    archived_at TEXT
);
```

and `group_members` gains one column after `created_at`:

```sql
    created_at TEXT NOT NULL,
    -- When a human last tagged or untagged this row. created_at belongs to
    -- whichever job inserted it first, so it cannot answer "what did I tag
    -- this week".
    decided_at TEXT,
```

- [ ] **Step 4: Add both to the MIGRATIONS dict**

In `sonde/db/store.py`, inside the `MIGRATIONS` dict (alongside the existing `"my_follows"` and `"group_candidates"` entries), add:

```python
    "groups": [
        ("archived_at", "TEXT"),
    ],
    "group_members": [
        ("decided_at", "TEXT"),
    ],
```

- [ ] **Step 5: Run the tests and the full suite**

Run: `uv run pytest tests/test_tags.py -v && uv run pytest -q`
Expected: 2 passed, then the whole suite green (655 passing before this plan starts).

- [ ] **Step 6: Commit**

```bash
git add sonde/db/schema.sql sonde/db/store.py tests/test_tags.py
git commit -m "M25: columns for archiving a tag and dating a hand decision"
```

---

### Task 2: Create, rename, archive and restore

**Files:**
- Modify: `sonde/db/store.py` — add near the other group functions, after `seed_groups` (~line 2435)
- Modify: `sonde/db/store.py:2208` — `decide_candidate` reuses the extracted slugify
- Test: `tests/test_tags.py`

**Interfaces:**
- Produces:
  - `slugify(name: str) -> str`
  - `create_group(name: str) -> dict` with keys `status` (`"created" | "exists" | "archived" | "invalid"`), `slug` (`str | None`), `name` (`str`)
  - `rename_group(slug: str, name: str) -> bool`
  - `archive_group(slug: str, archived: bool = True) -> bool`
  - `archived_groups() -> list[dict]` with keys `slug`, `name`, `archived_at`, `members`
  - `group_names() -> list[dict]` with keys `slug`, `name` — live tags only, for the datalist

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tags.py`:

```python
# --------------------------------------------------- creating and naming

async def test_creating_a_tag_derives_a_slug_from_the_name(db):
    result = await store.create_group("Local government")
    assert result == {"status": "created", "slug": "local-government",
                      "name": "Local government"}


async def test_creating_a_tag_that_already_exists_says_so_rather_than_duplicating(db):
    await store.create_group("Local government")
    assert (await store.create_group("local GOVERNMENT"))["status"] == "exists"


async def test_a_name_that_slugifies_to_nothing_is_rejected(db):
    """'🎮' strips to the empty string, and a group with slug '' would collide
    with the next one that did the same."""
    assert (await store.create_group("🎮"))["status"] == "invalid"
    assert (await store.create_group("   "))["status"] == "invalid"


async def test_creating_a_tag_whose_slug_is_archived_reports_it(db):
    """The nastiest silent failure in the feature. ON CONFLICT DO NOTHING would
    do nothing, redirect happily, and leave the operator staring at a page with
    no new tag and no error."""
    await store.create_group("Local government")
    await store.archive_group("local-government")

    result = await store.create_group("Local government")

    assert result["status"] == "archived"
    assert result["slug"] == "local-government"


async def test_renaming_changes_the_name_and_never_the_slug(db):
    """The slug is the URL key AND the key seed_groups upserts on. Change it and
    the next classify run re-inserts the original, giving two of everything."""
    await store.create_group("Privacy and security")
    assert await store.rename_group("privacy-and-security", "Infosec")

    names = {g["slug"]: g["name"] for g in await store.group_summary()}
    assert names["privacy-and-security"] == "Infosec"


async def test_renaming_to_blank_is_refused(db):
    await store.create_group("Keep me")
    assert not await store.rename_group("keep-me", "   ")
    assert {g["name"] for g in await store.group_summary()} >= {"Keep me"}


# ------------------------------------------------------------ archiving

async def test_archiving_hides_a_tag_and_restoring_brings_it_back(db):
    await store.create_group("Temporary")
    await store.archive_group("temporary")
    assert "temporary" not in {g["slug"] for g in await store.group_summary()}

    await store.archive_group("temporary", archived=False)
    assert "temporary" in {g["slug"] for g in await store.group_summary()}


async def test_the_archived_list_shows_what_can_be_restored(db):
    await store.create_group("Temporary")
    await store.archive_group("temporary")

    rows = await store.archived_groups()

    assert [r["slug"] for r in rows] == ["temporary"]
    assert rows[0]["archived_at"]


async def test_the_datalist_offers_live_tags_only(db):
    await store.create_group("Alive")
    await store.create_group("Gone")
    await store.archive_group("gone")

    assert [g["slug"] for g in await store.group_names()] == ["alive"]
```

- [ ] **Step 2: Run them and watch every one fail**

Run: `uv run pytest tests/test_tags.py -v`
Expected: FAIL — `AttributeError: module 'sonde.db.store' has no attribute 'create_group'`

- [ ] **Step 3: Extract slugify and write the six functions**

In `sonde/db/store.py`, add after `seed_groups()`:

```python
# A tag name is a label, not prose. 64 is generous for the chip it renders as.
GROUP_NAME_MAX = 64


def slugify(name: str) -> str:
    """A stable URL key from a display name.

    Extracted from `decide_candidate`, which had this inline, so hand-made and
    discovered tags cannot drift into two different slug conventions.
    """
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:48]


async def create_group(name: str) -> dict:
    """Make a tag, and say what actually happened.

    The caller needs the distinction: an archived slug must offer restore rather
    than silently doing nothing, which is what a bare
    `INSERT ... ON CONFLICT DO NOTHING` would do.
    """
    name = (name or "").strip()
    slug = slugify(name)
    if not name or not slug:
        return {"status": "invalid", "slug": None, "name": name}

    db = await _db()
    async with db.execute(
        "SELECT slug, archived_at FROM groups WHERE slug = ?", (slug,)
    ) as cur:
        existing = await cur.fetchone()
    if existing is not None:
        return {"status": "archived" if existing["archived_at"] else "exists",
                "slug": slug, "name": name}

    await db.execute(
        "INSERT INTO groups (slug, name, created_at) VALUES (?,?,?)",
        (slug, name[:GROUP_NAME_MAX], utcnow()),
    )
    await db.commit()
    return {"status": "created", "slug": slug, "name": name}


async def rename_group(slug: str, name: str) -> bool:
    """Display name only — never the slug. See the module note on create_group."""
    name = (name or "").strip()
    if not name:
        return False
    db = await _db()
    cur = await db.execute(
        "UPDATE groups SET name = ? WHERE slug = ?", (name[:GROUP_NAME_MAX], slug))
    await db.commit()
    return bool(cur.rowcount)


async def archive_group(slug: str, archived: bool = True) -> bool:
    """Delete is archive: memberships are preserved and it can come back."""
    db = await _db()
    cur = await db.execute(
        "UPDATE groups SET archived_at = ? WHERE slug = ?",
        (utcnow() if archived else None, slug))
    await db.commit()
    return bool(cur.rowcount)


async def archived_groups() -> list[dict]:
    db = await _db()
    async with db.execute(
        """SELECT g.slug, g.name, g.archived_at, COUNT(m.did) AS members
             FROM groups g
             LEFT JOIN group_members m ON m.group_id = g.id
                   AND COALESCE(m.confirmed, 1) = 1
            WHERE g.archived_at IS NOT NULL
            GROUP BY g.id ORDER BY g.archived_at DESC"""
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def group_names() -> list[dict]:
    """Live tags, for the type-to-create datalist."""
    db = await _db()
    async with db.execute(
        "SELECT slug, name FROM groups WHERE archived_at IS NULL ORDER BY name"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]
```

Then in `decide_candidate` (`sonde/db/store.py:2208`), replace the inline slug line:

```python
    slug = re.sub(r"[^a-z0-9]+", "-", row["label"].lower()).strip("-")[:48]
```

with:

```python
    slug = slugify(row["label"])
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_tags.py -v`
Expected: the archiving tests still FAIL (`group_summary` does not filter archived yet — that is Task 3). Creating/renaming tests PASS.

If `test_archiving_hides_a_tag...` passes at this point, something is wrong — stop and check you are reading `group_summary` and not a stale DB.

- [ ] **Step 5: Commit the part that is done**

```bash
git add sonde/db/store.py tests/test_tags.py
git commit -m "M25: create, rename and archive a tag

Rename deliberately cannot touch the slug: it is both the URL key and the key
seed_groups upserts on, so changing it re-creates the original on the next
classify run and you have two of everything.

create_group reports 'archived' as a distinct outcome because the obvious
INSERT ... ON CONFLICT DO NOTHING silently succeeds against an archived slug,
leaving the operator with no new tag and no error."
```

---

### Task 3: Make every read archive-aware

The design's rule is that archive applies everywhere or nowhere. Seven read paths expose a group. Miss one and an archived tag surfaces in exactly one place.

**Files:**
- Modify: `sonde/db/store.py:2509` (`group_summary`), `:2535` (`group_members`), `:2556` (`groups_for`), `:1707` (`composition`)
- Test: `tests/test_tags.py`

**Interfaces:**
- Consumes: `archive_group` from Task 2
- Produces: no signature changes — the four existing functions gain a `WHERE` clause

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tags.py`:

```python
# ------------------------------------------- archive applies everywhere

async def surfaces_showing(slug: str) -> set[str]:
    """Every read path that can expose a group, by name.

    Table-driven on purpose: a surface added later and not listed here fails by
    omission rather than passing because nobody remembered to assert on it.
    """
    found = set()
    if slug in {g["slug"] for g in await store.group_summary()}:
        found.add("group_summary")
    if await store.group_members(slug):
        found.add("group_members")
    if slug in {g["slug"] for g in await store.groups_for("did:plc:j")}:
        found.add("groups_for")
    if slug in {g["slug"] for g in (await store.composition())["groups"]}:
        found.add("composition")
    if slug in {g["slug"] for g in await store.group_names()}:
        found.add("group_names")
    return found


async def test_an_archived_tag_disappears_from_every_read_path(db):
    """Membership comes from the rules here, not from tag_actor: this task is
    about the reads, and it must not depend on a writer Task 5 has not built."""
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    assert await surfaces_showing("journalists"), "precondition: visible first"

    await store.archive_group("journalists")

    assert await surfaces_showing("journalists") == set()


async def test_restoring_brings_the_members_back_untouched(db):
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()

    await store.archive_group("journalists")
    await store.archive_group("journalists", archived=False)

    assert [m["did"] for m in await store.group_members("journalists")] == ["did:plc:j"]
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/test_tags.py::test_an_archived_tag_disappears_from_every_read_path -v`
Expected: FAIL — after archiving, the tag is still visible in every surface, so
the assertion that the set is empty fails and names the surfaces still showing it.

- [ ] **Step 3: Add the filter to all four queries**

`group_summary` — add a `WHERE` before the `GROUP BY`:

```python
        """SELECT g.slug, g.name, g.id,
                  COUNT(m.did) AS members,
                  SUM(CASE WHEN m.confirmed IS NULL THEN 1 ELSE 0 END) AS unreviewed
             FROM groups g
             LEFT JOIN group_members m ON m.group_id = g.id
                   AND COALESCE(m.confirmed, 1) = 1
            WHERE g.archived_at IS NULL
            GROUP BY g.id ORDER BY members DESC, g.name"""
```

`group_members` — extend the existing `WHERE`:

```python
             WHERE g.slug = ? AND g.archived_at IS NULL
               AND COALESCE(m.confirmed, 1) = 1
```

`groups_for` — extend the existing `WHERE`:

```python
            WHERE m.did = ? AND g.archived_at IS NULL
              AND COALESCE(m.confirmed, 1) = 1
```

`composition` — extend the first query's `WHERE`:

```python
            WHERE COALESCE(m.confirmed, 1) = 1 AND g.archived_at IS NULL
              AND fs.is_current = 1 AND fs.ignored_at IS NULL
```

The `in_scope` denominator in `composition()` also has to change, but it depends
on hand-tagged rows existing — so it belongs with the writer that creates them
and is **Task 5's** job, not this task's. Leave that line alone here.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: **everything green, including the whole existing suite.** If `test_groups.py` or `test_web.py` regress, an archive filter has been added to a query that also feeds a job — recheck you edited only the four read functions.

- [ ] **Step 5: Commit**

```bash
git add sonde/db/store.py tests/test_tags.py
git commit -m "M25: archived tags vanish from every read path

Table-driven over the surfaces rather than one assertion each, so a read path
added later fails by omission instead of passing unnoticed."
```

---

### Task 4: Make every job archive-aware

The hole the spec review found. Archiving hides a tag from the reads, but `classify_groups` and `propagate_groups` resolve groups by id with no archive check — so an archived tag goes on quietly gaining members, and restoring it a month later produces a group full of people nobody reviewed.

**Files:**
- Modify: `sonde/db/store.py:2443` (`classify_groups`), `:2932` (`propagate_groups`), `:2228` (`_apply_cluster_group`), `:2254` (`_apply_discovered_group`)
- Test: `tests/test_tags.py`

**Interfaces:**
- Consumes: `archive_group` from Task 2
- Produces: no signature changes

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tags.py`:

```python
async def test_an_archived_tag_stops_gaining_members(db):
    """Archiving hides a tag from the reads. Without this it stays visible to
    the *jobs*, so it keeps accumulating people invisibly and restoring it a
    month later hands you a group nobody has reviewed."""
    from tests.test_groups import add

    await add("did:plc:hack", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await store.archive_group("journalists")

    await store.classify_groups()

    conn = await store._db()
    async with conn.execute(
        """SELECT COUNT(*) FROM group_members m JOIN groups g ON g.id = m.group_id
            WHERE g.slug = 'journalists'"""
    ) as cur:
        assert (await cur.fetchone())[0] == 1, "classify added to an archived tag"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_tags.py::test_an_archived_tag_stops_gaining_members -v`

Expected: FAIL. `classify_groups` deletes the unreviewed row and re-inserts it, so the count is 1 either way — **read the failure carefully**. If it passes immediately, prove the test is real by asserting on a second person: add `did:plc:hack2` with the same occupation *after* archiving and assert the count stays 1.

Use this stronger form instead, which cannot pass by accident:

```python
async def test_an_archived_tag_stops_gaining_members(db):
    from tests.test_groups import add

    await add("did:plc:first", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await store.archive_group("journalists")

    await add("did:plc:second", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()

    conn = await store._db()
    async with conn.execute(
        """SELECT COUNT(*) FROM group_members m JOIN groups g ON g.id = m.group_id
            WHERE g.slug = 'journalists'"""
    ) as cur:
        assert (await cur.fetchone())[0] == 1, "an archived tag gained a member"


async def test_seeding_does_not_resurrect_an_archived_seeded_tag(db):
    """'journalists' is one of the 15 slugs in sonde/groups.py, and
    seed_groups() runs at the top of every classify job. The whole reason
    delete is archive: the surviving row is what makes the seeding upsert
    a no-op."""
    await store.seed_groups()
    await store.archive_group("journalists")

    await store.seed_groups()

    assert "journalists" not in {g["slug"] for g in await store.group_summary()}
    assert "journalists" in {g["slug"] for g in await store.archived_groups()}
```

- [ ] **Step 3: Filter archived groups out of the four job paths**

In `classify_groups`, the id lookup:

```python
    async with db.execute(
        "SELECT id, slug FROM groups WHERE archived_at IS NULL") as cur:
        ids = {r["slug"]: r["id"] for r in await cur.fetchall()}
```

In `propagate_groups`, the seed query:

```python
        """SELECT g.id, g.slug, m.did FROM group_members m
             JOIN groups g ON g.id = m.group_id
            WHERE COALESCE(m.confirmed, 1) = 1 AND m.tier != 'propagation'
              AND g.archived_at IS NULL"""
```

In both `_apply_cluster_group` and `_apply_discovered_group`, the group lookup:

```python
    async with db.execute(
        "SELECT id FROM groups WHERE slug = ? AND archived_at IS NULL", (slug,)
    ) as cur:
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_tags.py -v && uv run pytest tests/test_groups.py tests/test_propagation.py tests/test_discovery.py -q`
Expected: the archived-jobs test PASSES; the existing group, propagation and discovery suites stay green.

- [ ] **Step 5: Commit**

```bash
git add sonde/db/store.py tests/test_tags.py
git commit -m "M25: archived tags stop receiving members, not just displaying them

Found reviewing the spec. classify_groups and propagate_groups resolve groups
by id with no archive check, so an archived tag would have kept accumulating
people invisibly - and restoring it a month later would have produced a group
full of members nobody had ever reviewed."
```

---

### Task 5: Tag and untag

The heart of the feature, and the two tests that matter most in the milestone.

**Files:**
- Modify: `sonde/groups.py:30-35` (confidence constants)
- Modify: `sonde/db/store.py` — add after `review_group_member` (~line 2568)
- Test: `tests/test_tags.py`

**Interfaces:**
- Consumes: `slugify`, `create_group` from Task 2
- Produces:
  - `tag_actor(slug: str, did: str) -> bool` — False if the tag is missing or archived
  - `untag_actor(slug: str, did: str) -> bool` — False if there was no row to change
  - `tag_actors(slug: str, dids: list[str], *, add: bool) -> int` — how many rows changed
  - `sonde.groups.MANUAL: float = 1.0`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tags.py`:

```python
# ------------------------------------------------------- tag and untag

async def test_tagging_someone_the_machine_never_proposed_is_a_manual_row(db):
    from tests.test_groups import add

    await add("did:plc:new", is_verified=True)
    await store.create_group("Neighbours")

    assert await store.tag_actor("neighbours", "did:plc:new")

    row = (await store.group_members("neighbours"))[0]
    assert row["did"] == "did:plc:new"
    assert row["tier"] == "manual"
    assert row["confidence"] == 1.0


async def test_tagging_a_row_the_machine_made_confirms_it_and_keeps_the_evidence(db):
    """Agreeing with the machine is not the same as replacing it. Overwriting
    tier and evidence here would throw away *why* the person is in the group."""
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()

    await store.tag_actor("journalists", "did:plc:j")

    row = (await store.group_members("journalists"))[0]
    assert row["tier"] == "wikidata"
    assert row["evidence"] == "Wikidata occupation: journalist"
    assert row["confirmed"] == 1


async def test_a_hand_tag_survives_a_reclassify(db):
    """THE test of this milestone. classify_groups deletes unreviewed rows and
    re-runs the rules every six hours. If the guards do not hold, hand-tagging
    vanishes overnight and the operator finds out days later."""
    from tests.test_groups import add

    await add("did:plc:quiet", is_verified=True)
    await store.create_group("Neighbours")
    await store.tag_actor("neighbours", "did:plc:quiet")

    await store.classify_groups()

    row = (await store.group_members("neighbours"))[0]
    assert row["did"] == "did:plc:quiet"
    assert row["tier"] == "manual"


async def test_an_untag_survives_a_reclassify(db):
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    assert await store.untag_actor("journalists", "did:plc:j")

    await store.classify_groups()

    assert await store.group_members("journalists") == []


async def test_untagging_keeps_the_machines_reasoning(db):
    """confirmed = 0 records the disagreement without erasing what the rule
    thought — 'argued with rather than merely deleted'."""
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await store.untag_actor("journalists", "did:plc:j")

    conn = await store._db()
    async with conn.execute(
        "SELECT tier, evidence, confirmed FROM group_members") as cur:
        row = await cur.fetchone()
    assert row["tier"] == "wikidata"
    assert row["evidence"] == "Wikidata occupation: journalist"
    assert row["confirmed"] == 0


async def test_tagging_again_undoes_an_untag(db):
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await store.untag_actor("journalists", "did:plc:j")

    await store.tag_actor("journalists", "did:plc:j")

    assert [m["did"] for m in await store.group_members("journalists")] == ["did:plc:j"]


async def test_untagging_someone_who_was_never_tagged_does_nothing(db):
    """Removing a tag from a hundred people when thirty have it is a no-op for
    the other seventy. It must NOT write a pre-emptive tombstone: a rule may
    legitimately tag one of them tomorrow, and 'remove' means undo, not
    'never again'."""
    from tests.test_groups import add

    await add("did:plc:stranger", is_verified=True)
    await store.create_group("Neighbours")

    assert not await store.untag_actor("neighbours", "did:plc:stranger")

    conn = await store._db()
    async with conn.execute("SELECT COUNT(*) FROM group_members") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_tagging_an_archived_tag_is_refused(db):
    from tests.test_groups import add

    await add("did:plc:x", is_verified=True)
    await store.create_group("Gone")
    await store.archive_group("gone")

    assert not await store.tag_actor("gone", "did:plc:x")


async def test_a_hand_decision_is_dated(db):
    from tests.test_groups import add

    await add("did:plc:x", is_verified=True)
    await store.create_group("Neighbours")
    await store.tag_actor("neighbours", "did:plc:x")

    conn = await store._db()
    async with conn.execute("SELECT decided_at FROM group_members") as cur:
        assert (await cur.fetchone())["decided_at"]


async def test_a_batch_reports_how_many_rows_it_changed(db):
    from tests.test_groups import add

    for did in ("did:plc:a", "did:plc:b", "did:plc:c"):
        await add(did, is_verified=True)
    await store.create_group("Cohort")

    assert await store.tag_actors(
        "cohort", ["did:plc:a", "did:plc:b", "did:plc:c"], add=True) == 3
    assert await store.tag_actors(
        "cohort", ["did:plc:a", "did:plc:zzz"], add=False) == 1
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_tags.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'tag_actor'`

- [ ] **Step 3: Add the MANUAL constant**

In `sonde/groups.py`, with the other confidences (after `PROPAGATION = 0.5`):

```python
# A human said so. Nothing outranks this, and no job may overwrite it.
MANUAL = 1.0
```

Also update the tier table in the module docstring so it stops describing
`manual` as hypothetical — change the `T6 manual` line's confidence column from
`exact` to `1.0`.

- [ ] **Step 4: Write the three functions**

In `sonde/db/store.py`, after `review_group_member`:

```python
async def tag_actor(slug: str, did: str) -> bool:
    """Hand-tag one person. False if the tag is missing or archived.

    Three cases, and the middle one is the subtle one. Tagging someone the
    machine already proposed *confirms* the existing row rather than replacing
    it: agreeing with a rule is not the same as overruling it, and overwriting
    tier and evidence would discard the reason they are in the group at all.
    """
    from sonde.groups import MANUAL

    db = await _db()
    async with db.execute(
        "SELECT id FROM groups WHERE slug = ? AND archived_at IS NULL", (slug,)
    ) as cur:
        group = await cur.fetchone()
    if group is None:
        return False

    now = utcnow()
    await db.execute(
        """INSERT INTO group_members
             (group_id, did, tier, confidence, evidence, confirmed,
              created_at, decided_at)
           VALUES (?,?,?,?,?,1,?,?)
           ON CONFLICT (group_id, did) DO UPDATE SET
             confirmed = 1, decided_at = excluded.decided_at""",
        (group["id"], did, "manual", MANUAL, "tagged by hand", now, now),
    )
    await db.commit()
    return True


async def untag_actor(slug: str, did: str) -> bool:
    """`confirmed = 0`, whatever the row's origin. False if there was no row.

    Existing rows only. This is undo, not a pre-emptive block: removing a tag
    from people who never had it must not write tombstones, or a rule that
    legitimately matches one of them tomorrow would be silently suppressed
    forever.
    """
    db = await _db()
    cur = await db.execute(
        """UPDATE group_members SET confirmed = 0, decided_at = ?
            WHERE did = ? AND group_id = (
                  SELECT id FROM groups
                   WHERE slug = ? AND archived_at IS NULL)""",
        (utcnow(), did, slug),
    )
    await db.commit()
    return bool(cur.rowcount)


async def tag_actors(slug: str, dids: list[str], *, add: bool) -> int:
    """Batch. Returns the number of rows actually changed, which is what the
    page reports back — 'tagged 30' when 100 were selected is information."""
    changed = 0
    for did in dids:
        if await (tag_actor(slug, did) if add else untag_actor(slug, did)):
            changed += 1
    return changed
```

- [ ] **Step 5: Widen the composition denominator**

Now that hand-tagged rows can exist, `composition()`'s `"N of M in scope"` line
starts lying: manual tags are not restricted to the grouping set. Replace the
`in_scope` line at the end of `composition()`:

```python
            "in_scope": len(await group_target_dids(top_n=settings.posts_top_n)),
```

with:

```python
            "in_scope": await _in_scope_count(),
```

and add above `composition()`:

```python
async def _in_scope_count() -> int:
    """The grouping set, plus anyone a human tagged by hand.

    A hand-applied tag is a fact about a person regardless of how influential
    they are, so manual members exist outside the target set — and a
    denominator that excluded them would under-report the moment the first one
    was tagged.
    """
    targets = set(await group_target_dids(top_n=settings.posts_top_n))
    db = await _db()
    async with db.execute(
        """SELECT DISTINCT m.did FROM group_members m
             JOIN groups g ON g.id = m.group_id
             JOIN follower_state fs ON fs.did = m.did
            WHERE m.tier = 'manual' AND COALESCE(m.confirmed, 1) = 1
              AND g.archived_at IS NULL
              AND fs.is_current = 1 AND fs.ignored_at IS NULL"""
    ) as cur:
        targets.update(r["did"] for r in await cur.fetchall())
    return len(targets)
```

Its test, appended to `tests/test_tags.py`:

```python
async def test_hand_tagging_outside_the_grouping_set_widens_the_denominator(db):
    """The composition chart says 'N of M in scope'. A hand-applied tag is a
    fact about a person however uninfluential they are, so manual members exist
    outside the top-500-plus-verified target set — and a denominator ignoring
    them would under-report from the first tag onwards."""
    from tests.test_groups import add

    # Neither verified nor influential: outside the grouping set by construction.
    await add("did:plc:nobody")
    before = (await store.composition())["in_scope"]

    await store.create_group("Neighbours")
    await store.tag_actor("neighbours", "did:plc:nobody")

    assert (await store.composition())["in_scope"] == before + 1
```

- [ ] **Step 6: Run the tests, then the whole suite**

Run: `uv run pytest tests/test_tags.py -v && uv run pytest -q`
Expected: all of `test_tags.py` passes. Whole suite green.

- [ ] **Step 7: Commit**

```bash
git add sonde/groups.py sonde/db/store.py tests/test_tags.py
git commit -m "M25: tag and untag, and prove they survive the six-hourly job

classify_groups deletes unreviewed rows and re-runs the rules every six hours.
It has always excluded tier='manual' and confirmed IS NOT NULL - two correct
guards that had never protected a row, because nothing could create one. These
tests are the first thing to exercise them, and the failure they prevent is
silent: hand-tagging would vanish overnight and be noticed days later.

Tagging someone the machine already proposed confirms that row and keeps its
tier and evidence. Agreeing with a rule is not overruling it."
```

---

### Task 6: Routes

**Files:**
- Modify: `pyproject.toml:7-20`
- Modify: `sonde/web/app.py` — `_safe_back` near `_as_int` (~line 25); routes beside the existing group routes (~line 334); `follow_back` (~line 380) uses the helper
- Test: `tests/test_tags.py`

`python-multipart` is **not currently installed** and every existing write route takes query parameters. `Form(...)` needs it. Add it — one pure-Python dependency, and the alternative is hand-parsing `await request.form()` in five routes.

**Interfaces:**
- Consumes: everything from Tasks 2 and 5
- Produces:
  - `_safe_back(request: Request, fallback: str) -> str`
  - `POST /groups/new` · `POST /groups/{slug}/rename` · `POST /groups/{slug}/archive?undo=` · `POST /groups/apply` · `POST /followers/{did}/tags`
  - `GET /groups` gains a `notice: str | None` query parameter

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tags.py`:

```python
# ---------------------------------------------------------------- routes

import pytest
from fastapi.testclient import TestClient

from sonde.web.app import create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


async def test_the_batch_endpoint_tags_everyone_selected(db, client):
    from tests.test_groups import add

    for did in ("did:plc:a", "did:plc:b"):
        await add(did, is_verified=True)

    response = client.post("/groups/apply", data={
        "did": ["did:plc:a", "did:plc:b"], "tag": "Cohort", "action": "add",
    }, follow_redirects=False)

    assert response.status_code == 303
    assert {m["did"] for m in await store.group_members("cohort")} == {
        "did:plc:a", "did:plc:b"}


async def test_the_batch_endpoint_creates_the_tag_when_adding(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    client.post("/groups/apply", data={"did": ["did:plc:a"], "tag": "Brand new",
                                       "action": "add"}, follow_redirects=False)

    assert "brand-new" in {g["slug"] for g in await store.group_names()}


async def test_removing_with_an_unknown_tag_creates_nothing(db, client):
    """Only adding may create. Otherwise a typo in a remove conjures an empty
    tag out of nowhere."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    client.post("/groups/apply", data={"did": ["did:plc:a"], "tag": "Typo",
                                       "action": "remove"}, follow_redirects=False)

    assert await store.group_names() == []


async def test_the_batch_endpoint_returns_you_to_the_page_you_came_from(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    response = client.post(
        "/groups/apply", data={"did": ["did:plc:a"], "tag": "X", "action": "add"},
        headers={"referer": "http://testserver/followers?page=2"},
        follow_redirects=False)

    assert response.headers["location"].startswith("/followers?page=2")


async def test_a_referer_pointing_off_site_cannot_redirect_off_site(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    response = client.post(
        "/groups/apply", data={"did": ["did:plc:a"], "tag": "X", "action": "add"},
        headers={"referer": "https://evil.example/phish"},
        follow_redirects=False)

    assert response.headers["location"].startswith("/phish")


async def test_creating_a_tag_that_is_archived_says_so_on_the_page(db, client):
    await store.create_group("Gone")
    await store.archive_group("gone")

    client.post("/groups/new", data={"name": "Gone"}, follow_redirects=False)
    page = client.get("/groups?notice=archived&slug=gone")

    assert page.status_code == 200
    assert "archived" in page.text.lower()


async def test_renaming_through_the_route(db, client):
    await store.create_group("Before")
    client.post("/groups/before/rename", data={"name": "After"},
                follow_redirects=False)

    assert {g["name"] for g in await store.group_summary()} == {"After"}


async def test_archiving_and_restoring_through_the_route(db, client):
    await store.create_group("Temporary")

    client.post("/groups/temporary/archive", follow_redirects=False)
    assert await store.group_names() == []

    client.post("/groups/temporary/archive?undo=true", follow_redirects=False)
    assert [g["slug"] for g in await store.group_names()] == ["temporary"]


async def test_tagging_one_person_from_their_detail_page(db, client):
    from tests.test_groups import add

    await add("did:plc:solo", is_verified=True)
    client.post("/followers/did:plc:solo/tags",
                data={"tag": "Neighbours", "action": "add"},
                follow_redirects=False)

    assert [g["slug"] for g in await store.groups_for("did:plc:solo")] == ["neighbours"]
```

- [ ] **Step 2: Run and confirm they fail**

Run: `uv run pytest tests/test_tags.py -k route or batch -v`
Expected: FAIL with 405 Method Not Allowed, or a `RuntimeError` about `python-multipart`.

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, in `dependencies`, after `"jinja2>=3.1",`:

```toml
    # Form bodies. Every route before M25 took query parameters, so FastAPI
    # never needed this; batch tagging posts a repeated `did` field, which is
    # a form. Pure Python, no build step.
    "python-multipart>=0.0.9",
```

Then: `uv sync`

- [ ] **Step 4: Extract the referer helper**

In `sonde/web/app.py`, after `_as_int`:

```python
def _safe_back(request: Request, fallback: str) -> str:
    """Where to send the operator after a write.

    Only ever a page inside sonde, never an absolute URL from a header we do
    not control. Extracted from `follow_back` when the tag routes became the
    fifth caller.
    """
    back = request.headers.get("referer") or fallback
    if "://" in back:
        rest = back.split("://", 1)[1]
        back = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    return back
```

In `follow_back`, delete the four inline lines that compute `back` and their comment, and use:

```python
        back = _safe_back(request, f"/followers/{did}")
```

- [ ] **Step 5: Add the routes**

In `sonde/web/app.py`, after the existing `review_group` route:

```python
    async def _apply_tags(back: str, dids: list[str], tag: str,
                          action: str) -> RedirectResponse:
        """Shared by the batch bar and the single control on a detail page."""
        from sonde.db import store

        tag = (tag or "").strip()
        if not tag or not dids:
            return RedirectResponse(f"{back}#nothing-selected", status_code=303)

        if action == "add":
            # Type-to-create: an unknown name in the box makes the tag. Only on
            # add — a typo while removing must not conjure an empty tag.
            result = await store.create_group(tag)
            if result["status"] in ("invalid", "archived"):
                return RedirectResponse(f"{back}#tag-{result['status']}",
                                        status_code=303)
            slug = result["slug"]
        else:
            slug = store.slugify(tag)

        changed = await store.tag_actors(slug, dids, add=(action == "add"))
        return RedirectResponse(f"{back}#tagged-{changed}", status_code=303)

    @app.post("/groups/new")
    async def new_group(name: str = Form("")) -> RedirectResponse:
        from sonde.db import store

        result = await store.create_group(name)
        if result["status"] == "created":
            return RedirectResponse(f"/groups?slug={result['slug']}", status_code=303)
        suffix = f"&slug={result['slug']}" if result["slug"] else ""
        return RedirectResponse(f"/groups?notice={result['status']}{suffix}",
                                status_code=303)

    @app.post("/groups/{slug}/rename")
    async def rename_group(slug: str, name: str = Form("")) -> RedirectResponse:
        from sonde.db import store

        await store.rename_group(slug, name)
        return RedirectResponse(f"/groups?slug={slug}", status_code=303)

    @app.post("/groups/{slug}/archive")
    async def archive_group(slug: str, undo: bool = False) -> RedirectResponse:
        from sonde.db import store

        await store.archive_group(slug, archived=not undo)
        return RedirectResponse(f"/groups?slug={slug}" if undo else "/groups",
                                status_code=303)

    @app.post("/groups/apply")
    async def apply_tags(request: Request, did: list[str] = Form([]),
                         tag: str = Form(""),
                         action: str = Form("add")) -> RedirectResponse:
        return await _apply_tags(_safe_back(request, "/followers"), did, tag, action)

    @app.post("/followers/{did}/tags")
    async def tag_one(request: Request, did: str, tag: str = Form(""),
                      action: str = Form("add")) -> RedirectResponse:
        return await _apply_tags(
            _safe_back(request, f"/followers/{did}"), [did], tag, action)
```

Add `Form` to the FastAPI import at the top of the file:

```python
from fastapi import FastAPI, Form, Request
```

**On route ordering:** `/groups/new` and `/groups/apply` are two segments while
`/groups/{slug}/rename` and `/groups/{slug}/archive` are three, so nothing here
can shadow anything else and the declaration order is free. This only becomes a
trap if someone later adds a `POST /groups/{slug}` — at that point `new` and
`apply` would start matching it, and they would have to be declared first.

- [ ] **Step 6: Extend the /groups route for the notice and the new context**

In `groups_index`, add the parameter and three context keys:

```python
    @app.get("/groups", response_class=HTMLResponse)
    async def groups_index(
        request: Request, slug: str | None = None, order: str = "influence",
        direction: str = "desc", notice: str | None = None,
    ) -> HTMLResponse:
```

(keep whatever the existing signature has; only add `notice`), and in the
context dict add:

```python
                "notice": notice,
                "all_tags": await store.group_names(),
                "archived": await store.archived_groups(),
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_tags.py -v && uv run pytest -q`
Expected: all green. `test_creating_a_tag_that_is_archived_says_so_on_the_page` will still fail — the template does not render the notice until Task 8. Leave it failing and note it, or move that one assertion to Task 8.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock sonde/web/app.py tests/test_tags.py
git commit -m "M25: routes for tagging, renaming and archiving

python-multipart joins the dependencies. Every write route before this took
query parameters, so FastAPI never needed it; a batch posts a repeated 'did'
field, which is a form body.

Only adding may create a tag. A typo while removing would otherwise conjure an
empty tag out of nowhere.

The referer sanitiser comes out of follow_back rather than being copied into
five new write routes."
```

---

### Task 7: Tag macros, and the detail page

**Files:**
- Create: `sonde/web/templates/_tags.html`
- Modify: `sonde/web/templates/detail.html:177-185`
- Test: `tests/test_tags.py`

**Interfaces:**
- Produces three Jinja macros:
  - `tag_chip(group, did)` — a tag chip with an `×` that untags
  - `tag_input(all_tags, action_url, name='tag')` — datalist-backed add control
  - `batch_bar(all_tags)` — the sticky action row for a table-wrapping form

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tags.py`:

```python
async def test_the_detail_page_offers_a_tag_control(db, client):
    from tests.test_groups import add

    await add("did:plc:solo", is_verified=True)
    await store.create_group("Neighbours")
    await store.tag_actor("neighbours", "did:plc:solo")

    page = client.get("/followers/did:plc:solo")

    assert page.status_code == 200
    assert 'action="/followers/did:plc:solo/tags"' in page.text
    assert "Neighbours" in page.text


def test_every_template_importing_the_tag_macros_does_so_with_context():
    """A macro imported without context cannot see `settings`, and the control
    silently never renders — exactly how the follow button disappeared in M23."""
    from pathlib import Path

    root = Path("sonde/web/templates")
    for page in root.glob("*.html"):
        text = page.read_text()
        for line in text.splitlines():
            if '_tags.html' in line and 'import' in line:
                assert "with context" in line, f"{page.name} imports without context"
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/test_tags.py -k detail_page or with_context -v`
Expected: FAIL — `assert 'action="/followers/did:plc:solo/tags"' in page.text`

- [ ] **Step 3: Create the macro file**

`sonde/web/templates/_tags.html`:

```jinja
{#- Tag controls. Groups are tags: the machine proposes, a human decides, and
    the human always wins. Every control here is a plain form — no JavaScript
    is required for any of it, matching the rest of the app. -#}

{#- One tag, with the × that removes it. `did` is who the removal applies to. -#}
{%- macro tag_chip(group, did) -%}
<span class="inline-flex items-center gap-1 rounded-sm bg-slate-100 py-0.5 pl-2 pr-1
             font-mono text-[10px] uppercase tracking-wider text-slate-700">
  <a href="/groups?slug={{ group.slug }}" class="hover:text-red-800"
     title="{{ group.evidence }} ({{ group.tier }})">{{ group.name }}</a>
  <form method="post" action="/followers/{{ did }}/tags" class="inline">
    <input type="hidden" name="tag" value="{{ group.name }}">
    <input type="hidden" name="action" value="remove">
    <button type="submit" class="px-1 text-slate-400 hover:text-red-800"
            title="remove this tag">×</button>
  </form>
</span>
{%- endmacro -%}

{#- Add a tag. The datalist autocompletes existing tags; typing a name that is
    not in it creates that tag, which is why there is no separate "new tag"
    step anywhere except the admin row on /groups. -#}
{%- macro tag_input(all_tags, action_url, label='Add tag') -%}
<form method="post" action="{{ action_url }}" class="inline-flex items-center gap-1">
  <input type="hidden" name="action" value="add">
  <input type="text" name="tag" list="all-tags" placeholder="{{ label }}"
         class="w-40 border border-slate-300 px-2 py-0.5 text-xs
                focus:border-red-800 focus:outline-none">
  <button type="submit"
          class="border border-slate-300 px-2 py-0.5 font-mono text-[10px]
                 uppercase tracking-wider text-slate-600
                 hover:border-red-800 hover:text-red-800">add</button>
</form>
{%- endmacro -%}

{#- The datalist itself, rendered once per page that uses tag_input. -#}
{%- macro tag_options(all_tags) -%}
<datalist id="all-tags">
  {%- for t in all_tags %}<option value="{{ t.name }}"></option>{% endfor -%}
</datalist>
{%- endmacro -%}

{#- The batch bar. Belongs INSIDE a <form method="post" action="/groups/apply">
    that also wraps the table, so the checkboxes and these controls post
    together. Sticky by CSS; the count is filled in by the enhancement script in
    base.html and reads "0 selected" without it. -#}
{%- macro batch_bar(all_tags) -%}
<div class="sticky bottom-0 z-10 -mx-4 mt-4 flex flex-wrap items-center gap-3
            border-t border-slate-300 bg-white/95 px-4 py-3 backdrop-blur">
  <span class="font-mono text-[10px] uppercase tracking-widest text-slate-500">
    <span data-selection-count>0</span> selected
  </span>
  <input type="text" name="tag" list="all-tags" placeholder="tag name"
         class="w-48 border border-slate-300 px-2 py-1 text-sm
                focus:border-red-800 focus:outline-none">
  <button type="submit" name="action" value="add"
          class="bg-slate-900 px-3 py-1 font-mono text-[10px] uppercase
                 tracking-widest text-white hover:bg-red-800">add tag</button>
  <button type="submit" name="action" value="remove"
          class="border border-slate-300 px-3 py-1 font-mono text-[10px]
                 uppercase tracking-widest text-slate-600
                 hover:border-red-800 hover:text-red-800">remove tag</button>
  <span class="text-xs text-slate-400">
    an unfamiliar name creates that tag
  </span>
</div>
{%- endmacro -%}
```

- [ ] **Step 4: Rewrite the detail page's group block**

In `sonde/web/templates/detail.html`, add to the imports at the top of the file:

```jinja
{% from "_tags.html" import tag_chip, tag_input, tag_options with context %}
```

and replace the whole `{% if p.groups %}` block (lines 177-185) with:

```jinja
<div class="mt-4 flex flex-wrap items-center gap-2">
  {% for g in p.groups %}{{ tag_chip(g, p.did) }}{% endfor %}
  {{ tag_input(all_tags, "/followers/" ~ p.did ~ "/tags") }}
</div>
{{ tag_options(all_tags) }}
```

Note the `{% if %}` is gone deliberately: the add control must render even when
someone has no tags, which is exactly when you most want it.

- [ ] **Step 5: Pass `all_tags` to the detail template**

In `sonde/web/app.py`, in `follower_detail`, add to the context dict:

```python
                "all_tags": await store.group_names(),
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_tags.py -v && uv run pytest tests/test_detail_template.py -q`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add sonde/web/templates/_tags.html sonde/web/templates/detail.html sonde/web/app.py tests/test_tags.py
git commit -m "M25: tag chips you can remove, and an input that creates as you type

The add control renders even when someone has no tags at all - which is
precisely when you want it. A datalist gives native autocomplete over existing
tags with no JavaScript, and an unfamiliar name creates that tag."
```

---

### Task 8: The /groups page

**Files:**
- Modify: `sonde/web/templates/groups.html`
- Test: `tests/test_tags.py`

**Interfaces:**
- Consumes: `all_tags`, `archived`, `notice` from Task 6's context additions; macros from Task 7

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tags.py`:

```python
async def test_the_groups_page_can_rename_archive_and_create(db, client):
    await store.create_group("Cohort")

    page = client.get("/groups?slug=cohort")

    assert 'action="/groups/cohort/rename"' in page.text
    assert 'action="/groups/cohort/archive"' in page.text
    assert 'action="/groups/new"' in page.text


async def test_the_groups_page_lists_what_can_be_restored(db, client):
    await store.create_group("Gone")
    await store.archive_group("gone")

    page = client.get("/groups")

    assert 'action="/groups/gone/archive?undo=true"' in page.text


async def test_the_member_table_can_untag_a_row_the_machine_got_right(db, client):
    """Before M25 the 'remove' control rendered only on unreviewed rows, so a
    correctly-classified person could not be removed at all."""
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await store.tag_actor("journalists", "did:plc:j")   # confirmed = 1

    page = client.get("/groups?slug=journalists")

    assert 'name="did" value="did:plc:j"' in page.text
```

- [ ] **Step 2: Run and confirm they fail**

Run: `uv run pytest tests/test_tags.py -k groups_page or member_table -v`
Expected: FAIL — none of those strings are in the page.

- [ ] **Step 3: Add the admin row and notice banner**

In `sonde/web/templates/groups.html`, add to the imports:

```jinja
{% from "_tags.html" import batch_bar, tag_options with context %}
```

After the `<h1>` header block (after line 29), insert:

```jinja
{% if notice %}
<p class="mt-4 border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900">
  {% if notice == 'archived' %}
    That tag already exists but is archived — restore it below rather than
    making a second one.
  {% elif notice == 'exists' %}
    That tag already exists.
  {% else %}
    That name has no usable slug — try one with letters or numbers in it.
  {% endif %}
</p>
{% endif %}

<form method="post" action="/groups/new" class="mt-4 flex flex-wrap items-center gap-2">
  <input type="text" name="name" placeholder="new tag name"
         class="w-56 border border-slate-300 px-2 py-1 text-sm
                focus:border-red-800 focus:outline-none">
  <button type="submit"
          class="bg-slate-900 px-3 py-1 font-mono text-[10px] uppercase
                 tracking-widest text-white hover:bg-red-800">create</button>
</form>
```

- [ ] **Step 4: Add rename and archive for the selected tag**

Immediately after the chip row (after line 51's `</div>`), insert:

```jinja
{% if slug %}
{% set current = (summary | selectattr('slug', 'equalto', slug) | list | first) %}
{% if current %}
<div class="mt-4 flex flex-wrap items-center gap-3 border-y border-slate-200 py-2">
  <form method="post" action="/groups/{{ slug }}/rename"
        class="flex items-center gap-2">
    <input type="text" name="name" value="{{ current.name }}"
           class="w-56 border border-slate-300 px-2 py-1 text-sm
                  focus:border-red-800 focus:outline-none">
    <button type="submit"
            class="border border-slate-300 px-2 py-1 font-mono text-[10px]
                   uppercase tracking-wider text-slate-600
                   hover:border-red-800 hover:text-red-800">rename</button>
  </form>
  <span class="font-mono text-[10px] text-slate-400"
        title="the slug is the URL key and cannot change — renaming only
               changes the display name">/groups?slug={{ slug }}</span>
  <form method="post" action="/groups/{{ slug }}/archive" class="ml-auto">
    <button type="submit"
            class="font-mono text-[10px] uppercase tracking-wider text-slate-400
                   hover:text-red-800"
            title="hides the tag everywhere and stops it gaining members; the
                   members themselves are kept and it can be restored">archive</button>
  </form>
</div>
{% endif %}
{% endif %}
```

- [ ] **Step 5: Wrap the member table in the batch form**

Replace the member-table block (lines 53-96) so the table sits inside a form,
each row gains a checkbox, and the last cell untags through the tag endpoint:

```jinja
{% if slug and members %}
<form method="post" action="/groups/apply" data-batch>
  <div class="mt-8 overflow-x-auto">
    <table class="w-full text-sm">
      <thead>
        <tr class="border-b border-slate-300 text-left">
          <th class="w-8 py-2 pr-2">
            <input type="checkbox" data-select-all aria-label="select all on this page">
          </th>
          {{ th(base, 'handle', 'Handle') }}
          {{ th(base, 'name', 'Name') }}
          {{ th(base, 'followers', 'Followers', 'right') }}
          {{ th(base, 'influence', 'Score', 'right') }}
          {{ th(base, 'tier', 'Why') }}
        </tr>
      </thead>
      <tbody>
        {% for m in members %}
        <tr class="border-b border-slate-100 hover:bg-slate-50">
          <td class="py-2 pr-2">
            <input type="checkbox" name="did" value="{{ m.did }}"
                   aria-label="select {{ m.handle }}">
          </td>
          <td class="py-2 pr-4">{{ who(m) }}</td>
          <td class="py-2 pr-4">{{ m.display_name or "" }}</td>
          <td class="py-2 pr-4 text-right font-mono tabular-nums text-slate-600">
            {% if m.followers_count is not none %}{{ "{:,}".format(m.followers_count) }}{% else %}—{% endif %}
          </td>
          <td class="py-2 pr-4 text-right font-mono font-semibold tabular-nums">
            {% if m.influence_score is not none %}{{ m.influence_score }}{% else %}—{% endif %}
          </td>
          <td class="py-2 pr-4 text-xs text-slate-500">
            {{ m.evidence }}
            <span class="font-mono text-[10px] uppercase tracking-wider text-slate-400">{{ m.tier }}</span>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {{ batch_bar(all_tags) }}
</form>
{{ tag_options(all_tags) }}
{% elif slug %}
```

**There is deliberately no hidden `tag` field.** An earlier draft of this plan
had one, to guarantee the field was always present. It is both unnecessary —
`tag: str = Form("")` defaults happily when the field is absent, so a blank bar
gives `""` and not a 422 — and actively unsafe, because two inputs sharing a
name make which value the server reads depend on multidict internals rather
than on anything visible in the template. One input, one name.

**The per-row "remove" button is gone on purpose.** Untagging is now the batch
bar's "remove tag" with rows ticked, which works on every row rather than only
unreviewed ones.

- [ ] **Step 6: Add the archived section**

Before `{% endblock %}` at the end of the file:

```jinja
{% if archived %}
<section class="mt-12 border-t border-slate-200 pt-4">
  <h2 class="font-mono text-[10px] uppercase tracking-widest text-slate-500">
    Archived
  </h2>
  <p class="mt-1 max-w-2xl text-xs text-slate-500">
    Hidden everywhere and no longer gaining members. Nothing was deleted —
    restoring one brings its members back exactly as they were.
  </p>
  <ul class="mt-3 flex flex-wrap gap-2">
    {% for a in archived %}
    <li class="flex items-center gap-2 border border-slate-200 px-2 py-1">
      <span class="font-mono text-xs text-slate-500">{{ a.name }}</span>
      <span class="font-mono text-[10px] tabular-nums text-slate-400">{{ a.members }}</span>
      <form method="post" action="/groups/{{ a.slug }}/archive?undo=true">
        <button type="submit"
                class="font-mono text-[10px] uppercase tracking-wider text-slate-400
                       hover:text-emerald-700">restore</button>
      </form>
    </li>
    {% endfor %}
  </ul>
</section>
{% endif %}
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_tags.py tests/test_groups.py -v`
Expected: green, including `test_creating_a_tag_that_is_archived_says_so_on_the_page` from Task 6.

- [ ] **Step 8: Commit**

```bash
git add sonde/web/templates/groups.html tests/test_tags.py
git commit -m "M25: /groups gains create, rename, archive and batch untag

The per-row remove button is gone. It rendered only on unreviewed rows, so a
person the machine classified correctly could not be removed at all; the batch
bar works on every row.

The slug is shown next to the rename box, greyed, because renaming cannot
change it and the URL is the one place that surprises people."
```

---

### Task 9: The /followers page

**Files:**
- Modify: `sonde/web/templates/followers.html`
- Modify: `sonde/web/app.py` — `followers` route context
- Test: `tests/test_tags.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_the_followers_table_can_batch_tag(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)

    page = client.get("/followers")

    assert 'action="/groups/apply"' in page.text
    assert 'name="did" value="did:plc:a"' in page.text
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/test_tags.py -k followers_table -v`
Expected: FAIL.

- [ ] **Step 3: Wrap the table and add the column**

In `sonde/web/templates/followers.html` add to the imports:

```jinja
{% from "_tags.html" import batch_bar, tag_options with context %}
```

Wrap the existing `<div class="mt-6 overflow-x-auto">…</div>` table block in a
form and add the checkbox column. Open, immediately before that div:

```jinja
<form method="post" action="/groups/apply" data-batch>
```

Add as the first `<th>` in the header row, before `{{ th('handle', 'Handle') }}`:

```jinja
        <th class="w-8 py-2 pr-2">
          <input type="checkbox" data-select-all aria-label="select all on this page">
        </th>
```

Add as the first `<td>` in the body row, before the `who(row)` cell:

```jinja
        <td class="py-2 pr-2">
          <input type="checkbox" name="did" value="{{ row.did }}"
                 aria-label="select {{ row.handle }}">
        </td>
```

Close, immediately after that table div:

```jinja
  {{ batch_bar(all_tags) }}
</form>
{{ tag_options(all_tags) }}
```

- [ ] **Step 4: Pass `all_tags` to the template**

In `sonde/web/app.py`, in the `followers` route context dict:

```python
                "all_tags": await store.group_names(),
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_tags.py tests/test_web.py -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add sonde/web/templates/followers.html sonde/web/app.py tests/test_tags.py
git commit -m "M25: batch tagging on the followers list

Selection is the visible hundred. There is deliberately no select-all-across-
pages: a filter that matches 1,800 people and one mis-click is not a
combination worth building."
```

---

### Task 10: Filter /followers by tag

**Files:**
- Modify: `sonde/db/store.py:757` (`ranked_followers`)
- Modify: `sonde/web/app.py:595` (`followers` route)
- Modify: `sonde/web/templates/followers.html` (the filter form)
- Test: `tests/test_tags.py`

**Interfaces:**
- Produces: `ranked_followers(..., tag: str | None = None)`

- [ ] **Step 1: Write the failing tests**

```python
async def test_filtering_the_followers_list_by_tag(db):
    from tests.test_groups import add

    await add("did:plc:in", is_verified=True)
    await add("did:plc:out", is_verified=True)
    await store.create_group("Cohort")
    await store.tag_actor("cohort", "did:plc:in")

    rows = await store.ranked_followers(tag="cohort")

    assert [r["did"] for r in rows] == ["did:plc:in"]


async def test_the_tag_filter_ignores_untagged_and_archived(db):
    from tests.test_groups import add

    await add("did:plc:in", is_verified=True)
    await store.create_group("Cohort")
    await store.tag_actor("cohort", "did:plc:in")
    await store.untag_actor("cohort", "did:plc:in")
    assert await store.ranked_followers(tag="cohort") == []

    await store.tag_actor("cohort", "did:plc:in")
    await store.archive_group("cohort")
    assert await store.ranked_followers(tag="cohort") == []


async def test_the_tag_filter_survives_paging_and_sorting(db, client):
    """Every link on the page must carry the current filters. The comment at
    app.py:621 exists because dropping one silently broke sorting once."""
    from tests.test_groups import add

    await add("did:plc:in", is_verified=True)
    await store.create_group("Cohort")
    await store.tag_actor("cohort", "did:plc:in")

    page = client.get("/followers?tag=cohort&order=followers")

    assert "tag=cohort" in page.text
```

- [ ] **Step 2: Run and confirm they fail**

Run: `uv run pytest tests/test_tags.py -k tag_filter or filtering -v`
Expected: FAIL — `TypeError: ranked_followers() got an unexpected keyword argument 'tag'`

- [ ] **Step 3: Add the filter to the query**

In `ranked_followers`, add the parameter:

```python
async def ranked_followers(
    limit: int = 50, offset: int = 0, *, order: str = "influence",
    direction: str = "desc", verified_only: bool = False,
    min_followers: int | None = None, query: str | None = None,
    mutual_only: bool = False, tag: str | None = None,
) -> list[dict]:
```

and after the `mutual_only` clause:

```python
    if tag:
        # EXISTS rather than a JOIN: a person in a tag twice must not appear
        # twice in the list.
        where.append(
            """EXISTS (SELECT 1 FROM group_members m2
                         JOIN groups g2 ON g2.id = m2.group_id
                        WHERE m2.did = a.did AND g2.slug = ?
                          AND COALESCE(m2.confirmed, 1) = 1
                          AND g2.archived_at IS NULL)""")
        params.append(tag)
```

- [ ] **Step 4: Wire the route**

In the `followers` route, add `tag: str | None = None` to the signature, pass
it through to `ranked_followers(..., tag=tag)`, and add it to **both** the
`filters` dict and the context:

```python
        filters = {"q": q or "", "verified": verified, "mutual": mutual,
                   "min_followers": floor if floor is not None else "",
                   "tag": tag or ""}
```

```python
                "tag": tag or "", "all_tags": await store.group_names(),
```

- [ ] **Step 5: Add the control to the filter form**

In `followers.html`, inside the `<form method="get">`, after the "Min followers"
label:

```jinja
  <label class="flex flex-col gap-1">
    <span class="font-mono text-[10px] uppercase tracking-widest text-slate-500">Tag</span>
    <select name="tag" class="w-44 border border-slate-300 px-2 py-1 text-sm
                              focus:border-red-800 focus:outline-none">
      <option value="">any</option>
      {% for t in all_tags %}
      <option value="{{ t.slug }}" {% if tag == t.slug %}selected{% endif %}>{{ t.name }}</option>
      {% endfor %}
    </select>
  </label>
```

and add `or tag` to the two `{% if q or verified or mutual or min_followers is not none %}`
conditions (the "clear" link and the empty-state message) so both notice it.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_tags.py tests/test_web.py -q && uv run pytest -q`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add sonde/db/store.py sonde/web/app.py sonde/web/templates/followers.html tests/test_tags.py
git commit -m "M25: filter the followers list by tag

EXISTS rather than a JOIN, so someone matching twice is listed once. Carried in
the filters dict, because a filter left out of it is silently dropped the
moment you sort or page."
```

---

### Task 11: Tags in the CSV export

**Files:**
- Modify: `sonde/db/store.py:1193` (`export_rows`)
- Modify: `sonde/web/app.py` — the `fields` list in `export_csv`
- Test: `tests/test_tags.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_the_export_carries_tags(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await store.create_group("Cohort")
    await store.create_group("Neighbours")
    await store.tag_actor("cohort", "did:plc:a")
    await store.tag_actor("neighbours", "did:plc:a")

    rows = await store.export_rows()

    assert rows[0]["tags"] == "cohort;neighbours"


async def test_the_csv_header_includes_tags(db, client):
    body = client.get("/export.csv").text
    assert "tags" in body.splitlines()[0]
```

- [ ] **Step 2: Run and confirm they fail**

Run: `uv run pytest tests/test_tags.py -k export or csv -v`
Expected: FAIL — `KeyError: 'tags'`

- [ ] **Step 3: Add the column**

In `export_rows`, add to the `SELECT` list, after the `is_private` expression:

```sql
                   ,(SELECT GROUP_CONCAT(g.slug, ';')
                       FROM group_members m
                       JOIN groups g ON g.id = m.group_id
                      WHERE m.did = a.did
                        AND COALESCE(m.confirmed, 1) = 1
                        AND g.archived_at IS NULL) AS tags
```

SQLite's plain `GROUP_CONCAT` has no ordering guarantee, so sort in Python.
Add this loop immediately before `export_rows` returns — a CSV people diff
must not reshuffle its own columns between runs:

```python
    for row in rows:
        row["tags"] = ";".join(sorted(row["tags"].split(";"))) if row["tags"] else ""
```

- [ ] **Step 4: Add the field to the CSV writer**

In `sonde/web/app.py`, in `export_csv`, append `"tags"` to the end of the
`fields` list.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_tags.py -q && uv run pytest -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add sonde/db/store.py sonde/web/app.py tests/test_tags.py
git commit -m "M25: tags in the CSV export, sorted so the file diffs cleanly"
```

---

### Task 12: The selection count, and proving it fits on a phone

**Files:**
- Modify: `sonde/web/templates/base.html` (the existing `<script>` block)
- Modify: `tests/test_responsive.py`
- Test: `tests/test_tags.py`, plus the browser eval

A checkbox column is going onto two of the widest tables on the site. The static
tests cannot prove a page fits — only the browser can, and only against a
populated database.

- [ ] **Step 1: Write the failing static test**

In `tests/test_responsive.py`, add:

```python
def test_batch_forms_keep_their_table_scrollable():
    """The batch form wraps the table. If the overflow-x-auto div ends up
    OUTSIDE the form instead of inside it, the table stops scrolling on a phone
    and the page grows a horizontal scrollbar instead."""
    from pathlib import Path

    for name in ("followers.html", "groups.html"):
        text = Path("sonde/web/templates") / name
        body = text.read_text()
        if "data-batch" not in body:
            continue
        form_at = body.index("data-batch")
        assert "overflow-x-auto" in body[form_at:], (
            f"{name}: the scroll wrapper must be inside the batch form")
```

- [ ] **Step 2: Run the responsive suite**

Run: `uv run pytest tests/test_responsive.py -v`
Expected: PASS if Tasks 8 and 9 placed the wrapper correctly; FAIL tells you the
form and the scroll div are nested the wrong way round.

- [ ] **Step 3: Add the enhancement script**

In `sonde/web/templates/base.html`, inside the existing `<script>` block, after
the activity poller's closing `})();`, add a second IIFE:

```javascript
  (function () {
    // Progressive enhancement only. Without this the checkboxes and both
    // submit buttons still work; the count just reads 0 and select-all does
    // nothing. Nothing here is load-bearing.
    document.querySelectorAll("form[data-batch]").forEach(function (form) {
      const boxes = form.querySelectorAll('input[name="did"]');
      const count = form.querySelector("[data-selection-count]");
      const all   = form.querySelector("[data-select-all]");

      function recount() {
        if (!count) return;
        count.textContent = form.querySelectorAll('input[name="did"]:checked').length;
      }

      boxes.forEach((box) => box.addEventListener("change", recount));
      if (all) {
        all.addEventListener("change", function () {
          boxes.forEach((box) => { box.checked = all.checked; });
          recount();
        });
      }
      recount();
    });
  })();
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 5: Run the mobile eval against a POPULATED database**

Start a server pointed at real data, then run the eval. `DB_PATH`, not
`SONDE_DB_PATH` — the wrong name silently starts on an empty `./sonde.db`,
every page renders its empty state, and the eval passes while testing nothing.
Four real overflow bugs hid behind exactly that mistake in M24.

```bash
DB_PATH=/path/to/real.db uv run sonde serve &
DB_PATH=/path/to/real.db uv run --with playwright python -m evals.mobile_check
```

Expected: no horizontal overflow at 320px on `/followers` or `/groups`.

If a table overflows: the fix is `min-w-0` on the flex or grid *item*, not
`overflow-x: hidden` on the body — that hides the symptom, keeps the broken
layout, and blinds the eval by clipping the evidence it measures.

- [ ] **Step 6: Commit**

```bash
git add sonde/web/templates/base.html tests/test_responsive.py
git commit -m "M25: live selection count, and the mobile eval for two new columns

The script is pure enhancement - the checkboxes and both buttons work without
it. A static test pins the scroll wrapper inside the batch form, because a form
nested the wrong way round stops the table scrolling and grows the page a
horizontal scrollbar instead."
```

---

### Task 13: The digest measurement gate

**Do not build the digest change before running the measurement.** An arrival
has no hand-tags by definition and may have no rule hits either, because the
grouping set is the top 500 by influence plus verified accounts and most
arrivals are neither. This is the M24 precedent: two visualisations were killed
by measuring what data existed before drawing them.

**Files:**
- Modify (conditionally): `sonde/notify/digest.py:78`, its template, `tests/test_digest.py`
- Modify: `docs/superpowers/specs/2026-07-28-groups-as-tags-design.md` — record the result
- Modify: `PLAN.md` — the M25 milestone row

- [ ] **Step 1: Run the measurement on ubuntuplex**

Against the production database, not a local file:

```sql
SELECT COUNT(*) AS arrivals_30d,
       SUM(CASE WHEN EXISTS (SELECT 1 FROM group_members m
                              WHERE m.did = fs.did
                                AND COALESCE(m.confirmed,1) = 1) THEN 1 ELSE 0 END)
         AS with_a_group
  FROM follower_state fs
 WHERE COALESCE(fs.followed_at, fs.first_seen_at) >= date('now','-30 day');
```

- [ ] **Step 2: Apply the decision rule**

If `with_a_group / arrivals_30d` is **below 0.5**, the line is blank most days.
**Drop the digest change**, record the measured numbers in the design doc's
measurement-gate section, and skip to Step 4.

If it is **at or above 0.5**, continue to Step 3.

- [ ] **Step 3: Only if the gate passed — add tags to arrivals**

In `sonde/notify/digest.py`, where `digest.arrivals` is built (line 78), attach
each arrival's tags:

```python
    for arrival in digest.arrivals:
        arrival["tags"] = [g["name"] for g in await store.groups_for(arrival["did"])]
```

and render them in the `NEW FOLLOWERS` section, after the handle, as
`(tag, tag)` — omitted entirely when the list is empty, so a tagless arrival
does not render an empty pair of brackets.

Add to `tests/test_digest.py`, following the fixture style already in that file:

```python
async def test_a_tagged_arrival_shows_its_tags(db):
    from tests.test_groups import add

    await add("did:plc:new", is_verified=True)
    await store.create_group("Neighbours")
    await store.tag_actor("neighbours", "did:plc:new")

    body = render(await build_digest())

    assert "Neighbours" in body


async def test_an_arrival_with_no_tags_renders_no_empty_brackets(db):
    """Most arrivals have no tags at all. A bare '()' on every line would make
    the digest worse than not having the feature."""
    from tests.test_groups import add

    await add("did:plc:plain", is_verified=True)

    body = render(await build_digest())

    assert "()" not in body
```

Use whatever the file's existing build-and-render helpers are actually called —
read the top of `tests/test_digest.py` and match them rather than introducing
`build_digest`/`render` if those are not the real names.

- [ ] **Step 4: Record the outcome and close the milestone**

Add the measured numbers to the design doc under "Measurement gate", replacing
the "unknown" wording with what was actually found.

Add the M25 row to the milestone table in `PLAN.md`, after the M24 row:

```markdown
| M25 | Groups become tags | ✅ done | Hand decisions outrank every job; archive rather than delete, because seeded slugs resurrect |
```

- [ ] **Step 5: Full verification before calling it done**

```bash
uv run pytest -q
```

Expected: every test passes. State the number.

- [ ] **Step 6: Commit**

```bash
git add PLAN.md docs/superpowers/specs/2026-07-28-groups-as-tags-design.md sonde/notify/digest.py tests/test_digest.py
git commit -m "M25: the digest gate, measured"
```

Write the real numbers into the commit body — how many arrivals in thirty days,
how many carried a group, and whether that built the change or killed it. A
gate whose result is not written down has to be measured again by the next
person.

---

## Deployment

`make deploy`, never bare `docker compose` — `SERVICE_HOST` is only injected by
the Makefile, and without it Traefik's router rule becomes `Host(``)` and the
site 404s while `docker ps` shows a perfectly healthy container. Then
`make verify` to check the label.

The two new columns reach production through `_migrate` on startup. Confirm
after deploying:

```bash
curl -s https://<host>/healthz | jq .
```

and load `/groups` — a page that renders means both `ALTER TABLE`s applied.
