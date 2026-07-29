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

async def test_archiving_stamps_the_column_and_restoring_clears_it(db):
    """This task owns the writer only. Whether the reads then hide the tag is
    Task 3's job and is asserted there — do not reach for group_summary here,
    it does not filter archived rows yet."""
    await store.create_group("Temporary")

    assert await store.archive_group("temporary")
    assert [g["slug"] for g in await store.archived_groups()] == ["temporary"]

    assert await store.archive_group("temporary", archived=False)
    assert await store.archived_groups() == []


async def test_archiving_a_tag_that_does_not_exist_reports_failure(db):
    assert not await store.archive_group("never-existed")


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
