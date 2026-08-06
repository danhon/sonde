"""What the API returns, and — mostly — what it must never return.

The load-bearing test here is `test_no_response_carries_a_field_nobody_chose`.
Every other test can pass while a new column on `actors` quietly appears in
every person record, because `SELECT a.*` hands the serialiser the whole row and
nothing in Python objects to a dict growing a key. That test declares the shape
and fails when the shape changes, which is the only way a field gets a decision
made about it before a client sees it.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from sonde.db import store
from sonde.web.app import create_app

TOKEN = "w" * 40
READ_ONLY = "r" * 40


async def _seed(path: str) -> None:
    """A small, awkward roster: a plain follower, a private-flagged one, a
    hidden one, a departed one, and the operator themselves."""
    store.set_db_path(path)
    await store.connect()

    async def person(did, handle, **extra):
        await store.upsert_actor({
            "did": did, "handle": handle,
            "displayName": handle.split(".")[0].title(),
            "description": "Reporter. https://ft.com/profile",
            **extra})

    await person("did:plc:alice", "alice.test")
    await person("did:plc:bob", "bob.test")
    await person("did:plc:carol", "carol.test",
                 labels=[{"val": "!no-unauthenticated"}])
    await person("did:plc:hidden", "hidden.test")
    await person("did:plc:gone", "gone.test")
    await person("did:plc:danhon", "danhon.com")

    for rank, did in enumerate(["did:plc:alice", "did:plc:bob", "did:plc:carol",
                                "did:plc:hidden", "did:plc:gone",
                                "did:plc:danhon"]):
        await store.mark_seen(did, rank)

    # Hidden by a moderation list — the case ACCESS.md keeps off every surface,
    # because publishing it republishes an accusation about someone named.
    await store.set_ignored("did:plc:hidden", True, reason="moderation")
    await store.mark_departed(["did:plc:gone"], reason="unknown")

    await store.add_event("did:plc:alice", "followed")
    await store.add_event("did:plc:bob", "handle_changed",
                          detail="old.test → bob.test")
    # sonde's own write. Its detail is the follow record's URI, which is the
    # reason `followed_back` is not a public event.
    await store.record_my_follow("did:plc:alice",
                                 "at://did:plc:danhon/app.bsky.graph.follow/xyz")

    await store.create_group("Journalists")
    await store.tag_actor("journalists", "did:plc:alice")
    await store.tag_actor("journalists", "did:plc:hidden")
    await store.create_group("Retired")
    await store.archive_group("retired")

    await store.record_interactions([
        {"did": "did:plc:alice", "direction": "inbound", "kind": "reply",
         "uri": "at://x/1", "subject": "at://me/1", "thread": "at://me/1",
         "occurred_at": "2026-08-01T00:00:00+00:00"},
    ])
    # Bio links are derived by a job rather than written by the sweep, so
    # without this the `links` field is empty for everyone and the test that
    # checks it would pass against nothing.
    await store.refresh_link_signals()
    await store.commit()
    await store.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDE_API_TOKENS",
                       f"crm:{TOKEN}:write,reader:{READ_ONLY}")
    path = str(tmp_path / "api.db")
    # Seeded in its own loop and closed again: the connection is a module
    # global, and one held open across two event loops deadlocks on the first
    # query TestClient makes.
    asyncio.run(_seed(path))
    store.set_db_path(path)
    with TestClient(create_app(), base_url="http://sonde.test") as c:
        yield c
    store.set_db_path(None)


H = {"authorization": f"Bearer {TOKEN}"}


def get(client, path):
    r = client.get(path, headers=H)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


def dids(payload) -> set[str]:
    return {p["did"] for p in payload["data"]}


# ---------------------------------------------------------- what is withheld

PERSON_KEYS = {
    "did", "handle", "display_name", "description", "avatar_url",
    "account_created_at", "followers_count", "follows_count", "posts_count",
    "last_post_at", "verified", "verified_status", "trusted_verifier",
    "is_private", "influence_score", "relationship_score", "following_since",
    "following_since_exact", "first_seen_at", "list_rank", "is_mutual",
    "circles", "organisation", "last_interaction_at", "bsky_url", "sonde_url",
}

DETAIL_KEYS = PERSON_KEYS | {
    "score", "relationship", "affiliations", "wikidata", "links",
    "interactions", "shared_connections", "posts", "events",
}


def _keys(node, out: set[str]) -> set[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            out.add(key)
            _keys(value, out)
    elif isinstance(node, list):
        for item in node:
            _keys(item, out)
    return out


def test_a_person_record_has_exactly_the_declared_fields(client):
    (person,) = [p for p in get(client, "/api/v1/people")["data"]
                 if p["did"] == "did:plc:alice"]
    assert set(person) == PERSON_KEYS


def test_a_person_detail_has_exactly_the_declared_fields(client):
    detail = get(client, "/api/v1/people/alice.test")["data"]
    assert set(detail) == DETAIL_KEYS


def test_no_response_carries_a_field_nobody_chose(client):
    """The whole point of building responses from dict literals.

    `ranked_followers` selects `a.*`, so the serialiser is handed
    `ignored_reason`, `unservable_since`, `score_components`, `follow_uri` and
    every column added to `actors` from here on. A blocklist would let each new
    one through on the day it landed; this declares the allowlist and fails
    when a key appears outside it.
    """
    allowed = DETAIL_KEYS | {
        # Envelope
        "data", "next_cursor", "error", "code", "message",
        # Nested shapes, each named in API.md
        "name", "role", "confidence", "method", "slug", "tier", "evidence",
        "value", "components", "organisation", "kind", "note", "url",
        "source_url", "id", "occupations", "employers", "past_employers",
        "positions", "wikipedia_title", "wikipedia_views_30d", "host",
        "totals", "recent", "inbound", "outbound", "last_at", "direction",
        "occurred_at", "total", "exact", "people", "uri", "text",
        "indexed_at", "like_count", "repost_count", "reply_count",
        "is_repost", "event", "detected_at", "reason", "detail", "query",
        "status", "i_follow", "members", "unreviewed", "people_url",
        # /meta
        "api_version", "client", "scopes", "actor", "build", "counts",
        "tracked", "verified", "private", "departed", "mutuals", "ignored",
        "reported", "last_sync", "last_sync_age_seconds", "limits",
        "default_limit", "max_limit", "max_resolve", "started_at",
        "ended_at", "completed", "pages_fetched", "actors_seen",
        "new_followers", "lost_followers", "profiles_hydrated", "api_calls",
    } | set(store.INTERACTION_KINDS)  # `interactions.totals` is keyed by kind
    for path in ("/api/v1/meta", "/api/v1/people", "/api/v1/people/alice.test",
                 "/api/v1/circles", "/api/v1/changes",
                 "/api/v1/resolve?handle=alice.test"):
        found = _keys(get(client, path), set())
        assert found <= allowed, f"{path} leaked {sorted(found - allowed)}"


def test_no_response_mentions_a_moderation_decision_or_a_credential(client):
    """A substring sweep beside the key allowlist, because a leak can be a
    *value* — a follow record URI inside an event detail, say — rather than a
    key with a telling name."""
    for path in ("/api/v1/meta", "/api/v1/people", "/api/v1/people/alice.test",
                 "/api/v1/circles", "/api/v1/changes",
                 "/api/v1/resolve?handle=hidden.test"):
        blob = client.get(path, headers=H).text
        for forbidden in ("ignored_at", "ignored_reason", "ignore_locked",
                          "moderation", "follow_uri", "unservable",
                          "app.bsky.graph.follow", "password", "smtp",
                          TOKEN):
            assert forbidden not in blob, f"{path} leaked {forbidden!r}"


# ------------------------------------------------------------ who is listed

def test_hidden_departed_and_the_operator_are_all_absent(client):
    listed = dids(get(client, "/api/v1/people?limit=200"))
    assert listed == {"did:plc:alice", "did:plc:bob", "did:plc:carol"}


def test_a_hidden_person_is_a_404_not_a_redaction(client):
    """Omitting someone from a list while leaving their record fetchable is not
    a redaction, it is an unindexed leak."""
    assert client.get("/api/v1/people/hidden.test", headers=H).status_code == 404
    assert client.get("/api/v1/people/did:plc:hidden",
                      headers=H).status_code == 404


def test_a_hidden_person_resolves_as_unknown(client):
    """Indistinguishable from someone sonde has never seen — deliberately. The
    alternative reports that sonde hid them, and why."""
    (row,) = get(client, "/api/v1/resolve?handle=hidden.test")["data"]
    assert row["status"] == "unknown"
    assert row["did"] is None


def test_a_departed_follower_resolves_as_departed(client):
    (row,) = get(client, "/api/v1/resolve?handle=gone.test")["data"]
    assert row["status"] == "departed"
    assert row["did"] == "did:plc:gone"


def test_resolve_answers_every_identifier_in_the_order_asked(client):
    """One result per identifier *asked for*, present or not.

    Returning only the matches leaves the client index-zipping its request
    against a shorter response — the `getProfiles` hazard in CLAUDE.md,
    reproduced one layer up.
    """
    payload = get(client, "/api/v1/resolve"
                          "?handle=nobody.test&did=did:plc:alice&handle=bob.test")
    assert [r["query"] for r in payload["data"]] == [
        "nobody.test", "did:plc:alice", "bob.test"]
    assert [r["status"] for r in payload["data"]] == [
        "unknown", "follower", "follower"]


def test_handles_resolve_whatever_case_a_human_typed(client):
    (row,) = get(client, "/api/v1/resolve?handle=Alice.Test")["data"]
    assert row["did"] == "did:plc:alice"


def test_a_private_follower_is_listed_and_flagged(client):
    """The operator sees them; the flag is how a client knows not to
    republish them."""
    (carol,) = [p for p in get(client, "/api/v1/people")["data"]
                if p["did"] == "did:plc:carol"]
    assert carol["is_private"] is True


def test_a_circle_listing_hides_the_same_people_the_roster_does(client):
    """`store.group_members` would have been the obvious way to serve this and
    applies none of the roster's three filters, so a hidden member would have
    been published by the circle route alone."""
    listed = dids(get(client, "/api/v1/people?circle=journalists"))
    assert listed == {"did:plc:alice"}


def test_an_archived_circle_is_not_offered(client):
    slugs = {c["slug"] for c in get(client, "/api/v1/circles")["data"]}
    assert slugs == {"journalists"}


def test_an_empty_circle_reports_nobody_awaiting_review(client):
    """The LEFT JOIN emits an all-NULL row for a circle with no members, and
    NULL was being counted as an unreviewed membership — so every empty circle
    reported one person waiting to be reviewed, and no such person existed."""
    client.post("/api/v1/circles", json={"name": "Empty"}, headers=H)
    (circle,) = [c for c in get(client, "/api/v1/circles")["data"]
                 if c["slug"] == "empty"]
    assert circle["members"] == 0
    assert circle["unreviewed"] == 0


def test_a_circles_member_count_matches_its_member_list(client):
    """`group_summary` counts memberships, which includes the hidden and the
    departed; `/people?circle=` lists neither. A count that disagrees with the
    list under it reads as a sync fault.

    This is also the anti-drift device for `circle_member_counts`, which
    restates `ranked_followers`' filters in a second query: change one and this
    fails.
    """
    for circle in get(client, "/api/v1/circles")["data"]:
        listed = get(client, f"/api/v1/people?circle={circle['slug']}&limit=200")
        assert circle["members"] == len(listed["data"]), circle["slug"]


# -------------------------------------------------------------- pagination

def test_paging_by_did_sees_every_person_exactly_once(client):
    """The property a full sync depends on. Page size 1 so the walk is all
    boundary."""
    seen, cursor = [], None
    for _ in range(10):
        url = "/api/v1/people?limit=1" + (f"&cursor={cursor}" if cursor else "")
        payload = get(client, url)
        seen += [p["did"] for p in payload["data"]]
        cursor = payload["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(seen), "the default order is not stable"
    assert len(seen) == len(set(seen)) == 3


def test_a_cursor_from_a_different_query_is_refused(client):
    """Rather than answering rows from one query numbered by another, with no
    error anywhere."""
    cursor = get(client, "/api/v1/people?limit=1")["next_cursor"]
    r = client.get(f"/api/v1/people?limit=1&circle=journalists&cursor={cursor}",
                   headers=H)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "cursor_mismatch"


def test_a_cursor_sonde_did_not_issue_is_refused(client):
    r = client.get("/api/v1/people?cursor=not-a-cursor", headers=H)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_cursor"


def test_the_page_size_is_capped(client):
    payload = get(client, "/api/v1/meta")
    assert payload["data"]["limits"]["max_limit"] == 200
    assert get(client, "/api/v1/people?limit=99999")["data"] is not None


def test_an_unknown_order_is_refused_rather_than_silently_ignored(client):
    """The store falls back to `influence` for an unknown key, which is right
    for a page with a sort header and wrong for a client that believes it
    asked for something else."""
    r = client.get("/api/v1/people?order=whatever", headers=H)
    assert r.status_code == 400
    assert "order must be one of" in r.json()["error"]["message"]


def test_a_ranked_order_still_pages(client):
    payload = get(client, "/api/v1/people?order=followers&limit=2")
    assert len(payload["data"]) == 2
    assert payload["next_cursor"] is not None


# ----------------------------------------------------------------- changes

def test_the_change_feed_is_oldest_first_and_resumes_exactly_once(client):
    everything = [e["event"] for e in get(client, "/api/v1/changes")["data"]]
    assert everything == ["departed", "followed", "handle_changed"]

    walked, cursor = [], None
    for _ in range(6):
        url = "/api/v1/changes?limit=1" + (f"&cursor={cursor}" if cursor else "")
        page = get(client, url)
        walked += [e["event"] for e in page["data"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert walked == everything


def test_the_change_feed_carries_a_rename_but_no_follow_uri(client):
    """`handle_changed` detail is about the follower and a CRM keyed on handles
    cannot survive a rename without it. `followed_back` detail is the follow
    record's URI, which is why that event is not published at all."""
    events = get(client, "/api/v1/changes")["data"]
    kinds = {e["event"] for e in events}
    assert "handle_changed" in kinds
    assert "followed_back" not in kinds
    rename = next(e for e in events if e["event"] == "handle_changed")
    assert rename["detail"] == "old.test → bob.test"
    assert all(e["detail"] is None for e in events if e["event"] != "handle_changed")


