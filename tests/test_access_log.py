"""The access line: the path only, never the query string or a token (LOG-1, LOG-2)."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from sonde.web.app import create_app


def _lines(caplog):
    return [r for r in caplog.records if r.name == "sonde.access"]


async def test_the_path_but_never_the_query_or_the_token(db, caplog):
    with caplog.at_level(logging.INFO, logger="sonde.access"):
        TestClient(create_app()).get("/api/v1/nope?secret=hunter2",
                                     headers={"Authorization": "Bearer " + "hunter3"})
    lines = _lines(caplog)
    assert len(lines) == 1
    assert lines[0].path == "/api/v1/nope"
    assert lines[0].getMessage().split()[:2] == ["GET", "/api/v1/nope"]
    assert "hunter2" not in caplog.text and "hunter3" not in caplog.text


async def test_the_probes_are_quiet_when_they_pass(db, caplog):
    with caplog.at_level(logging.INFO, logger="sonde.access"):
        r = TestClient(create_app()).get("/healthz")
    assert r.status_code == 200
    assert _lines(caplog) == []
