"""Is what's deployed still what's on the branch?

The repository is private, so this needs a token and has to degrade honestly
without one — saying "cannot tell" rather than "up to date", which would be the
dangerous failure.
"""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from sonde.config import settings
from sonde.external import version
from sonde.web.app import create_app


@pytest.fixture(autouse=True)
def _no_cache():
    version._cache.clear()
    yield
    version._cache.clear()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(version, "settings", dataclasses.replace(
        settings, build_sha="aaaaaaa", github_token="t",
        github_repo="danhon/sonde", github_branch="main"))


def fake_github(monkeypatch, *, status=200, payload=None):
    class Response:
        status_code = status
        def json(self):
            return payload or {}

    class Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None):
            Client.url = url
            return Response()

    monkeypatch.setattr(version.httpx, "AsyncClient", Client)
    return Client


async def test_without_a_token_it_says_it_cannot_tell(monkeypatch):
    """The dangerous failure would be reporting "up to date" when it simply has
    no way to know."""
    monkeypatch.setattr(version, "settings",
                        dataclasses.replace(settings, build_sha="aaaaaaa",
                                            github_token=""))

    result = await version.build_status()

    assert result["status"] == "unknown"
    assert "GITHUB_TOKEN" in result["detail"]


async def test_an_unstamped_build_says_so(monkeypatch):
    """`dev` means it was not built by make deploy, so there is nothing to
    compare and the reason is worth saying."""
    monkeypatch.setattr(version, "settings",
                        dataclasses.replace(settings, build_sha="dev",
                                            github_token="t"))

    result = await version.build_status()

    assert result["status"] == "unknown"
    assert "make deploy" in result["detail"]


async def test_a_deployed_head_is_current(monkeypatch, configured):
    fake_github(monkeypatch, payload={"ahead_by": 0, "behind_by": 0, "commits": []})

    assert (await version.build_status())["status"] == "current"


async def test_commits_on_the_branch_mean_prod_is_behind(monkeypatch, configured):
    """compare/{deployed}...{branch} reports ahead_by as the commits the BRANCH
    has that we do not — which is how far behind prod is. Getting that mapping
    backwards would report the reassuring answer to the worrying case."""
    fake_github(monkeypatch, payload={
        "ahead_by": 3, "behind_by": 0, "html_url": "https://github.test/compare",
        "commits": [
            {"sha": "1111111aaa", "commit": {"message": "one\n\nbody",
                                             "author": {"date": "2026-07-29T10:00:00Z"}}},
            {"sha": "2222222bbb", "commit": {"message": "two",
                                             "author": {"date": "2026-07-29T11:00:00Z"}}},
        ]})

    result = await version.build_status()

    assert result["status"] == "behind"
    assert result["behind"] == 3
    assert result["branch_sha"] == "2222222"
    # Newest first, and only the subject line.
    assert [c["message"] for c in result["commits"]] == ["two", "one"]


async def test_deploying_something_unpushed_reads_as_ahead(monkeypatch, configured):
    """make deploy builds from whatever is checked out on the host, so this is a
    real state: the running build contains commits the branch has never seen."""
    fake_github(monkeypatch, payload={"ahead_by": 0, "behind_by": 2, "commits": []})

    result = await version.build_status()

    assert result["status"] == "ahead"
    assert result["ahead"] == 2


async def test_both_at_once_is_diverged(monkeypatch, configured):
    fake_github(monkeypatch, payload={"ahead_by": 1, "behind_by": 1, "commits": []})

    assert (await version.build_status())["status"] == "diverged"


async def test_a_404_does_not_claim_to_know(monkeypatch, configured):
    fake_github(monkeypatch, status=404)

    result = await version.build_status()

    assert result["status"] == "unknown"
    assert "does not recognise" in result["detail"]


async def test_a_network_failure_does_not_claim_to_know(monkeypatch, configured):
    import httpx

    class Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            raise httpx.ConnectError("no route")

    monkeypatch.setattr(version.httpx, "AsyncClient", Client)

    result = await version.build_status()

    assert result["status"] == "unknown"
    assert "Could not reach GitHub" in result["detail"]


async def test_the_answer_is_cached_so_a_second_look_is_free(monkeypatch, configured):
    calls = []

    class Response:
        status_code = 200
        def json(self):
            calls.append(1)
            return {"ahead_by": 0, "behind_by": 0, "commits": []}

    class Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return Response()

    monkeypatch.setattr(version.httpx, "AsyncClient", Client)

    await version.build_status()
    await version.build_status()

    assert len(calls) == 1


async def test_the_build_stamp_opens_a_panel(db):
    # The `db` fixture, not because this touches data, but because creating the
    # app opens a database and conftest fails any test that reaches the real one.
    with TestClient(create_app()) as client:
        page = client.get("/circles")

    assert 'id="build"' in page.text
    assert "data-build-body" in page.text


async def test_the_endpoint_answers_json(db, monkeypatch):
    monkeypatch.setattr(version, "settings",
                        dataclasses.replace(settings, github_token=""))
    with TestClient(create_app()) as client:
        response = client.get("/build")

    assert response.status_code == 200
    assert response.json()["status"] == "unknown"