def test_the_change_feed_takes_a_starting_timestamp(client):
    assert get(client, "/api/v1/changes?since=2099-01-01T00:00:00Z")["data"] == []


# ------------------------------------------------------------------ writes

def test_tagging_is_idempotent_in_both_directions(client):
    first = client.put("/api/v1/people/bob.test/circles/journalists", headers=H)
    again = client.put("/api/v1/people/bob.test/circles/journalists", headers=H)
    assert first.status_code == again.status_code == 200
    assert again.json()["data"]["circles"] == ["journalists"]

    removed = client.delete("/api/v1/people/bob.test/circles/journalists",
                            headers=H)
    once_more = client.delete("/api/v1/people/bob.test/circles/journalists",
                              headers=H)
    assert removed.json()["data"]["changed"] is True
    assert once_more.status_code == 200
    assert once_more.json()["data"]["changed"] is False


def test_an_archived_circle_refuses_rather_than_doing_nothing(client):
    """BUG-01 is exactly this silence on a profile page. A program cannot
    notice silence at all."""
    r = client.put("/api/v1/people/bob.test/circles/retired", headers=H)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "circle_archived"


def test_an_unknown_circle_is_a_404_not_an_archived_one(client):
    """`tag_actor` returns False for both. One is a typo, the other is a
    decision the client is about to undo."""
    r = client.put("/api/v1/people/bob.test/circles/nope", headers=H)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "no_such_circle"


