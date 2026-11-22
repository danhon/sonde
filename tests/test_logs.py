"""The fleet log format (reverse-proxy LOG-1): text on stdout, JSON lines in the file."""

import json
import logging

import pytest

from sonde import logs


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    yield root
    for h in root.handlers:
        if h not in saved[0]:
            h.close()
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


def test_setup_writes_both_formats(tmp_path, capsys, root_logger):
    logs.setup("abc1234", str(tmp_path / "app.log"))
    logging.getLogger("sonde.test").info("hello %s", "there", extra={"status": 200})
    for h in root_logger.handlers:
        h.flush()
    line = capsys.readouterr().out.strip()
    stamp, rest = line.split(" ", 1)
    assert rest == "abc1234 INFO sonde.test: hello there"
    assert stamp[10] == "T" and stamp[19] == "." and stamp[23] in "+-"   # ms and an offset
    d = json.loads((tmp_path / "app.log").read_text().splitlines()[-1])
    assert {k: d[k] for k in ("ts", "level", "logger", "build", "msg", "status")} == {
        "ts": stamp, "level": "INFO", "logger": "sonde.test", "build": "abc1234",
        "msg": "hello there", "status": 200}


def test_setup_without_a_file_writes_only_stdout(root_logger):
    logs.setup("dev")
    assert len(root_logger.handlers) == 1


def test_uvicorns_colour_copy_stays_out_of_the_file(tmp_path, root_logger):
    logs.setup("abc1234", str(tmp_path / "app.log"))
    logging.getLogger("uvicorn.error").info("Started", extra={"color_message": "\x1b[36mStarted\x1b[0m"})
    for h in root_logger.handlers:
        h.flush()
    assert "color_message" not in json.loads((tmp_path / "app.log").read_text().splitlines()[-1])
