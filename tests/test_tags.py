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
    assert ("merged_into", "TEXT") in store.MIGRATIONS["groups"]
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
    """'journalists' is one of the 15 slugs in sonde/circles.py, and
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

    response = client.post("/circles/apply", data={
        "did": ["did:plc:a", "did:plc:b"], "tag": "Cohort", "action": "add",
    }, follow_redirects=False)

    assert response.status_code == 303
    assert {m["did"] for m in await store.group_members("cohort")} == {
        "did:plc:a", "did:plc:b"}


async def test_the_batch_endpoint_creates_the_tag_when_adding(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    client.post("/circles/apply", data={"did": ["did:plc:a"], "tag": "Brand new",
                                       "action": "add"}, follow_redirects=False)

    assert "brand-new" in {g["slug"] for g in await store.group_names()}


async def test_removing_with_an_unknown_tag_creates_nothing(db, client):
    """Only adding may create. Otherwise a typo in a remove conjures an empty
    tag out of nowhere."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    client.post("/circles/apply", data={"did": ["did:plc:a"], "tag": "Typo",
                                       "action": "remove"}, follow_redirects=False)

    assert await store.group_names() == []


async def test_the_batch_endpoint_returns_you_to_the_page_you_came_from(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    response = client.post(
        "/circles/apply", data={"did": ["did:plc:a"], "tag": "X", "action": "add"},
        headers={"referer": "http://testserver/followers?page=2"},
        follow_redirects=False)

    assert response.headers["location"].startswith("/followers?page=2")


async def test_a_referer_pointing_off_site_cannot_redirect_off_site(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    response = client.post(
        "/circles/apply", data={"did": ["did:plc:a"], "tag": "X", "action": "add"},
        headers={"referer": "https://evil.example/phish"},
        follow_redirects=False)

    assert response.headers["location"].startswith("/phish")


async def test_creating_a_tag_whose_slug_is_archived_redirects_with_a_notice(db, client):
    """The route's half of this. Whether /circles then RENDERS the notice is
    Task 8's assertion — the template does not know about `notice` yet, so do
    not assert on page text here."""
    await store.create_group("Gone")
    await store.archive_group("gone")

    response = client.post("/circles/new", data={"name": "Gone"},
                           follow_redirects=False)

    assert response.status_code == 303
    assert "notice=archived" in response.headers["location"]
    assert "slug=gone" in response.headers["location"]


async def test_creating_a_brand_new_tag_redirects_straight_to_it(db, client):
    response = client.post("/circles/new", data={"name": "Fresh"},
                           follow_redirects=False)

    assert response.headers["location"] == "/circles?slug=fresh"


async def test_renaming_through_the_route(db, client):
    await store.create_group("Before")
    client.post("/circles/before/rename", data={"name": "After"},
                follow_redirects=False)

    assert {g["name"] for g in await store.group_summary()} == {"After"}


async def test_archiving_and_restoring_through_the_route(db, client):
    await store.create_group("Temporary")

    client.post("/circles/temporary/archive", follow_redirects=False)
    assert await store.group_names() == []

    client.post("/circles/temporary/archive?undo=true", follow_redirects=False)
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


# --------------------------------------------------------- the /circles page

async def test_the_groups_page_can_rename_archive_and_create(db, client):
    await store.create_group("Cohort")

    page = client.get("/circles?slug=cohort")

    assert 'action="/circles/cohort/rename"' in page.text
    assert 'action="/circles/cohort/archive"' in page.text
    assert 'action="/circles/new"' in page.text


async def test_the_groups_page_lists_what_can_be_restored(db, client):
    await store.create_group("Gone")
    await store.archive_group("gone")

    page = client.get("/circles")

    assert 'action="/circles/gone/archive?undo=true"' in page.text


async def test_the_member_table_can_untag_a_row_the_machine_got_right(db, client):
    """Before M25 the 'remove' control rendered only on unreviewed rows, so a
    correctly-classified person could not be removed at all."""
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await store.tag_actor("journalists", "did:plc:j")   # confirmed = 1

    page = client.get("/circles?slug=journalists")

    assert 'name="did" value="did:plc:j"' in page.text


async def test_the_archived_notice_renders(db, client):
    """Task 6 proved /circles/new redirects with notice=archived. This is the
    other half: the page has to actually say something when it arrives."""
    page = client.get("/circles?notice=archived&slug=gone")

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

    for url in ("/followers", "/circles?slug=journalists"):
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

    assert 'action="/circles/apply"' in page.text
    assert 'name="did" value="did:plc:a"' in page.text


async def test_the_follow_button_survives_on_batch_pages(db, client):
    """The reason nesting was avoided rather than worked around."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()

    for url in ("/followers", "/circles?slug=journalists"):
        assert "/followers/did:plc:a/follow" in client.get(url).text, (
            f"{url} lost the one-click follow-back control")


# ------------------------------------------------- filtering by tag

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


async def test_someone_in_a_tag_is_listed_once(db):
    """EXISTS rather than a JOIN. A join against group_members would emit a row
    per membership, so anyone matching twice would appear twice in the list."""
    from tests.test_groups import add

    await add("did:plc:both", is_verified=True)
    await store.create_group("One")
    await store.create_group("Two")
    await store.tag_actor("one", "did:plc:both")
    await store.tag_actor("two", "did:plc:both")

    assert len(await store.ranked_followers(tag="one")) == 1


async def test_the_tag_filter_survives_paging_and_sorting(db, client):
    """Every link on the page has to carry the current filters. There is a
    comment in the route saying so, because dropping one silently broke sorting
    once already."""
    from tests.test_groups import add

    await add("did:plc:in", is_verified=True)
    await store.create_group("Cohort")
    await store.tag_actor("cohort", "did:plc:in")

    page = client.get("/followers?tag=cohort&order=followers")

    assert "tag=cohort" in page.text


# ---------------------------------- the dashboard shows arrivals only

async def test_the_dashboard_lists_follows_and_not_unfollows(db, client):
    """Asked for directly: departures are noise on the front page. They are
    still recorded and still on /changes — this only changes what greets you."""
    from tests.test_groups import add

    await add("did:plc:gone", is_verified=True)
    await store.add_event("did:plc:gone", "departed")
    await add("did:plc:new", is_verified=True)
    await store.add_event("did:plc:new", "followed")

    events = (await store.dashboard_stats())["recent_changes"]

    assert [e["event"] for e in events] == ["followed"]


async def test_a_returning_follower_still_counts_as_an_arrival(db):
    from tests.test_groups import add

    await add("did:plc:back", is_verified=True)
    await store.add_event("did:plc:back", "returned")

    assert [e["event"] for e in (await store.dashboard_stats())["recent_changes"]] == ["returned"]


async def test_sondes_own_follow_back_is_not_an_arrival(db):
    """followed_back is us following them. It is not someone arriving."""
    from tests.test_groups import add

    await add("did:plc:x", is_verified=True)
    await store.add_event("did:plc:x", "followed_back")

    assert (await store.dashboard_stats())["recent_changes"] == []


async def test_the_full_timeline_still_shows_departures(db):
    from tests.test_groups import add

    await add("did:plc:gone", is_verified=True)
    await store.add_event("did:plc:gone", "departed")

    assert [e["event"] for e in await store.recent_changes(50)] == ["departed"]


# ------------------------------------------------------- CSV export

async def test_the_export_carries_tags(db):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await store.create_group("Neighbours")
    await store.create_group("Cohort")
    await store.tag_actor("neighbours", "did:plc:a")
    await store.tag_actor("cohort", "did:plc:a")

    rows = await store.export_rows()

    assert rows[0]["tags"] == "cohort;neighbours"


async def test_the_export_leaves_untagged_people_blank(db):
    from tests.test_groups import add

    await add("did:plc:none", is_verified=True)

    assert (await store.export_rows())[0]["tags"] == ""


async def test_the_export_omits_untagged_and_archived(db):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await store.create_group("Gone")
    await store.tag_actor("gone", "did:plc:a")
    await store.archive_group("gone")

    assert (await store.export_rows())[0]["tags"] == ""


async def test_the_csv_header_includes_tags(db, client):
    assert "tags" in client.get("/export.csv").text.splitlines()[0]


# ------------------------------------------------- redirect safety

async def test_a_scheme_relative_referer_cannot_redirect_off_site(db, client):
    """`referer` is attacker-controllable and lands in a Location header.

    "//evil.example/phish" contains no "://", so a check for that alone waves it
    through — and a browser resolves a scheme-relative Location off-site. The
    bug shipped with follow_back in M23 and M25 took it from one call site to
    six, which is what made it worth fixing rather than noting.
    """
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    for evil in ("//evil.example/phish", "/\\evil.example/phish",
                 "https://evil.example//phish", "javascript:alert(1)"):
        response = client.post(
            "/circles/apply",
            data={"did": ["did:plc:a"], "tag": "X", "action": "add"},
            headers={"referer": evil}, follow_redirects=False)
        location = response.headers["location"]
        assert location.startswith("/"), f"{evil!r} -> {location!r}"
        assert not location.startswith("//"), f"{evil!r} -> {location!r}"
        assert "evil.example" not in location, f"{evil!r} -> {location!r}"


async def test_an_ordinary_referer_still_comes_back(db, client):
    """The guard must not throw away legitimate in-app returns — the whole
    point of the helper is landing you back where you were working."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    response = client.post(
        "/circles/apply", data={"did": ["did:plc:a"], "tag": "X", "action": "add"},
        headers={"referer": "http://testserver/followers?page=3&order=followers"},
        follow_redirects=False)

    assert response.headers["location"].startswith("/followers?page=3&order=followers")


# ============================================ previewing a candidate

async def seed_cluster(dids: list[str], label: str = "Unnamed community") -> int:
    """A cluster candidate is its stored member list — there is no rule."""
    import json as _json
    conn = await store._db()
    cur = await conn.execute(
        """INSERT INTO group_candidates
             (kind, term, label, member_count, why, members, first_seen_at)
           VALUES ('cluster', ?, ?, ?, 'follow the same accounts', ?, ?)""",
        (label.lower(), label, len(dids), _json.dumps(dids), store.utcnow()))
    await store.commit()
    return cur.lastrowid


async def seed_phrase(term: str, label: str) -> int:
    conn = await store._db()
    cur = await conn.execute(
        """INSERT INTO group_candidates
             (kind, term, label, member_count, why, first_seen_at)
           VALUES ('phrase', ?, ?, 0, 'bio phrase', ?)""",
        (term, label, store.utcnow()))
    await store.commit()
    return cur.lastrowid


async def test_a_cluster_preview_lists_its_stored_members(db):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await add("did:plc:b", is_verified=True)
    cid = await seed_cluster(["did:plc:a", "did:plc:b"])

    preview = await store.candidate_members(cid)

    assert {m["did"] for m in preview["members"]} == {"did:plc:a", "did:plc:b"}


async def test_a_rule_based_preview_is_recomputed_from_the_rule(db):
    """Phrase candidates store no members at all — they are rebuilt on demand,
    which is also what makes the preview reflect today's data."""
    from tests.test_groups import add

    await add("did:plc:w", is_verified=True, description="Public sector wonk")
    # A real phrase candidate is always a bigram — phrase_candidates only ever
    # emits "{a} {b}" — so a one-word term is not a case that occurs.
    cid = await seed_phrase("public sector", "Public Sector")

    preview = await store.candidate_members(cid)

    assert [m["did"] for m in preview["members"]] == ["did:plc:w"]
    assert "public sector" in preview["members"][0]["evidence"]


async def test_previewing_writes_nothing(db):
    """The whole point of a preview. Recomputing a rule-based candidate runs the
    same matcher the accept does, so it would be easy to write by accident."""
    from tests.test_groups import add

    await add("did:plc:w", is_verified=True, description="Public sector wonk")
    cid = await seed_phrase("public sector", "Public Sector")
    before = await store._scalar("SELECT COUNT(*) FROM group_members")
    groups_before = await store._scalar("SELECT COUNT(*) FROM groups")

    preview = await store.candidate_members(cid)

    # Without this the test passes for the wrong reason: a preview that matched
    # nobody writes nothing too.
    assert len(preview["members"]) == 1
    assert await store._scalar("SELECT COUNT(*) FROM group_members") == before
    assert await store._scalar("SELECT COUNT(*) FROM groups") == groups_before
    assert (await store.group_candidates())[0]["decided"] is None


async def test_the_preview_marks_members_who_have_departed(db):
    """A cluster's members were captured when it was found. Dropping the ones
    who left would hide that a candidate is half-stale."""
    from tests.test_groups import add

    await add("did:plc:here", is_verified=True)
    await add("did:plc:gone", is_verified=True)
    await store.mark_departed(["did:plc:gone"], "unfollow")
    cid = await seed_cluster(["did:plc:here", "did:plc:gone"])

    preview = await store.candidate_members(cid)

    assert preview["candidate"]["departed"] == 1
    assert {m["did"]: bool(m["is_current"]) for m in preview["members"]} == {
        "did:plc:here": True, "did:plc:gone": False}


async def test_the_preview_counts_members_it_cannot_resolve(db):
    """A DID with no stored profile cannot be rendered; a count that ignored it
    would not add up to the number on the candidate row."""
    from tests.test_groups import add

    await add("did:plc:known", is_verified=True)
    cid = await seed_cluster(["did:plc:known", "did:plc:never-seen"])

    preview = await store.candidate_members(cid)

    assert preview["candidate"]["members_found"] == 1
    assert preview["candidate"]["unresolved"] == 1


async def test_an_unknown_candidate_previews_as_nothing(db):
    assert await store.candidate_members(9999) is None


async def test_accepting_a_subset_creates_the_group_with_only_those(db):
    from tests.test_groups import add

    for did in ("did:plc:a", "did:plc:b", "did:plc:c"):
        await add(did, is_verified=True)
    cid = await seed_cluster(["did:plc:a", "did:plc:b", "did:plc:c"], "Cohort")

    slug = await store.decide_candidate(cid, True, dids=["did:plc:a", "did:plc:c"])

    assert {m["did"] for m in await store.group_members(slug)} == {
        "did:plc:a", "did:plc:c"}


async def test_a_hand_picked_member_survives_a_reclassify(db):
    """Picked by a person looking at a list, so it gets the same standing as any
    other hand tag — including immunity from the six-hourly job."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    cid = await seed_cluster(["did:plc:a"], "Cohort")
    slug = await store.decide_candidate(cid, True, dids=["did:plc:a"])

    await store.classify_groups()

    assert [m["did"] for m in await store.group_members(slug)] == ["did:plc:a"]


async def test_accepting_without_a_subset_still_takes_everything(db):
    """The list page's one-click accept must not change behaviour."""
    from tests.test_groups import add

    for did in ("did:plc:a", "did:plc:b"):
        await add(did, is_verified=True)
    cid = await seed_cluster(["did:plc:a", "did:plc:b"], "Cohort")

    slug = await store.decide_candidate(cid, True)

    assert len(await store.group_members(slug)) == 2


# ------------------------------------------------- the preview page

async def test_the_preview_page_renders_its_members(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    cid = await seed_cluster(["did:plc:a"], "Cohort")

    page = client.get(f"/circles/discover/{cid}")

    assert page.status_code == 200
    assert 'name="did" value="did:plc:a"' in page.text
    assert 'form="accept"' in page.text


async def test_the_preview_page_does_not_nest_forms(db, client):
    """who() renders follow-back as its own form, so the members table must not
    be wrapped — same trap as the batch bars."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    cid = await seed_cluster(["did:plc:a"], "Cohort")

    body = client.get(f"/circles/discover/{cid}").text

    depth = 0
    for chunk in body.split("<form")[1:]:
        depth += 1
        assert depth == 1, "the preview page nests a form inside another form"
        depth -= chunk.count("</form>")


async def test_ticking_nobody_creates_nothing(db, client):
    """An empty selection is a misclick, not a request for an empty group."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    cid = await seed_cluster(["did:plc:a"], "Cohort")

    response = client.post(f"/circles/discover/{cid}?accept=true",
                           data={"subset": "1"}, follow_redirects=False)

    assert "notice=nobody" in response.headers["location"]
    assert "cohort" not in {g["slug"] for g in await store.group_names()}


async def test_an_unknown_candidate_page_is_a_404(db, client):
    assert client.get("/circles/discover/4242").status_code == 404


async def test_a_candidate_matching_nobody_says_so(db, client):
    cid = await seed_phrase("nobodyhasthisinabio", "Nobody")

    page = client.get(f"/circles/discover/{cid}")

    assert page.status_code == 200
    assert "matches nobody now" in page.text


# ------------------------- the phrase matcher agrees with the proposer

async def add_post(did: str, text: str) -> None:
    conn = await store._db()
    await conn.execute(
        """INSERT INTO posts (did, uri, text, indexed_at, fetched_at)
           VALUES (?,?,?,?,?)""",
        (did, f"at://{did}/{abs(hash(text))}", text, store.utcnow(), store.utcnow()))
    await store.commit()


async def test_a_phrase_in_posts_is_matched_not_just_one_in_a_bio(db):
    """The proposer counted bios AND posts; the matcher read bios only. On the
    live list "data center" appeared in 21 people's posts and nobody's bio, so
    it was proposed with a count of 10 and would have created an empty group."""
    from tests.test_groups import add

    await add("did:plc:poster", is_verified=True, description="nothing relevant")
    await add_post("did:plc:poster", "touring a data center today")
    cid = await seed_phrase("data center", "Data Center")

    preview = await store.candidate_members(cid)

    assert [m["did"] for m in preview["members"]] == ["did:plc:poster"]


async def test_a_phrase_matches_across_a_dropped_stopword(db):
    """"wrote a book" tokenises to the bigram "wrote book", which appears
    nowhere verbatim. A literal LIKE found nobody, every time."""
    from tests.test_groups import add

    await add("did:plc:author", is_verified=True, description="I wrote a book")
    cid = await seed_phrase("wrote book", "Wrote Book")

    preview = await store.candidate_members(cid)

    assert [m["did"] for m in preview["members"]] == ["did:plc:author"]


async def test_a_phrase_does_not_match_across_punctuation(db):
    """The matcher must not become so permissive that it matches things the
    proposer would never have counted."""
    from tests.test_groups import add

    await add("did:plc:x", is_verified=True, description="digital rights, human dignity")
    cid = await seed_phrase("rights human", "Rights Human")

    assert (await store.candidate_members(cid))["members"] == []


async def test_accepting_a_phrase_candidate_populates_it_with_who_was_previewed(db):
    """The preview and the accept run the same matcher, so what you saw is what
    you get. That was not true before: they were separate code."""
    from tests.test_groups import add

    await add("did:plc:poster", is_verified=True, description="nothing relevant")
    await add_post("did:plc:poster", "touring a data center today")
    cid = await seed_phrase("data center", "Data Center")
    previewed = {m["did"] for m in (await store.candidate_members(cid))["members"]}

    slug = await store.decide_candidate(cid, True)

    assert {m["did"] for m in await store.group_members(slug)} == previewed
    assert previewed == {"did:plc:poster"}


# ================================================== merging two groups

async def test_merging_moves_members_and_archives_the_source(db):
    from tests.test_groups import add

    for did in ("did:plc:a", "did:plc:b"):
        await add(did, is_verified=True)
    await store.create_group("Game Dev")
    await store.create_group("Game industry")
    await store.tag_actor("game-dev", "did:plc:a")
    await store.tag_actor("game-dev", "did:plc:b")

    result = await store.merge_groups("game-dev", "game-industry")

    assert result["status"] == "merged" and result["moved"] == 2
    assert {m["did"] for m in await store.group_members("game-industry")} == {
        "did:plc:a", "did:plc:b"}
    assert "game-dev" not in {g["slug"] for g in await store.group_summary()}


async def test_the_source_is_archived_not_deleted(db):
    """Its URL keeps resolving and the trail to where its people went survives."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await store.create_group("Game Dev")
    await store.create_group("Game industry")
    await store.tag_actor("game-dev", "did:plc:a")

    await store.merge_groups("game-dev", "game-industry")

    archived = {a["slug"]: a for a in await store.archived_groups()}
    assert archived["game-dev"]["merged_into"] == "game-industry"
    assert archived["game-dev"]["merged_into_name"] == "Game industry"


async def test_a_merge_does_not_resurrect_someone_untagged_from_the_target(db):
    """The guarantee the whole tagging model rests on. Absorbing a group must
    not overrule a hand decision already made on the target."""
    from tests.test_groups import add

    await add("did:plc:no", is_verified=True)
    await store.create_group("Source")
    await store.create_group("Target")
    await store.tag_actor("source", "did:plc:no")
    await store.tag_actor("target", "did:plc:no")
    await store.untag_actor("target", "did:plc:no")

    await store.merge_groups("source", "target")

    assert await store.group_members("target") == []


async def test_the_targets_own_evidence_wins_on_a_shared_member(db):
    """They were already a member of the target on the target's own terms.
    Overwriting that would discard why they are there."""
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await store.create_group("Hacks")
    await store.tag_actor("hacks", "did:plc:j")

    await store.merge_groups("hacks", "journalists")

    row = (await store.group_members("journalists"))[0]
    assert row["tier"] == "wikidata"
    assert row["evidence"] == "Wikidata occupation: journalist"


async def test_a_merged_member_keeps_the_reason_it_came_with(db):
    """Someone the target did not already have arrives with the source's
    evidence, because that is the true record of why they are in the group."""
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, wikidata_occupations='["journalist"]')
    await store.classify_groups()
    await store.create_group("Hacks")

    await store.merge_groups("journalists", "hacks")

    row = (await store.group_members("hacks"))[0]
    assert row["tier"] == "wikidata"
    assert row["evidence"] == "Wikidata occupation: journalist"


async def test_a_group_cannot_be_merged_into_itself(db):
    await store.create_group("Solo")
    assert (await store.merge_groups("solo", "solo"))["status"] == "same-group"


async def test_merging_into_an_archived_group_is_refused(db):
    """It would move people somewhere invisible that also stops gaining members."""
    await store.create_group("Source")
    await store.create_group("Gone")
    await store.archive_group("gone")

    assert (await store.merge_groups("source", "gone"))["status"] == "target-archived"


async def test_merging_an_unknown_group_is_refused(db):
    await store.create_group("Real")
    assert (await store.merge_groups("real", "imaginary"))["status"] == "unknown-group"
    assert (await store.merge_groups("imaginary", "real"))["status"] == "unknown-group"


async def test_a_merged_member_survives_a_reclassify(db):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await store.create_group("Source")
    await store.create_group("Target")
    await store.tag_actor("source", "did:plc:a")
    await store.merge_groups("source", "target")

    await store.classify_groups()

    assert [m["did"] for m in await store.group_members("target")] == ["did:plc:a"]


# ------------------------------------------------------ the overlap table

async def test_overlaps_report_containment_of_the_smaller_group(db):
    from tests.test_groups import add

    for did in ("did:plc:a", "did:plc:b", "did:plc:c"):
        await add(did, is_verified=True)
    await store.create_group("Small")
    await store.create_group("Large")
    await store.tag_actor("small", "did:plc:a")
    for did in ("did:plc:a", "did:plc:b", "did:plc:c"):
        await store.tag_actor("large", did)

    row = (await store.group_overlaps())[0]

    assert row["shared"] == 1
    assert row["containment"] == 100
    assert (row["source"], row["target"]) == ("small", "large")


async def test_overlaps_ignore_archived_groups(db):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await store.create_group("One")
    await store.create_group("Two")
    await store.tag_actor("one", "did:plc:a")
    await store.tag_actor("two", "did:plc:a")
    assert await store.group_overlaps()

    await store.archive_group("two")

    assert await store.group_overlaps() == []


async def test_groups_sharing_nobody_are_not_listed(db):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await add("did:plc:b", is_verified=True)
    await store.create_group("One")
    await store.create_group("Two")
    await store.tag_actor("one", "did:plc:a")
    await store.tag_actor("two", "did:plc:b")

    assert await store.group_overlaps() == []


async def test_the_groups_page_offers_a_merge(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await store.create_group("Small")
    await store.create_group("Large")
    await store.tag_actor("small", "did:plc:a")
    await store.tag_actor("large", "did:plc:a")

    page = client.get("/circles?slug=small")

    assert 'action="/circles/small/merge"' in page.text
    assert "Circles that share members" in page.text


async def test_merging_through_the_route(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await store.create_group("Small")
    await store.create_group("Large")
    await store.tag_actor("small", "did:plc:a")

    response = client.post("/circles/small/merge", data={"into": "large"},
                           follow_redirects=False)


    assert "notice=merged-1-1" in response.headers["location"]
    assert [m["did"] for m in await store.group_members("large")] == ["did:plc:a"]


async def test_a_wholly_redundant_merge_says_nothing_moved(db, client):
    """The first real merge moved 0 of 17, because game-dev sat entirely inside
    game-industry. "0 moved" alone reads as a failure; it is the opposite."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    await store.create_group("Small")
    await store.create_group("Large")
    await store.tag_actor("small", "did:plc:a")
    await store.tag_actor("large", "did:plc:a")

    response = client.post("/circles/small/merge", data={"into": "large"},
                           follow_redirects=False)
    page = client.get(response.headers["location"])

    assert "notice=merged-0-1" in response.headers["location"]
    assert "already here" in page.text


# ------------------------------------------------- bios where they help

async def test_the_circle_member_table_shows_bios(db, client):
    """Deciding whether somebody belongs in a circle needs their own words, not
    just a handle and a score."""
    from tests.test_groups import add

    await add("did:plc:j", is_verified=True, description="Reporter at a paper",
              wikidata_occupations='["journalist"]')
    await store.classify_groups()

    page = client.get("/circles?slug=journalists")

    assert "Reporter at a paper" in page.text


async def test_the_candidate_preview_shows_bios(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True, description="Makes games for a living")
    cid = await seed_cluster(["did:plc:a"], "Cohort")

    page = client.get(f"/circles/discover/{cid}")

    assert "Makes games for a living" in page.text


async def test_a_long_bio_is_truncated_rather_than_widening_the_table(db, client):
    """These are the widest tables in the app. An untruncated 300-character bio
    is how a row stops fitting on a phone."""
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True, description="x" * 400)
    cid = await seed_cluster(["did:plc:a"], "Cohort")

    page = client.get(f"/circles/discover/{cid}")

    assert "…" in page.text
    assert "x" * 400 not in page.text.replace('title="' + "x" * 400 + '"', "")


async def test_someone_with_no_bio_renders_nothing_extra(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True, description=None)
    cid = await seed_cluster(["did:plc:a"], "Cohort")

    assert client.get(f"/circles/discover/{cid}").status_code == 200


# ------------------------------------- Groups became Circles on 2026-07-29

async def test_old_group_urls_still_resolve(db, client):
    """Bookmarks, and links in digest emails already sent, must keep working.
    308 rather than 302 so the new address is learned and the method survives."""
    for old, new in (
        ("/groups", "/circles"),
        ("/groups/discover", "/circles/discover"),
        ("/groups/discover/7", "/circles/discover/7"),
    ):
        response = client.get(old, follow_redirects=False)
        assert response.status_code == 308, old
        assert response.headers["location"] == new, old


async def test_an_old_url_keeps_its_query_string(db, client):
    """/groups?slug=journalists is the shape of every real bookmark; dropping
    the query would land you on the index having lost what you asked for."""
    response = client.get("/groups?slug=journalists&order=followers",
                          follow_redirects=False)

    assert response.headers["location"] == "/circles?slug=journalists&order=followers"


async def test_the_nav_says_circles(db, client):
    page = client.get("/circles")
    assert ">Circles<" in page.text
    assert ">Groups<" not in page.text
