"""The Docker healthcheck probe (sonde/healthcheck.py).

Traefik routes only to containers this calls healthy, so a probe that wrongly
fails takes the site down, and one that wrongly passes breaks deploy drains.
Each test runs the script the way compose.yml does, `python3 -I -S <path>`.
"""
from __future__ import annotations

import http.server
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent  / "sonde" / "healthcheck.py"


def _probe(port: int, drain_file: Path) -> int:
    return subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), str(port), str(drain_file)],
        timeout=10,
    ).returncode


@pytest.fixture
def server():
    """A local HTTP server whose /healthz answers with `server.status`."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(srv.status if self.path == "/healthz" else 404)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.status = 200
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_healthy_when_health_route_answers_200(server, tmp_path):
    assert _probe(server.server_port, tmp_path / "draining") == 0


@pytest.mark.parametrize("status", [500, 503, 404])
def test_unhealthy_when_health_route_is_not_200(server, tmp_path, status):
    server.status = status
    assert _probe(server.server_port, tmp_path / "draining") == 1


def test_unhealthy_while_draining_even_if_app_is_up(server, tmp_path):
    # deploy.sh touches this to take the old container out of Traefik.
    drain = tmp_path / "draining"
    drain.touch()
    assert _probe(server.server_port, drain) == 1


def test_unhealthy_when_nothing_listens(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _probe(port, tmp_path / "draining") == 1


def test_unhealthy_when_app_accepts_but_never_answers(tmp_path):
    # Must fail inside compose's 3 s timeout rather than hang.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        assert _probe(s.getsockname()[1], tmp_path / "draining") == 1
    finally:
        s.close()


def test_imports_stay_cheap():
    # The point of the script: urllib.request's imports were the CPU cost.
    out = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         "import runpy, sys; runpy.run_path(sys.argv[1]);"
         "print(' '.join(m for m in sys.modules"
         " if m.split('.')[0] in ('urllib', 'http', 'email', 'ssl', 'sonde')))",
         str(SCRIPT)],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout.strip()
    assert out == ""
