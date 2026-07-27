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

    @app.get("/changes", response_class=HTMLResponse)
    async def changes(request: Request, event: str | None = None) -> HTMLResponse:
        from sonde.db import store

        return TEMPLATES.TemplateResponse(
            request=request,
            name="changes.html",
            context={
                "events": await store.recent_changes(200, event=event),
                "totals": await store.change_totals(),
                "growth": await store.growth_series(90),
                "event": event,
                "settings": settings,
            },
        )

    @app.get("/influential", response_class=HTMLResponse)
    async def influential(request: Request, page: int = 1, order: str = "influence") -> HTMLResponse:
        from sonde.db import store
        from sonde.scoring import WEIGHTS

        per_page = 50
        rows = await store.ranked_followers(
            limit=per_page, offset=(max(page, 1) - 1) * per_page, order=order
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="influential.html",
            context={
                "rows": rows, "page": max(page, 1), "order": order,
                "weights": WEIGHTS, "progress": await store.hydration_progress(),
                "settings": settings,
            },
        )

    @app.get("/followers", response_class=HTMLResponse)
    async def followers(
        request: Request, page: int = 1, order: str = "influence",
        q: str | None = None, verified: bool = False, min_followers: int | None = None,
    ) -> HTMLResponse:
        from sonde.db import store

        per_page = 100
        rows = await store.ranked_followers(
            limit=per_page, offset=(max(page, 1) - 1) * per_page, order=order,
            verified_only=verified, min_followers=min_followers, query=q,
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="followers.html",
            context={
                "rows": rows, "page": max(page, 1), "order": order, "q": q or "",
                "verified": verified, "min_followers": min_followers,
                "counts": await store.counts(), "settings": settings,
            },
        )

    @app.get("/verified", response_class=HTMLResponse)
    async def verified(request: Request) -> HTMLResponse:
        from sonde.db import store

        summary = await store.verified_summary()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="verified.html",
            context={"v": summary, "settings": settings},
        )

    return app


app = create_app()
