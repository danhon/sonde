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

from sonde.api.auth import authenticator
from sonde.config import settings
from sonde.jobs import registry

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _as_int(value: str | int | None) -> int | None:
    """Coerce a query-string value, treating blank and junk as absent.

    HTML forms submit empty fields as "", which strict int parsing rejects with
    a 422 — taking the whole page down rather than the one filter.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

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
        person["posts"] = await store.posts_for(did)
        person["moderation_lists"] = await store.lists_matching(did)
        person["affiliations"] = await store.affiliations_for(did)
        person["groups"] = await store.groups_for(did)
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

    @app.get("/institutions", response_class=HTMLResponse)
    async def institutions(
        request: Request, name: str | None = None, kind: str | None = None,
        order: str = "members", direction: str = "desc",
    ) -> HTMLResponse:
        from sonde.db import store

        direction = "asc" if direction == "asc" else "desc"
        return TEMPLATES.TemplateResponse(
            request=request, name="institutions.html",
            context={
                "orgs": await store.organisation_summary(
                    order=order, direction=direction, kind=kind),
                "kinds": await store.organisation_kinds(),
                "detail": await store.organisation_members(name) if name else None,
                "name": name, "kind": kind, "order": order, "direction": direction,
                "settings": settings,
            },
        )

    @app.post("/institutions/{name}/weight")
    async def set_org_weight(name: str, weight: float = 0.7) -> RedirectResponse:
        from sonde.db import store

        await store.set_organisation_weight(name, weight)
        return RedirectResponse(f"/institutions?name={name}", status_code=303)

    @app.get("/groups", response_class=HTMLResponse)
    async def groups_index(
        request: Request, slug: str | None = None,
        order: str = "influence", direction: str = "desc",
    ) -> HTMLResponse:
        from sonde.db import store

        direction = "asc" if direction == "asc" else "desc"
        return TEMPLATES.TemplateResponse(
            request=request, name="groups.html",
            context={
                "summary": await store.group_summary(),
                "slug": slug, "order": order, "direction": direction,
                "members": await store.group_members(
                    slug, order=order, direction=direction) if slug else [],
                "settings": settings,
            },
        )

    @app.post("/groups/{slug}/{did}/review")
    async def review_group(slug: str, did: str, keep: bool = True) -> RedirectResponse:
        from sonde.db import store

        await store.review_group_member(slug, did, keep)
        return RedirectResponse(f"/groups?slug={slug}", status_code=303)

    @app.get("/ignored", response_class=HTMLResponse)
    async def ignored(request: Request) -> HTMLResponse:
        from sonde.db import store

        return TEMPLATES.TemplateResponse(
            request=request, name="ignored.html",
            context={
                "rows": await store.ignored_followers(),
                "lists": await store.moderation_lists(),
                "dismissals": await store.dismissal_log(),
                "settings": settings,
            },
        )

    @app.post("/followers/{did}/posts")
    async def fetch_posts_now(did: str) -> RedirectResponse:
        """On-demand fetch for anyone outside the automatic set."""
        from sonde.api.client import BlueskyClient
        from sonde.sync.posts import fetch_one

        client = BlueskyClient()
        try:
            await fetch_one(client, did)
        finally:
            await client.aclose()
        return RedirectResponse(f"/followers/{did}", status_code=303)

    @app.post("/followers/{did}/ignore")
    async def ignore_follower(did: str, restore: bool = False) -> RedirectResponse:
        """Hide or restore. Always locks the decision so a moderation refresh
        cannot overrule a human."""
        from sonde.db import store

        await store.set_ignored(did, not restore, reason="manual", lock=True)
        return RedirectResponse(
            "/ignored" if restore else f"/followers/{did}", status_code=303
        )

    @app.post("/settings/lists/{rkey}")
    async def toggle_list(rkey: str, enabled: bool = False) -> RedirectResponse:
        from sonde.db import store
        from sonde.sync import moderation  # noqa: F401 - keeps the module importable

        for entry in await store.moderation_lists():
            if entry["uri"].rsplit("/", 1)[-1] == rkey:
                await store.set_list_enabled(entry["uri"], enabled)
                await store.apply_moderation_hides()
                break
        return RedirectResponse("/ignored", status_code=303)

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
                "auth": authenticator.status(),
                "lists": await store.moderation_lists(),
                "dismissals": await store.dismissal_log(),
                "needs_review": await store.get_meta("needs_review_count"),
                "running": registry.running(),
                "settings": settings,
            },
        )

    @app.post("/settings/sync/{kind}")
    async def trigger_sync(kind: str) -> RedirectResponse:
        """Manual trigger. Single-flight: a second request attaches to the run."""
        from fastapi import HTTPException

        from sonde.db import store
        from sonde.external import wikidata, wikipedia
        from sonde.notify import digest

        async def external_job() -> dict:
            return {"wikidata": await wikidata.refresh(),
                    "pageviews": await wikipedia.refresh()}

        from sonde.sync import (
            backup, moderation, mutuals, posts, profiles, relevance, runner,
        )

        jobs = {
            "head": runner.head_sweep,
            "full": runner.full_sweep,
            "hydrate": lambda: profiles.hydrate(limit=1000),
            "posts": posts.fetch_posts,
            "relevance": relevance.enrich,
            "digest": lambda: digest.run_digest(force=True),
            "external": external_job,
            "affiliations": store.rebuild_affiliations,
            "groups": store.classify_groups,
            "moderation": moderation.sync_lists,
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
    async def influential(
        request: Request, page: str | None = None, order: str = "influence"
    ) -> HTMLResponse:
        from sonde.db import store
        from sonde.scoring import WEIGHTS

        per_page = 50
        page = max(_as_int(page) or 1, 1)
        rows = await store.ranked_followers(
            limit=per_page, offset=(page - 1) * per_page, order=order
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="influential.html",
            context={
                "rows": rows, "page": page, "order": order,
                "weights": WEIGHTS, "progress": await store.hydration_progress(),
                "settings": settings,
            },
        )

    @app.get("/followers", response_class=HTMLResponse)
    async def followers(
        request: Request, page: str | None = None, order: str = "influence",
        direction: str = "desc", q: str | None = None,
        verified: bool = False, mutual: bool = False,
        min_followers: str | None = None,
    ) -> HTMLResponse:
        """Numeric filters arrive as strings on purpose.

        An HTML form submits every field it contains, so leaving "Min
        followers" blank sends `min_followers=`. Typed as `int | None` that is
        a 422 and the page never renders — which is why sorting appeared
        broken: the form never returned a table at all.
        """
        from sonde.db import store

        per_page = 100
        page_num = max(_as_int(page) or 1, 1)
        floor = _as_int(min_followers)
        direction = "asc" if direction == "asc" else "desc"

        rows = await store.ranked_followers(
            limit=per_page, offset=(page_num - 1) * per_page, order=order,
            direction=direction, verified_only=verified, mutual_only=mutual,
            min_followers=floor, query=q,
        )
        # Every link on the page has to carry the current filters, or
        # paginating or re-sorting silently drops them.
        filters = {"q": q or "", "verified": verified, "mutual": mutual,
                   "min_followers": floor if floor is not None else ""}
        return TEMPLATES.TemplateResponse(
            request=request,
            name="followers.html",
            context={
                "rows": rows, "page": page_num, "order": order,
                "direction": direction, "filters": filters,
                "q": q or "", "verified": verified, "mutual": mutual,
                "min_followers": floor,
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
            context={
                "v": summary,
                "notices": await store.active_notices(),
                "settings": settings,
            },
        )

    @app.post("/notices/{kind}/{signature}/dismiss")
    async def dismiss_notice(kind: str, signature: str, back: str = "/verified"):
        from sonde.db import store

        await store.dismiss_notice(kind, signature)
        return RedirectResponse(back if back.startswith("/") else "/verified",
                                status_code=303)

    @app.post("/notices/{dismissal_id}/restore")
    async def restore_notice(dismissal_id: int) -> RedirectResponse:
        from sonde.db import store

        await store.restore_notice(dismissal_id)
        return RedirectResponse("/settings", status_code=303)

    return app


app = create_app()
