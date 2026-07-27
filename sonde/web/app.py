"""FastAPI application."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from sonde.config import settings
from sonde.jobs import registry

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
        # Job kinds and ages only — never follower data. This is the one
        # surface Authelia does not guard.
        payload["jobs_running"] = registry.running()
        payload["scheduler"] = bool(
            getattr(app.state, "scheduler", None)
            and app.state.scheduler.running
        )
        try:
            payload["last_sync"] = await store.last_sync_summary()
            payload["last_sync_age_seconds"] = await store.last_sync_age_seconds()
        except Exception:
            # A missing or unopened DB must not make the container look dead.
            payload["last_sync"] = None
            payload["last_sync_age_seconds"] = None
        return JSONResponse(payload)

    @app.get("/api/status")
    async def api_status(request: Request) -> JSONResponse:
        """Live job state for the nav strip. Polled; keep it cheap."""
        from sonde.db import store

        scheduler = getattr(request.app.state, "scheduler", None)
        upcoming = []
        if scheduler is not None:
            for job in scheduler.get_jobs():
                if job.next_run_time:
                    upcoming.append({
                        "id": job.id,
                        "next_run": job.next_run_time.isoformat(),
                        "in_seconds": max(
                            round((job.next_run_time - datetime.now(timezone.utc))
                                  .total_seconds()), 0,
                        ),
                    })
            upcoming.sort(key=lambda j: j["in_seconds"])

        return JSONResponse({
            "running": registry.snapshot(),
            "scheduled": upcoming,
            "scheduler": scheduler is not None and scheduler.running,
            "last_sync_age_seconds": await store.last_sync_age_seconds(),
        })

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        from sonde.db import store

        stats = await store.dashboard_stats()
        return TEMPLATES.TemplateResponse(
            request=request, name="index.html", context={"stats": stats, "settings": settings}
        )

    @app.get("/followers/{did:path}", response_class=HTMLResponse)
    async def follower_detail(request: Request, did: str) -> HTMLResponse:
        from fastapi import HTTPException
        from sonde.db import store

        person = await store.follower_detail(did)
        if person is None:
            raise HTTPException(status_code=404, detail="not tracked")
        return TEMPLATES.TemplateResponse(
            request=request, name="detail.html",
            context={"p": person, "settings": settings},
        )

    @app.get("/export.csv")
    async def export_csv() -> StreamingResponse:
        import csv
        import io

        from sonde.db import store

        rows = await store.export_rows()
        buffer = io.StringIO()
        fields = [
            "did", "handle", "display_name", "followers_count", "follows_count",
            "posts_count", "verified_status", "trusted_verifier_status",
            "influence_score", "account_created_at", "first_seen_at", "list_rank",
            "is_mutual", "is_private",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        buffer.seek(0)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="sonde-followers.csv"'},
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        from sonde.db import store
        from sonde.scoring import WEIGHTS
        from sonde.sync import backup

        return TEMPLATES.TemplateResponse(
            request=request, name="settings.html",
            context={
                "weights": WEIGHTS,
                "counts": await store.counts(),
                "progress": await store.hydration_progress(),
                "runs": await store.recent_runs(15),
                "active": sorted(await store.active_score_components()),
                "last_backup": await backup.last_backup(),
                "needs_review": await store.get_meta("needs_review_count"),
                "running": registry.running(),
                "settings": settings,
            },
        )

    @app.post("/settings/sync/{kind}")
    async def trigger_sync(kind: str) -> RedirectResponse:
        """Manual trigger. Single-flight: a second request attaches to the run."""
        from fastapi import HTTPException

        from sonde.sync import backup, mutuals, profiles, runner

        jobs = {
            "head": runner.head_sweep,
            "full": runner.full_sweep,
            "hydrate": lambda: profiles.hydrate(limit=1000),
            "follows": mutuals.sync_follows,
            "rescore": profiles.rescore,
            "backup": backup.snapshot,
        }
        if kind not in jobs:
            raise HTTPException(status_code=404, detail="unknown job")
        # spawn() keeps a strong reference; bare create_task lets the event
        # loop drop the task mid-flight and swallow its exception.
        registry.spawn(kind, jobs[kind])
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/accept-departures")
    async def accept_departures() -> RedirectResponse:
        """Operator override for a halted sweep — see PLAN.md integrity rule 5."""
        from sonde.sync import runner

        await runner.accept_pending_departures()
        return RedirectResponse("/settings", status_code=303)

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
        q: str | None = None, verified: bool = False, mutual: bool = False,
        min_followers: int | None = None,
    ) -> HTMLResponse:
        from sonde.db import store

        per_page = 100
        rows = await store.ranked_followers(
            limit=per_page, offset=(max(page, 1) - 1) * per_page, order=order,
            verified_only=verified, mutual_only=mutual,
            min_followers=min_followers, query=q,
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="followers.html",
            context={
                "rows": rows, "page": max(page, 1), "order": order, "q": q or "",
                "verified": verified, "mutual": mutual, "min_followers": min_followers,
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
