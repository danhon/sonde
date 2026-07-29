"""M25 — groups as tags: hand decisions outrank every job."""

import dataclasses

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


# ----------------------------- job paths must also skip archived


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


async def test_hand_tagging_outside_the_grouping_set_widens_the_denominator(db, monkeypatch):
    """The composition chart says 'N of M in scope'. A hand-applied tag is a
    fact about a person however uninfluential they are, so manual members exist
    outside the top-500-plus-verified target set — and a denominator ignoring
    them would under-report from the first tag onwards."""
    from tests.test_groups import add

    # top_n=500 does not discriminate against a single-row test database — the
    # LIMIT never binds when there is nothing to cut off, so an unverified,
    # uninfluential actor would land in the target set anyway. Pin top_n to 0
    # so only verified_status='valid' can qualify, which is what makes
    # did:plc:nobody genuinely outside the grouping set here, matching the
    # many-thousand-follower production database this composes against.
    monkeypatch.setattr(store, "settings",
                         dataclasses.replace(store.settings, posts_top_n=0))

    # Neither verified nor influential: outside the grouping set by construction.
    await add("did:plc:nobody")
    before = (await store.composition())["in_scope"]

    await store.create_group("Neighbours")
    await store.tag_actor("neighbours", "did:plc:nobody")

    assert (await store.composition())["in_scope"] == before + 1


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


async def test_creating_a_tag_whose_slug_is_archived_redirects_with_a_notice(db, client):
    """The route's half of this. Whether /groups then RENDERS the notice is
    Task 8's assertion — the template does not know about `notice` yet, so do
    not assert on page text here."""
    await store.create_group("Gone")
    await store.archive_group("gone")

    response = client.post("/groups/new", data={"name": "Gone"},
                           follow_redirects=False)

    assert response.status_code == 303
    assert "notice=archived" in response.headers["location"]
    assert "slug=gone" in response.headers["location"]


async def test_creating_a_brand_new_tag_redirects_straight_to_it(db, client):
    response = client.post("/groups/new", data={"name": "Fresh"},
                           follow_redirects=False)

    assert response.headers["location"] == "/groups?slug=fresh"


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


# --------------------------------------------------------- the /groups page

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


async def test_the_archived_notice_renders(db, client):
    """Task 6 proved /groups/new redirects with notice=archived. This is the
    other half: the page has to actually say something when it arrives."""
    page = client.get("/groups?notice=archived&slug=gone")

    assert page.status_code == 200
    assert "archived" in page.text.lower()


# -------------------------------------------- no nested forms, ever again

async def test_no_batch_page_ever_nests_a_form(db, client):
    """A <form> inside another <form> is invalid HTML and browsers silently drop
    the inner one — the control renders perfectly and does nothing. Real, not
    theoretical: who() emits follow-back as its own form, so any page wrapping
    its follower table in the batch form would break follow-back with no visible
    symptom. Hence form="batch" rather than a wrapper."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()

    for url in ("/followers", "/groups?slug=journalists"):
        body = client.get(url).text
        depth = 0
        for chunk in body.split("<form")[1:]:
            depth += 1
            assert depth == 1, f"{url} nests a form inside another form"
            depth -= chunk.count("</form>")
            assert depth >= 0, f"{url} has an unbalanced </form>"


async def test_the_followers_table_can_batch_tag(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)

    page = client.get("/followers")

    assert 'action="/groups/apply"' in page.text
    assert 'name="did" value="did:plc:a"' in page.text


async def test_the_follow_button_survives_on_batch_pages(db, client):
    """The reason nesting was avoided rather than worked around."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()

    for url in ("/followers", "/groups?slug=journalists"):
        assert "/followers/did:plc:a/follow" in client.get(url).text, (
            f"{url} lost the one-click follow-back control")
