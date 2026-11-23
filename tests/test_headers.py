"""The CSP, and the templates it depends on (sonde#6, BUG-20, BUG-21).

A policy of `script-src 'self'` is only true while every script is a file sonde
serves. These tests hold both halves: the header on every kind of response, and
templates with nothing in them the policy would refuse. The browser half (a
rendered page with no violations and no unstyled classes) is in
`evals/mobile_check.py`, which needs a populated database.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sonde.db import store
from sonde.web import headers
from sonde.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted((ROOT / "sonde/web/templates").glob("*.html"))


@pytest.fixture
def client(tmp_path):
    store.set_db_path(str(tmp_path / "headers.db"))
    with TestClient(create_app(), base_url="http://sonde.test") as c:
        yield c
    store.set_db_path(None)


def directives() -> dict[str, list[str]]:
    out = {}
    for part in headers.CONTENT_SECURITY_POLICY.split(";"):
        name, *values = part.split()
        out[name] = values
    return out


# ----------------------------------------------------------------- the policy

def test_scripts_come_only_from_sonde():
    """The line the whole change exists for."""
    assert directives()["script-src"] == ["'self'"]


def test_nothing_in_the_policy_reopens_what_the_play_cdn_needed():
    policy = headers.CONTENT_SECURITY_POLICY
    assert "'unsafe-eval'" not in policy
    assert "cdn.tailwindcss.com" not in policy
    # Inline style *attributes* only: the bar widths. Never <style> or script.
    assert "'unsafe-inline'" not in " ".join(
        v for k, vs in directives().items() if k != "style-src-attr" for v in vs)


def test_framing_matches_traefiks_x_frame_options():
    """NET-7: Traefik sends SAMEORIGIN; the app sends frame-ancestors."""
    assert directives()["frame-ancestors"] == ["'self'"]


# ------------------------------------------------------ on every response kind

@pytest.mark.parametrize("method,path,request_headers", [
    ("GET", "/", {}),                                   # a page
    ("GET", "/healthz", {}),                            # JSON
    ("GET", "/static/app.css", {}),                     # a static file
    ("GET", "/static/no-such-file.css", {}),            # a 404
    ("GET", "/api/v1/followers", {}),                   # the API guard's refusal
    ("POST", "/settings/sync/head",                     # the origin guard's 403
     {"sec-fetch-site": "same-site"}),
])
def test_every_response_carries_the_headers(client, method, path, request_headers):
    response = client.request(method, path, headers=request_headers,
                              follow_redirects=False)
    assert response.headers["content-security-policy"] == headers.CONTENT_SECURITY_POLICY
    assert response.headers["referrer-policy"] == "same-origin"


def test_the_app_leaves_traefiks_headers_to_traefik(client):
    """NET-7: Traefik overwrites these two, so setting them here misleads."""
    response = client.get("/")
    assert "x-frame-options" not in response.headers
    assert "x-content-type-options" not in response.headers


# ---------------------------------------------------------- the static files

def test_pages_load_the_compiled_stylesheet_and_script(client):
    page = client.get("/").text
    assert headers.static_url("app.css") in page
    assert headers.static_url("sonde.js") in page
    for name, kind in [("app.css", "text/css"), ("sonde.js", "text/javascript")]:
        response = client.get(headers.static_url(name))
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(kind)


def test_static_urls_change_when_the_file_does():
    url = headers.static_url("app.css")
    assert re.fullmatch(r"/static/app\.css\?v=[0-9a-f]{12}", url)


def test_the_wheel_carries_the_static_files():
    """The image installs sonde as a wheel built from an explicit include list.

    Templates were on it and static/ was not, so the first build of this
    change served pages whose stylesheet 404'd.
    """
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["include"]
    assert "sonde/web/static/*" in include


# ------------------------------------------------- templates the policy allows

@pytest.mark.parametrize("page", TEMPLATES, ids=lambda p: p.name)
def test_no_inline_script(page):
    for tag in re.findall(r"<script\b[^>]*>", page.read_text()):
        assert "src=" in tag, f"inline <script> in {page.name}: CSP refuses it"


@pytest.mark.parametrize("page", TEMPLATES, ids=lambda p: p.name)
def test_no_inline_event_handlers(page):
    found = re.findall(r"<[^>]*\son[a-z]+\s*=", page.read_text())
    assert not found, f"{page.name}: on…= handlers are inline script; use sonde.js"


@pytest.mark.parametrize("page", TEMPLATES, ids=lambda p: p.name)
def test_no_style_blocks_or_third_party_assets(page):
    text = page.read_text()
    assert "<style" not in text, f"{page.name}: put it in sonde/web/tailwind.css"
    assert not re.search(r"<(script|link)\b[^>]*(src|href)=\"https?:", text), (
        f"{page.name}: a third-party script or stylesheet")


@pytest.mark.parametrize("page", TEMPLATES, ids=lambda p: p.name)
def test_no_class_name_assembled_from_parts(page):
    """Tailwind finds classes by reading source, not rendered pages.

    `text-{{ align }}` never appears whole in any file, so the compiled CSS
    holds `text-right` only if something else happens to spell it out. The Play
    CDN read the live DOM and never noticed. Spell the class in full instead:
    `{{ 'text-right' if align == 'right' else 'text-left' }}`.
    """
    found = re.findall(r"[a-z0-9]-\{\{", page.read_text())
    assert not found, f"{page.name}: class name built from a fragment"
