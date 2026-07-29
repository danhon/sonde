"""FastAPI application."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
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


def _safe_back(request: Request, fallback: str) -> str:
    """Where to send the operator after a write.

    Only ever a page inside sonde, never an absolute URL from a header we do
    not control. Extracted from `follow_back` when the tag routes became the
    fifth caller.
    """
    back = request.headers.get("referer") or fallback
    if "://" in back:
        rest = back.split("://", 1)[1]
        back = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    return back

STARTED_AT = time.time()


def _attention_scatter(points: list[dict]) -> dict:
    """The M17 region picked out: real reach, few follows."""
    from sonde import attention, charts

    for p in points:
        p["notable"] = attention.raw(p.get("followers"), p.get("follows")) > 0.25
    plot = charts.scatter(points, charts.Box(height=260),
                          x_key="follows", y_key="followers")
    # Direct labels, but only where they can be read. The first version took
    # the top six by reach and drew "mattbors", "wiswell" and
    # "tylerfromtheinte" on top of each other. A label that overlaps another
    # label is worse than no label, so a candidate is skipped when it lands
    # within 14px vertically of one already placed.
    notable = sorted((p for p in plot["points"] if p.get("notable")),
                     key=lambda p: -(p.get("followers") or 0))
    placed: list[dict] = []
    for point in notable:
        if any(abs(point["cy"] - other["cy"]) < 14
               and abs(point["cx"] - other["cx"]) < 150 for other in placed):
            continue
        placed.append(point)
        if len(placed) == 5:
            break
    plot["highlights"] = placed
    return plot


def _interaction_panels(series: dict) -> dict:
    """One panel per kind, each with its own scale — never stacked."""
    from sonde import charts

    panels = []
    for index, (kind, values) in enumerate(series["series"].items()):
        panels.append({
            "kind": kind, "colour": charts.SERIES[index % len(charts.SERIES)],
            "spark": charts.sparkline(values),
        })
    return {"panels": panels, "months": series["months"]}


def _composition_bars(data: dict) -> dict:
    from sonde import charts

    data["bars"] = charts.bars([(g["name"], g["n"]) for g in data["groups"]])
    return data


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

        from sonde import charts

        stats = await store.dashboard_stats()
        cohorts = await store.account_cohorts()
        return TEMPLATES.TemplateResponse(
            request=request, name="index.html",
            context={
                "stats": stats, "settings": settings,
                "cohorts": cohorts,
                # Operational faults belong on the page you land on, not buried
                # on /settings where nobody was looking when the backup broke.
                "notices_list": await store.active_notices(
                    kinds=("backup_failing",)),
                "cohort_plot": charts.columns(
                    [(r["month"], r["n"]) for r in cohorts],
                    charts.Box(height=180)),
            },
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
        person["interactions"] = await store.interactions_for(did)
        person["breakdown"] = await store.interaction_breakdown(did)
        return TEMPLATES.TemplateResponse(
            request=request, name="detail.html",
            context={"p": person, "settings": settings,
                      "all_tags": await store.group_names()},
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
            "influence_score", "account_created_at",
            # `following_since` is the answer to "since when"; `first_seen_at`
            # is kept beside it because the two differ for backfilled rows and
            # an export that silently conflated them would be wrong forever.
            "following_since", "following_since_exact", "first_seen_at", "list_rank",
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

    @app.get("/relationships", response_class=HTMLResponse)
    async def relationships(
        request: Request, order: str = "relationship", direction: str = "desc",
        page: str | None = None,
    ) -> HTMLResponse:
        from sonde.db import store

        per_page = 100
        page_num = max(_as_int(page) or 1, 1)
        direction = "asc" if direction == "asc" else "desc"
        return TEMPLATES.TemplateResponse(
            request=request, name="relationships.html",
            context={
                "scatter": _attention_scatter(await store.attention_points()),
                "rows": await store.ranked_relationships(
                    limit=per_page, offset=(page_num - 1) * per_page,
                    order=order, direction=direction),
                "order": order, "direction": direction, "page": page_num,
                "settings": settings,
            },
        )

    @app.get("/interactions", response_class=HTMLResponse)
    async def interactions_page(
        request: Request, kind: str = "reply", direction: str = "inbound",
        days: int | None = None, order: str = "count",
        sort: str = "desc",
    ) -> HTMLResponse:
        """One kind at a time. A like and a reply do not belong in one ranking."""
        from sonde.db import store

        return TEMPLATES.TemplateResponse(
            request=request, name="interactions.html",
            context={
                "rows": await store.interaction_leaderboard(
                    kind, direction=direction, days=days,
                    order=order, sort_direction=sort),
                "totals": await store.interaction_totals(days=days),
                "panels": _interaction_panels(
                    await store.interaction_series(direction=direction)),
                "window": await store.interaction_window(),
                "kinds": store.INTERACTION_KINDS,
                "kind": kind, "direction": direction, "days": days,
                "order": order, "sort": sort,
                "settings": settings,
            },
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
        notice: str | None = None,
    ) -> HTMLResponse:
        from sonde.db import store

        direction = "asc" if direction == "asc" else "desc"
        return TEMPLATES.TemplateResponse(
            request=request, name="groups.html",
            context={
                "composition": _composition_bars(await store.composition()),
                "summary": await store.group_summary(),
                "slug": slug, "order": order, "direction": direction,
                "members": await store.group_members(
                    slug, order=order, direction=direction) if slug else [],
                "settings": settings,
                "notice": notice,
                "all_tags": await store.group_names(),
                "archived": await store.archived_groups(),
            },
        )

    @app.get("/groups/discover", response_class=HTMLResponse)
    async def discover(request: Request) -> HTMLResponse:
        from sonde.db import store

        return TEMPLATES.TemplateResponse(
            request=request, name="discover.html",
            context={
                "candidates": await store.group_candidates(),
                "accepted": await store.group_candidates(decided=True, limit=50),
                "rejected": await store.group_candidates(decided=False, limit=50),
                "settings": settings,
            },
        )

    @app.post("/groups/discover/{candidate_id}")
    async def decide(candidate_id: int, accept: bool = False) -> RedirectResponse:
        from sonde.db import store

        slug = await store.decide_candidate(candidate_id, accept)
        return RedirectResponse(
            f"/groups?slug={slug}" if slug else "/groups/discover", status_code=303
        )

    @app.post("/groups/{slug}/{did}/review")
    async def review_group(slug: str, did: str, keep: bool = True) -> RedirectResponse:
        from sonde.db import store

        await store.review_group_member(slug, did, keep)
        return RedirectResponse(f"/groups?slug={slug}", status_code=303)

    async def _apply_tags(back: str, dids: list[str], tag: str,
                          action: str) -> RedirectResponse:
        """Shared by the batch bar and the single control on a detail page."""
        from sonde.db import store

        tag = (tag or "").strip()
        if not tag or not dids:
            return RedirectResponse(f"{back}#nothing-selected", status_code=303)

        if action == "add":
            # Type-to-create: an unknown name in the box makes the tag. Only on
            # add — a typo while removing must not conjure an empty tag.
            result = await store.create_group(tag)
            if result["status"] in ("invalid", "archived"):
                return RedirectResponse(f"{back}#tag-{result['status']}",
                                        status_code=303)
            slug = result["slug"]
        else:
            slug = store.slugify(tag)

        changed = await store.tag_actors(slug, dids, add=(action == "add"))
        return RedirectResponse(f"{back}#tagged-{changed}", status_code=303)

    @app.post("/groups/new")
    async def new_group(name: str = Form("")) -> RedirectResponse:
        from sonde.db import store

        result = await store.create_group(name)
        if result["status"] == "created":
            return RedirectResponse(f"/groups?slug={result['slug']}", status_code=303)
        suffix = f"&slug={result['slug']}" if result["slug"] else ""
        return RedirectResponse(f"/groups?notice={result['status']}{suffix}",
                                status_code=303)

    @app.post("/groups/{slug}/rename")
    async def rename_group(slug: str, name: str = Form("")) -> RedirectResponse:
        from sonde.db import store

        await store.rename_group(slug, name)
        return RedirectResponse(f"/groups?slug={slug}", status_code=303)

    @app.post("/groups/{slug}/archive")
    async def archive_group(slug: str, undo: bool = False) -> RedirectResponse:
        from sonde.db import store

        await store.archive_group(slug, archived=not undo)
        return RedirectResponse(f"/groups?slug={slug}" if undo else "/groups",
                                status_code=303)

    @app.post("/groups/apply")
    async def apply_tags(request: Request, did: list[str] = Form([]),
                         tag: str = Form(""),
                         action: str = Form("add")) -> RedirectResponse:
        return await _apply_tags(_safe_back(request, "/followers"), did, tag, action)

    @app.post("/followers/{did}/tags")
    async def tag_one(request: Request, did: str, tag: str = Form(""),
                      action: str = Form("add")) -> RedirectResponse:
        return await _apply_tags(
            _safe_back(request, f"/followers/{did}"), [did], tag, action)

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

    @app.post("/followers/{did}/follow")
    async def follow_back(request: Request, did: str,
                          undo: bool = False) -> RedirectResponse:
        """One click, and one click to undo. The only write sonde makes.

        Returns the operator to the page they clicked from, because this is
        meant to be used while working down a list.
        """
        from sonde.api.client import BlueskyClient
        from sonde.api.follows import FollowError, create_follow, delete_follow
        from sonde.db import store

        back = _safe_back(request, f"/followers/{did}")

        if not settings.enable_follow_write:
            return RedirectResponse(f"{back}#writes-disabled", status_code=303)

        target = await store.follow_back_target(did)
        if target is None:
            return RedirectResponse(f"{back}#unknown", status_code=303)

        client = BlueskyClient()
        try:
            if undo:
                await delete_follow(client, target.get("follow_uri") or "")
                await store.forget_my_follow(did)
            else:
                if target["already"]:
                    return RedirectResponse(back, status_code=303)
                if not target["is_current"] or target["ignored_at"]:
                    # Follow-back only: sonde never follows a stranger, so a
                    # stray DID fails here rather than writing.
                    return RedirectResponse(f"{back}#not-a-follower",
                                            status_code=303)
                uri = await create_follow(client, did)
                await store.record_my_follow(did, uri)
        except (FollowError, Exception) as exc:  # noqa: BLE001
            log.warning("follow-back failed for %s: %s", did, exc)
            await store.record_follow_failure(did, str(exc), undo=undo)
            return RedirectResponse(f"{back}#follow-failed", status_code=303)
        finally:
            await client.aclose()
        return RedirectResponse(back, status_code=303)

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
        from sonde import joblist
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
                "backup_attempts": await store.backup_attempts(),
                "auth": authenticator.status(),
                "lists": await store.moderation_lists(),
                "dismissals": await store.dismissal_log(),
                "needs_review": await store.get_meta("needs_review_count"),
                "running": registry.running(),
                "jobs": joblist.JOBS, "batches": joblist.BATCHES,
                "in_a_batch": joblist.IN_A_BATCH,
                "settings": settings,
            },
        )

    def _job_map() -> dict:
        """Every runnable job. Imports are local to keep app import cheap."""
        override = globals().get("_JOB_MAP_OVERRIDE")
        if override is not None:
            return override()

        from sonde.db import store
        from sonde.external import wikidata, wikipedia
        from sonde.notify import digest
        from sonde.sync import (
            affinity, backup, interactions, moderation, mutuals, posts,
            profiles, relevance, runner,
        )

        async def external_job() -> dict:
            return {"wikidata": await wikidata.refresh(),
                    "pageviews": await wikipedia.refresh()}

        return {
            "head": runner.head_sweep,
            "full": runner.full_sweep,
            "hydrate": lambda: profiles.hydrate(limit=1000),
            "follows": mutuals.sync_follows,
            "posts": posts.fetch_posts,
            "external": external_job,
            "affiliations": store.rebuild_affiliations,
            "relevance": relevance.enrich,
            "groups": store.classify_groups,
            "discover": store.discover_group_candidates,
            "latent": store.discover_latent_groups,
            "propagate": store.propagate_groups,
            "affinity": affinity.build_index,
            "interactions": interactions.sync,
            "rescore-relationships": store.score_relationships,
            "moderation": moderation.sync_lists,
            "rescore": profiles.rescore,
            "backup": backup.snapshot,
            "digest": lambda: digest.run_digest(force=True),
        }

    @app.post("/settings/sync/{kind}")
    async def trigger_sync(kind: str) -> RedirectResponse:
        """Manual trigger. Single-flight: a second request attaches to the run."""
        from fastapi import HTTPException

        jobs = _job_map()
        if kind not in jobs:
            raise HTTPException(status_code=404, detail="unknown job")
        # spawn() keeps a strong reference; bare create_task lets the event
        # loop drop the task mid-flight and swallow its exception.
        registry.spawn(kind, jobs[kind])
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/batch/{key}")
    async def trigger_batch(key: str) -> RedirectResponse:
        """Run a batch's steps in order, stopping at the first failure.

        The ordering is real — enrichment needs hydrated profiles, grouping
        needs enrichment — and it used to live only in release notes.
        """
        from fastapi import HTTPException

        from sonde.joblist import BATCH_BY_KEY

        batch = BATCH_BY_KEY.get(key)
        if batch is None:
            raise HTTPException(status_code=404, detail="unknown batch")
        jobs = _job_map()

        async def run_steps() -> dict:
            results: dict = {}
            for position, step in enumerate(batch.steps, 1):
                registry.progress(key, position, len(batch.steps), "steps", step)
                results[step] = await jobs[step]()
                if isinstance(results[step], dict) and \
                        results[step].get("status") == "failed":
                    return {"status": "failed", "stopped_at": step, **results}
            return {"status": "ok", **results}

        registry.spawn(key, run_steps)
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
        min_followers: str | None = None, tag: str | None = None,
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
            min_followers=floor, query=q, tag=tag,
        )
        # Every link on the page has to carry the current filters, or
        # paginating or re-sorting silently drops them.
        filters = {"q": q or "", "verified": verified, "mutual": mutual,
                   "min_followers": floor if floor is not None else "",
                   "tag": tag or ""}
        return TEMPLATES.TemplateResponse(
            request=request,
            name="followers.html",
            context={
                "rows": rows, "page": page_num, "order": order,
                "direction": direction, "filters": filters,
                "q": q or "", "verified": verified, "mutual": mutual,
                "min_followers": floor, "tag": tag or "",
                "counts": await store.counts(), "settings": settings,
                "all_tags": await store.group_names(),
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
                "reach": await store.verification_reach(),
                "v": summary,
                # `notices` is the macro's name in the template, so the data
                # cannot also be called that.
                "notices_list": await store.active_notices(
                    kinds=("invalid_verification",)),
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
