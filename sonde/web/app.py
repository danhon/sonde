"""FastAPI application."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from sonde.config import settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STARTED_AT = time.time()


def create_app() -> FastAPI:
    app = FastAPI(title="sonde", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Unauthenticated liveness probe.

        This is the one surface Authelia does not guard, so it exposes only
        liveness and staleness — never follower data. It needs its own Traefik
        router (see compose.yml); Authelia attaches to the router, not the path.
        """
        from sonde.db import store

        payload = {
            "status": "ok",
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
            "build": settings.build_sha,
        }
        try:
            payload["last_sync"] = await store.last_sync_summary()
        except Exception:
            # A missing or unopened DB must not make the container look dead.
            payload["last_sync"] = None
        return JSONResponse(payload)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        from sonde.db import store

        stats = await store.dashboard_stats()
        return TEMPLATES.TemplateResponse(
            request=request, name="index.html", context={"stats": stats, "settings": settings}
        )

    return app


app = create_app()
