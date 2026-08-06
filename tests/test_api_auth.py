"""Who gets into `/api/v1`, and what they can do once inside.

The API's Traefik router carries no Authelia — a program cannot do an
interactive 2FA login — so this is the only thing between the token check and
the open internet. Every test here is about a way in that must not work.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sonde.db import store
from sonde.web import apikey
from sonde.web.app import create_app

READ_TOKEN = "r" * 40
WRITE_TOKEN = "w" * 40
TOKENS = f"reader:{READ_TOKEN},crm:{WRITE_TOKEN}:write"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDE_API_TOKENS", TOKENS)
    store.set_db_path(str(tmp_path / "api.db"))
    with TestClient(create_app(), base_url="http://sonde.test") as c:
        yield c
    store.set_db_path(None)


def read(token: str = WRITE_TOKEN) -> dict:
    return {"authorization": f"Bearer {token}"}


# ------------------------------------------------- parsing the environment

def test_a_token_is_read_only_unless_it_says_write(monkeypatch):
    """Write is granted, never assumed. A leaked read token must not tag."""
    monkeypatch.setenv("SONDE_API_TOKENS", TOKENS)
    by_name = {c.name: c for _, c in apikey.clients()}
    assert by_name["reader"].scopes == frozenset({"read"})
    assert by_name["crm"].scopes == frozenset({"read", "write"})
    assert not by_name["reader"].may("write")


def test_a_short_secret_is_dropped_rather_than_honoured(monkeypatch):
    """A guessable token is worse than no API: it advertises the door."""
    monkeypatch.setenv("SONDE_API_TOKENS", "weak:abc123:write")
    assert apikey.clients() == []
    assert not apikey.configured()


def test_one_bad_entry_does_not_take_the_others_down(monkeypatch):
    """A fat-fingered token in the compose env must not disable the API."""
    monkeypatch.setenv("SONDE_API_TOKENS", f"broken,crm:{WRITE_TOKEN}:write")
    assert [c.name for _, c in apikey.clients()] == ["crm"]


def test_an_unknown_scope_is_ignored_not_granted(monkeypatch):
    monkeypatch.setenv("SONDE_API_TOKENS", f"crm:{WRITE_TOKEN}:admin")
    (_, client), = apikey.clients()
    assert client.scopes == frozenset({"read"})


def test_no_tokens_means_the_api_is_off(monkeypatch):
    monkeypatch.delenv("SONDE_API_TOKENS", raising=False)
    assert not apikey.configured()


def test_the_secret_never_reaches_the_client_object(monkeypatch):
    """`Client` is logged and attached to the request, so it must stay safe to
    print in full."""
    monkeypatch.setenv("SONDE_API_TOKENS", TOKENS)
    for _, client in apikey.clients():
        assert WRITE_TOKEN not in repr(client)
        assert READ_TOKEN not in repr(client)


# ------------------------------------------------------------ the prefix

def test_only_the_api_prefix_is_guarded():
    """The token grants nothing on the pages Authelia protects, and — the one
    that would matter — `/api/status` is not swept up by a prefix of `/api`."""
    assert apikey.guards("/api/v1")
    assert apikey.guards("/api/v1/people")
    assert not apikey.guards("/api/status")
    assert not apikey.guards("/settings")
    assert not apikey.guards("/")
    # Neither the prefix nor below it. Guarding it would be the safe direction
    # to be wrong in, which is exactly why it is asserted rather than assumed.
    assert not apikey.guards("/api/v1abc")


def test_writes_need_the_write_scope_and_reads_do_not():
    assert apikey.needed_scope("GET") == "read"
    assert apikey.needed_scope("HEAD") == "read"
    assert apikey.needed_scope("POST") == "write"
    assert apikey.needed_scope("PUT") == "write"
    assert apikey.needed_scope("DELETE") == "write"


def test_only_a_bearer_header_is_read():
    assert apikey.bearer({"authorization": "Bearer abc"}) == "abc"
    assert apikey.bearer({"authorization": "bearer abc"}) == "abc"
    assert apikey.bearer({"authorization": "Basic abc"}) is None
    assert apikey.bearer({"authorization": "abc"}) is None
    assert apikey.bearer({}) is None


def test_identify_matches_only_a_configured_secret(monkeypatch):
    monkeypatch.setenv("SONDE_API_TOKENS", TOKENS)
    assert apikey.identify({"authorization": f"Bearer {READ_TOKEN}"}).name == "reader"
    assert apikey.identify({"authorization": "Bearer nope"}) is None
    assert apikey.identify({}) is None
    # A prefix of a real token must not pass. compare_digest is doing the work;
    # this is here so that swapping it for `==` or `startswith` fails loudly.
    assert apikey.identify({"authorization": f"Bearer {READ_TOKEN[:20]}"}) is None


# --------------------------------------------------------- wired to routes

def test_every_api_path_refuses_an_unauthenticated_caller(client):
    for path in ("/api/v1/meta", "/api/v1/people", "/api/v1/circles",
                 "/api/v1/changes", "/api/v1/resolve",
                 "/api/v1/people/did:plc:x"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "unauthenticated"
        assert r.headers["www-authenticate"] == "Bearer"


def test_a_path_that_does_not_exist_refuses_identically(client):
    """Default deny in front of the router, so an anonymous caller cannot map
    the API by watching which paths 404 and which 401."""
    unknown = client.get("/api/v1/not-a-route")
    known = client.get("/api/v1/people")
    assert unknown.status_code == known.status_code == 401
    assert unknown.json() == known.json()


def test_a_wrong_token_is_refused(client):
    assert client.get("/api/v1/meta", headers=read("x" * 40)).status_code == 401


def test_a_valid_token_gets_in(client):
    r = client.get("/api/v1/meta", headers=read())
    assert r.status_code == 200
    assert r.json()["data"]["client"] == {
        "name": "crm", "scopes": ["read", "write"]}


def test_a_read_token_cannot_write(client):
    r = client.put("/api/v1/people/did:plc:x/circles/anything",
                   headers=read(READ_TOKEN))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_the_api_says_so_when_it_is_not_configured(tmp_path, monkeypatch):
    """Rather than 401ing a correct token forever, which reads as a broken
    deployment instead of an absent feature."""
    monkeypatch.delenv("SONDE_API_TOKENS", raising=False)
    store.set_db_path(str(tmp_path / "off.db"))
    with TestClient(create_app(), base_url="http://sonde.test") as c:
        r = c.get("/api/v1/meta", headers=read())
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "api_disabled"
    store.set_db_path(None)


def test_the_same_origin_guard_still_covers_api_writes(client):
    """Defence in depth, and the assertion that proves both middlewares run.

    A valid token is not a reason to skip the check that a write came from
    somewhere legitimate — and if the two guards were ever collapsed into one,
    this is the test that notices.
    """
    r = client.put("/api/v1/people/did:plc:x/circles/anything",
                   headers={**read(), "sec-fetch-site": "same-site"})
    assert r.status_code == 403
    # And in the API's error shape, because "errors are always this object" has
    # to hold for the refusals that come from outside the API's own code too.
    assert r.json()["error"]["code"] == "forbidden"


def test_api_responses_are_never_cached(client):
    """Personal data crossing a proxy, including the refusals — a 404 from
    `/people/{handle}` says whether sonde tracks that person."""
    for r in (client.get("/api/v1/meta", headers=read()),
              client.get("/api/v1/meta"),
              client.get("/api/v1/people/nobody.test", headers=read())):
        assert r.headers["cache-control"] == "private, no-store"


def test_the_api_errors_in_one_shape(client):
    """Including the errors it does not raise itself. Two shapes means two
    parsers in every client, and the second is found in production."""
    for r in (client.get("/api/v1/not-a-route", headers=read()),
              client.get("/api/v1/people?limit=nope", headers=read()),
              client.get("/api/v1/people/nobody.test", headers=read()),
              client.get("/api/v1/meta")):
        body = r.json()
        assert set(body) == {"error"}, body
        assert set(body["error"]) == {"code", "message"}


def test_the_admin_surface_is_untouched_by_a_token(client):
    """The token buys `/api/v1` and nothing else. Authelia guards the pages in
    production; what matters here is that presenting a token does not change
    how any of them behave."""
    with_token = client.get("/settings", headers=read())
    without = client.get("/settings")
    assert with_token.status_code == without.status_code == 200


def test_the_compose_api_router_is_scoped_and_unguarded_deliberately():
    """Cheap, and it catches the copy-paste that would matter most.

    The API router is the one router in this deployment with no Authelia on it.
    Its `PathPrefix` is therefore the containment: widened to `/api` it takes
    `/api/status` out from behind Authelia, and dropped entirely it takes the
    whole admin surface.
    """
    from pathlib import Path

    compose = Path(__file__).resolve().parents[1] / "compose.yml"
    rule = [line for line in compose.read_text().splitlines()
            if "-api.rule=" in line]
    assert len(rule) == 1, "exactly one API router"
    assert "PathPrefix(`/api/v1`)" in rule[0]
    assert "Host(`${SERVICE_HOST}`)" in rule[0]

    middlewares = [line for line in compose.read_text().splitlines()
                   if "-api.middlewares=" in line]
    assert middlewares and "authelia" not in middlewares[0], (
        "the API router is unguarded by design — if this ever gains authelia@file "
        "the CRM stops working, and if it gains it silently nobody will know why")


def test_the_openapi_map_is_gone():
    """It listed every write route. Harmless behind Authelia; a directory of
    the write surface on a router that has none."""
    with TestClient(create_app(), base_url="http://sonde.test") as c:
        assert c.get("/openapi.json").status_code == 404
