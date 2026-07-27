"""Wikidata: reputation from outside Bluesky, as a bulk join.

The governing principle is **join, don't search**. Wikidata has property
`P12361` — Bluesky handle — so the entire Bluesky↔Wikidata mapping is a single
SPARQL query. Measured 2026-07-26: 10,563 pairs in 5.8 seconds, one HTTP call.
After that it is a local join, at zero per-follower cost, forever.

Against this follower set that matched 107 people (1.07%) — low coverage,
perfect precision, and exactly the right people: Naomi Klein (67 language
editions), Bruce Sterling, Meredith Whittaker, Molly White, Emily M. Bender.

Sitelink count is the notability measure: how many language Wikipedias consider
someone worth an article. It is editorially reviewed, hard to game, and already
computed by someone else.

The same mechanism resolves *organisations*, which is what makes "President of
Signal" work without anyone hand-adding Signal to a list.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from sonde.db import store

log = logging.getLogger("sonde.wikidata")

ENDPOINT = "https://query.wikidata.org/sparql"
# Wikimedia asks for a descriptive agent with a contact address.
USER_AGENT = "sonde/0.1 (https://github.com/danhon/sonde; follower analytics)"

# TWO queries, deliberately. Folding occupation, employer and position into the
# mapping query with three OPTIONAL joins and GROUP_CONCAT over ~10,500 entities
# times out the public endpoint with a 504 — measured, not guessed. The mapping
# alone returns in under six seconds.
#
# So: fetch the whole mapping cheaply, join it locally to find the ~1% who are
# actually followers, then ask for detail on only those, bound with VALUES.
MAPPING_QUERY = """
SELECT ?handle ?item ?itemLabel ?sitelinks WHERE {
  ?item wdt:P12361 ?handle .
  ?item wikibase:sitelinks ?sitelinks .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# P106 = occupation, P108 = employer, P39 = position held.
#
# Employers and positions are read through the STATEMENT rather than the direct
# `wdt:` shortcut, so an end-date qualifier (P582) can exclude past roles.
# Without this, Meredith Whittaker came back as employed by Google — true until
# 2019, and wrong now that she is President of Signal. Occupations need no such
# filter: "journalist" does not stop being true when you change jobs.
DETAIL_QUERY = """
SELECT ?item
       (GROUP_CONCAT(DISTINCT ?occLabel; separator="|") AS ?occupations)
       (GROUP_CONCAT(DISTINCT ?empLabel; separator="|") AS ?employers)
       (GROUP_CONCAT(DISTINCT ?posLabel; separator="|") AS ?positions)
       (GROUP_CONCAT(DISTINCT ?pastLabel; separator="|") AS ?past_employers)
WHERE {
  VALUES ?item { %s }
  OPTIONAL { ?item wdt:P106 ?occ . ?occ rdfs:label ?occLabel . FILTER(LANG(?occLabel)="en") }
  OPTIONAL {
    ?item p:P108 ?empSt . ?empSt ps:P108 ?emp .
    FILTER NOT EXISTS { ?empSt pq:P582 ?empEnd }
    ?emp rdfs:label ?empLabel . FILTER(LANG(?empLabel)="en")
  }
  OPTIONAL {
    ?item p:P108 ?pastSt . ?pastSt ps:P108 ?past .
    ?pastSt pq:P582 ?pastEnd .
    ?past rdfs:label ?pastLabel . FILTER(LANG(?pastLabel)="en")
  }
  OPTIONAL {
    ?item p:P39 ?posSt . ?posSt ps:P39 ?pos .
    FILTER NOT EXISTS { ?posSt pq:P582 ?posEnd }
    ?pos rdfs:label ?posLabel . FILTER(LANG(?posLabel)="en")
  }
}
GROUP BY ?item
"""
DETAIL_BATCH = 250


class TruncatedResponse(Exception):
    """The endpoint returned 200 with a body that is not complete JSON."""


async def _sparql(query: str, timeout: float = 180.0, attempts: int = 4) -> list[dict]:
    """POST the query, retrying truncated and throttled responses.

    The public endpoint answers a too-slow query with HTTP 200 and a body that
    simply stops mid-object, so `raise_for_status()` passes and `.json()` then
    fails with "Expecting property name enclosed in double quotes". Seen in
    production on 2026-07-27; it works locally whenever the endpoint is less
    loaded, which is exactly the kind of failure that only shows up in
    production.

    POST rather than GET: Wikimedia recommends it for anything long, and a
    ~14KB query in a URL is asking for trouble at proxies.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            # Long backoff on purpose — retrying a heavy query immediately just
            # adds load to the thing that is already struggling.
            await asyncio.sleep(min(15 * (2 ** (attempt - 1)), 120))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    ENDPOINT,
                    data={"query": query},
                    headers={"Accept": "application/sparql-results+json",
                             "User-Agent": USER_AGENT,
                             "Content-Type": "application/x-www-form-urlencoded"},
                )
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = httpx.HTTPStatusError(
                    f"{resp.status_code} from Wikidata", request=resp.request,
                    response=resp,
                )
                log.warning("wikidata returned %s (attempt %d/%d)",
                            resp.status_code, attempt + 1, attempts)
                continue
            resp.raise_for_status()
            try:
                payload = resp.json()
            except ValueError as exc:
                # 200 with an incomplete body.
                last_error = TruncatedResponse(
                    f"{exc} (received {len(resp.content):,} bytes)"
                )
                log.warning("wikidata response was truncated at %s bytes "
                            "(attempt %d/%d)", f"{len(resp.content):,}",
                            attempt + 1, attempts)
                continue
            return payload["results"]["bindings"]
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = exc
            log.warning("wikidata transport error: %s (attempt %d/%d)",
                        exc, attempt + 1, attempts)
    raise last_error or TruncatedResponse("wikidata query failed")


def _value(row: dict, key: str) -> str | None:
    entry = row.get(key)
    return entry.get("value") if entry else None


def _split(row: dict, key: str) -> list[str]:
    raw = _value(row, key) or ""
    return sorted({part.strip() for part in raw.split("|") if part.strip()})


def parse_people(rows: list[dict]) -> dict[str, dict]:
    """Map handle -> entity facts. Handles are normalised for joining."""
    out: dict[str, dict] = {}
    for row in rows:
        handle = (_value(row, "handle") or "").lower().lstrip("@").strip()
        if not handle:
            continue
        try:
            sitelinks = int(_value(row, "sitelinks") or 0)
        except ValueError:
            sitelinks = 0
        out[handle] = {
            "qid": (_value(row, "item") or "").rsplit("/", 1)[-1],
            "label": _value(row, "itemLabel"),
            "sitelinks": sitelinks,
            "occupations": _split(row, "occupations"),
            "employers": _split(row, "employers"),
            "positions": _split(row, "positions"),
            "past_employers": _split(row, "past_employers"),
        }
    return out


def parse_detail(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        qid = (_value(row, "item") or "").rsplit("/", 1)[-1]
        if qid:
            out[qid] = {
                "occupations": _split(row, "occupations"),
                "employers": _split(row, "employers"),
                "positions": _split(row, "positions"),
                "past_employers": _split(row, "past_employers"),
            }
    return out


async def refresh(fetch=None) -> dict:
    """Bulk mapping, local join, then detail for the handful that matched."""
    run_id = await store.start_run("wikidata")
    query = fetch or _sparql
    calls = 0
    try:
        mapping = parse_people(await query(MAPPING_QUERY))
        calls += 1

        # Only the followers actually matched need occupation and employer —
        # about 1% of the mapping, which keeps the detail query small enough
        # to answer.
        wanted = await store.wikidata_qids_for_followers(mapping)
        for start in range(0, len(wanted), DETAIL_BATCH):
            batch = wanted[start:start + DETAIL_BATCH]
            values = " ".join(f"wd:{qid}" for qid in batch)
            detail = parse_detail(await query(DETAIL_QUERY % values))
            calls += 1
            for handle, entity in mapping.items():
                extra = detail.get(entity["qid"])
                if extra:
                    entity.update(extra)

        matched = await store.apply_wikidata(mapping)
        await store.rescore_all()
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc), api_calls=calls)
        log.exception("wikidata refresh failed")
        raise

    await store.finish_run(run_id, status="ok", completed=1,
                           actors_seen=matched, api_calls=calls)
    log.info("wikidata: %d entities fetched, %d followers matched", len(mapping), matched)
    return {"status": "ok", "kind": "wikidata", "entities": len(mapping),
            "matched": matched, "api_calls": calls}
