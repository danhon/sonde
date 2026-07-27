"""Tier 3 — the inverted follow-graph index, and institution rosters.

Affinity answers "who is influential in MY corner of the network", which raw
follower count cannot. `getKnownFollowers` would answer it directly but costs one
call per follower and needs auth, so sonde inverts the question: fetch the follow
lists of a sample of accounts I follow, once, and every follower's overlap
becomes a local lookup covering all 10,042 at once.

Source selection was piloted rather than assumed — see
SCORING.md#choosing-index-sources. Taking "the most selective accounts I follow"
picks accounts following 0–15 people and reaches 0.8% of followers; a mid-band
sample reaches 14% from a third as many sources. Selectivity is applied to the
HIT WEIGHT instead, where it belongs.
"""

from __future__ import annotations

import logging

from sonde.api.client import BlueskyClient
from sonde.api.graph import iter_follows
from sonde.config import settings
from sonde.db import store
from sonde.jobs import registry

log = logging.getLogger("sonde.affinity")


def source_weight(follows_count: int | None) -> float:
    """How much one source's endorsement is worth.

    A source following the band minimum contributes 1.0 per hit; one following
    ten times that contributes 0.1. This is where "an account following 200
    endorses more meaningfully than one following 50,000" actually belongs.
    """
    if not follows_count or follows_count <= 0:
        return 1.0
    ratio = settings.affinity_min_follows / follows_count
    return max(0.1, min(1.0, ratio))


async def build_index(client: BlueskyClient | None = None, *, cap: int | None = None) -> dict:
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("affinity")
    cap = cap if cap is not None else settings.affinity_max_sources

    scores: dict[str, float] = {}
    verified_hits: dict[str, int] = {}
    sources_used = failed = 0

    try:
        followers = await store.known_dids()
        # Our own account turns up in other people's follow lists and would
        # otherwise top its own ranking.
        me = await store.subject_did()
        followers.discard(me)
        if not followers:
            await store.finish_run(run_id, status="ok", completed=1, api_calls=0)
            return {"status": "ok", "kind": "affinity", "sources": 0,
                    "reached": 0, "api_calls": 0, "skipped": "no followers"}

        sources = await store.choose_affinity_sources(
            settings.affinity_min_follows, settings.affinity_max_follows, cap
        )
        if not sources:
            await store.finish_run(run_id, status="ok", completed=1, api_calls=0)
            return {"status": "ok", "kind": "affinity", "sources": 0, "reached": 0,
                    "api_calls": 0, "skipped": "no sources — sync follows first"}

        log.info(
            "building affinity index from %d sources (follows %d–%d)",
            len(sources), sources[0]["follows_count"], sources[-1]["follows_count"],
        )

        for index, source in enumerate(sources, 1):
            registry.progress(
                "affinity", index, len(sources), "sources",
                f"{len(scores):,} followers reached",
            )
            weight = source_weight(source["follows_count"])
            is_verified = source.get("verified_status") == "valid"
            pages = 0
            try:
                async for page, actors in iter_follows(client, source["did"]):
                    pages = page
                    for a in actors:
                        did = a.get("did")
                        if did in followers:
                            scores[did] = scores.get(did, 0.0) + weight
                            if is_verified:
                                verified_hits[did] = verified_hits.get(did, 0) + 1
            except Exception:
                # One unreachable source must not abort a 600-source run.
                failed += 1
                log.debug("affinity source %s failed", source["did"], exc_info=True)
                continue
            sources_used += 1
            if index % 25 == 0:
                await store.record_progress(
                    run_id, pages=sources_used, actors=len(scores), calls=client.calls
                )
            await store.record_affinity_source(
                source["did"], source["handle"], source["follows_count"],
                weight, is_verified, pages,
            )

        await store.store_affinity(scores, verified_hits)
        await store.rescore_all()
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc), api_calls=client.calls)
        log.exception("affinity index failed")
        raise
    finally:
        if owns_client:
            await client.aclose()

    await store.finish_run(
        run_id, status="ok", completed=1, actors_seen=len(scores),
        pages_fetched=sources_used, api_calls=client.calls,
    )
    reached = len(scores)
    return {
        "status": "ok", "kind": "affinity", "sources": sources_used,
        "failed_sources": failed, "reached": reached,
        "coverage_pct": round(reached / max(len(followers), 1) * 100, 1),
        "verified_reached": len(verified_hits), "api_calls": client.calls,
    }


async def fetch_rosters(client: BlueskyClient | None = None) -> dict:
    """Enumerate each institution's published verification records.

    Records live in the VERIFIER's repo, so one paginated public call per
    institution yields their whole verified staff list.
    """
    owns_client = client is None
    client = client or BlueskyClient()
    run_id = await store.start_run("rosters")
    fetched = total = 0
    try:
        for inst in await store.all_institutions():
            did = inst.get("verifier_did")
            if not did:
                did = await _resolve_verifier_did(client, inst)
                if not did:
                    continue
            records = await _list_verifications(client, did)
            if records:
                total += await store.save_roster(inst["id"], records)
                fetched += 1
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc), api_calls=client.calls)
        raise
    finally:
        if owns_client:
            await client.aclose()

    await store.finish_run(run_id, status="ok", completed=1,
                           actors_seen=total, api_calls=client.calls)
    return {"status": "ok", "kind": "rosters", "institutions": fetched,
            "records": total, "api_calls": client.calls}


async def _resolve_verifier_did(client: BlueskyClient, inst: dict) -> str | None:
    for domain in inst.get("domains") or []:
        try:
            profile = await client.xrpc("app.bsky.actor.getProfile", {"actor": domain})
        except Exception:
            continue
        did = profile.get("did")
        if did:
            db = await store._db()  # noqa: SLF001
            await db.execute(
                "UPDATE institutions SET verifier_did = ? WHERE id = ?", (did, inst["id"])
            )
            await db.commit()
            return did
    return None


async def _list_verifications(client: BlueskyClient, repo: str) -> list[dict]:
    """Walk `com.atproto.repo.listRecords` on the verifier's own PDS."""
    try:
        doc = await client.plc(repo)
    except Exception:
        return []
    pds = _pds_endpoint(doc)
    if not pds:
        return []

    out: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, object] = {
            "repo": repo,
            "collection": "app.bsky.graph.verification",
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            data = await client.xrpc("com.atproto.repo.listRecords", params, base_url=pds)
        except Exception:
            return out
        for record in data.get("records", []):
            value = record.get("value") or {}
            if value.get("subject"):
                out.append({
                    "did": value["subject"],
                    "handle": value.get("handle"),
                    "createdAt": value.get("createdAt"),
                })
        cursor = data.get("cursor")
        if not cursor:
            return out


def _pds_endpoint(doc: dict) -> str | None:
    for service in doc.get("service") or []:
        if service.get("type") == "AtprotoPersonalDataServer":
            return service.get("serviceEndpoint")
    return None