def test_tagging_will_not_create_a_circle(client):
    """Type-to-create is right on a page a human is looking at and wrong for a
    program: a typo becomes a near-duplicate category nobody notices until a
    report is wrong."""
    client.put("/api/v1/people/bob.test/circles/typoo", headers=H)
    assert {c["slug"] for c in get(client, "/api/v1/circles")["data"]} == \
        {"journalists"}


def test_a_hidden_person_cannot_be_tagged(client):
    assert client.put("/api/v1/people/hidden.test/circles/journalists",
                      headers=H).status_code == 404


def test_creating_a_circle_says_whether_it_created_one(client):
    made = client.post("/api/v1/circles", json={"name": "VC friends"}, headers=H)
    assert made.status_code == 201
    assert made.json()["data"] == {"slug": "vc-friends", "name": "VC friends",
                                   "created": True}
    again = client.post("/api/v1/circles", json={"name": "VC friends"},
                        headers=H)
    assert again.status_code == 200
    assert again.json()["data"]["created"] is False


def test_creating_an_archived_circle_is_a_conflict(client):
    r = client.post("/api/v1/circles", json={"name": "Retired"}, headers=H)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "circle_archived"


def test_a_nameless_circle_is_refused(client):
    for body in ({"name": "   "}, {"name": "!!!"}, {}):
        r = client.post("/api/v1/circles", json=body, headers=H)
        assert r.status_code == 400, body


