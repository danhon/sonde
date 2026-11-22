"""The fleet's log format (reverse-proxy LOG-1, decisions/019; python-apps.md, "Logging").

One text line on stdout for people (`docker logs`, `make logs`), and JSON lines
in /logs/app.log, bind-mounted from ~/sonde-logs, for machines (VictoriaLogs,
`jq`). Same records, two formatters.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime

# color_message: uvicorn's copy of a message with terminal colour codes.
_STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "build", "color_message"}


def _ts(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = f"{_ts(record)} {record.build} {record.levelname} {record.name}: {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": _ts(record), "level": record.levelname, "logger": record.name,
             "build": record.build, "msg": record.getMessage()}
        d.update({k: v for k, v in vars(record).items() if k not in _STD and not k.startswith("_")})
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


def setup(build: str, log_file: str | None = None, level: str = "INFO") -> None:
    """Route every logger, uvicorn's included, through the two formats."""
    def add_build(record: logging.LogRecord) -> bool:
        record.build = build
        return True

    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(TextFormatter())
    handlers: list[logging.Handler] = [out]
    if log_file:
        fh = logging.handlers.TimedRotatingFileHandler(log_file, when="midnight", backupCount=30, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        handlers.append(fh)
    for h in handlers:
        h.addFilter(add_build)  # on the handlers, so library loggers get a build too
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
    # At INFO these log every request's full URL (httpx, httpcore: a query
    # string or a token in it would reach the file, LOG-2) and every
    # scheduled job's start and end (apscheduler). Their warnings still show.
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)


QUIET = ("httpx", "httpcore", "apscheduler")
