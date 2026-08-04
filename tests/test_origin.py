"""Writes must come from sonde's own pages.

The Authelia cookie is scoped to `domain: sgc.rayandhon.com` with the default
`same_site: lax`, and Lax stops cross-*site* requests only. Subdomains of one
registrable domain are same-site, so every other service on the fleet could
POST here with the operator's session attached. These tests are mostly about
one value: `Sec-Fetch-Site: same-site` must be refused, which is the opposite
of what most CSRF advice says to do with it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sonde.db import store
from sonde.web import origin
from sonde.web.app import create_app


@pytest.fixture
def client(tmp_path):
    store.set_db_path(str(tmp_path / "origin.db"))
    with TestClient(create_app(), base_url="http://sonde.test") as c:
        yield c
    store.set_db_path(None)


# ------------------------------------------------- the rule on its own

def test_reads_are_never_refused():
    """Only writes are guarded — a cross-site GET is just a link."""
    for method in ("GET", "HEAD", "OPTIONS"):
        assert origin.refusal(method, {"sec-fetch-site": "cross-site"}) is None


def test_a_sibling_subdomain_is_refused():
    """The whole point. calibre-web and sonde are same-site, not same-origin."""
    assert origin.refusal("POST", {"sec-fetch-site": "same-site"}) is not None


def test_a_hostile_origin_is_refused():
    assert origin.refusal("POST", {"sec-fetch-site": "cross-site"}) is not None


def test_sondes_own_pages_are_allowed():
    assert origin.refusal("POST", {"sec-fetch-site": "same-origin"}) is None
    assert origin.refusal("POST", {"sec-fetch-site": "none"}) is None


def test_the_origin_header_is_the_fallback():
    """For browsers too old to send Sec-Fetch-Site."""
    ours = {"origin": "https://sonde.sgc.rayandhon.com",
            "host": "sonde.sgc.rayandhon.com"}
    assert origin.refusal("POST", ours) is None

    sibling = {"origin": "https://calibre.sgc.rayandhon.com",
               "host": "sonde.sgc.rayandhon.com"}
    assert origin.refusal("POST", sibling) is not None


def test_the_port_is_part_of_the_comparison():
    """Local dev is localhost:8090 on both sides."""
    assert origin.refusal(
        "POST", {"origin": "http://localhost:8090", "host": "localhost:8090"}
    ) is None
    assert origin.refusal(
        "POST", {"origin": "http://localhost:9999", "host": "localhost:8090"}
    ) is not None


def test_sec_fetch_site_wins_over_a_matching_origin():
    """A forged Origin cannot buy anything the browser has already contradicted."""
    assert origin.refusal("POST", {
        "sec-fetch-site": "same-site",
        "origin": "https://sonde.test",
        "host": "sonde.test",
    }) is not None


def test_a_client_sending_neither_header_is_allowed():
    """Documented fail-open: curl, the test suite, a script on the box.

    A hostile page cannot strip these headers, so refusing here would block the
    operator's own tooling and nothing else. If this ever changes, the whole
    suite fails, which is the intended alarm.
    """
    assert origin.refusal("POST", {}) is None


# ------------------------------------------------- wired to every write

def _writes(app):
    """Every state-changing route the app actually exposes."""
    out = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        unsafe = methods - origin.SAFE_METHODS
        if unsafe:
            out.append((sorted(unsafe)[0], route.path))
    return out


def test_the_app_has_writes_to_guard():
    """Guards the guard: an empty list would make the sweep below vacuous."""
    assert len(_writes(create_app())) >= 15


def test_every_write_route_refuses_a_sibling_subdomain(client):
    """A middleware, so a route added later is covered without being listed.

    This proves the middleware is genuinely in front of the router rather than
    trusting that it is.
    """
    for method, path in _writes(client.app):
        url = path.replace("{did:path}", "did:plc:x")
        while "{" in url:
            start = url.index("{")
            end = url.index("}", start)
            url = url[:start] + "1" + url[end + 1:]

        response = client.request(
            method, url, headers={"sec-fetch-site": "same-site"},
            follow_redirects=False)
        assert response.status_code == 403, f"{method} {url} -> {response.status_code}"


def test_the_same_writes_are_not_refused_from_sonde(client):
    """The guard must discriminate, not blanket-refuse.

    Without this, a middleware that returned 403 unconditionally would pass the
    test above and take the whole application down.
    """
    for method, path in _writes(client.app):
        url = path.replace("{did:path}", "did:plc:x")
        while "{" in url:
            start = url.index("{")
            end = url.index("}", start)
            url = url[:start] + "1" + url[end + 1:]

        response = client.request(
            method, url, headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False)
        assert response.status_code != 403, f"{method} {url} was refused"


def test_the_follow_button_is_covered(client):
    """Named explicitly because it is the only route that writes to Bluesky."""
    response = client.post(
        "/followers/did:plc:x/follow",
        headers={"sec-fetch-site": "same-site"}, follow_redirects=False)
    assert response.status_code == 403


def test_a_cross_site_read_still_works(client):
    """Reads are not guarded, and the public view in ACCESS.md depends on it."""
    response = client.get("/healthz", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 200