def test_a_body_that_is_not_a_json_object_is_refused(client):
    r = client.post("/api/v1/circles", content=b"not json", headers=H)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_body"


# ------------------------------------------------------------- the detail

def test_the_detail_record_answers_a_crm_in_one_call(client):
    """The fields a contact record is actually built from — who they are, what
    you have called them, when you last spoke, and where to go next."""
    person = get(client, "/api/v1/people/alice.test")["data"]
    assert person["circles"][0]["slug"] == "journalists"
    assert person["last_interaction_at"] == "2026-08-01T00:00:00+00:00"
    assert person["interactions"]["totals"]["reply"]["inbound"] == 1
    assert person["is_mutual"] is True
    assert person["links"][0]["host"] == "ft.com"
    assert person["bsky_url"] == "https://bsky.app/profile/alice.test"
    assert person["sonde_url"].endswith("/followers/did:plc:alice")
    assert [e["event"] for e in person["events"]] == ["followed"]


def test_the_list_carries_last_contact_without_a_query_per_row(client):
    (alice,) = [p for p in get(client, "/api/v1/people")["data"]
                if p["did"] == "did:plc:alice"]
    assert alice["last_interaction_at"] == "2026-08-01T00:00:00+00:00"
    assert alice["circles"] == ["journalists"]


def test_a_person_is_reachable_by_did_or_by_handle(client):
    by_handle = get(client, "/api/v1/people/alice.test")["data"]
    by_did = get(client, "/api/v1/people/did:plc:alice")["data"]
    assert by_handle == by_did
