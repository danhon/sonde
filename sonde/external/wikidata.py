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

import logging

import httpx

from sonde.db import store

log = logging.getLogger("sonde.wikidata")

ENDPOINT = "https://query.wikidata.org/sparql"
# Wikimedia asks for a descriptive agent with a contact address.
USER_AGENT = "sonde/0.1 (https://github.com/danhon/sonde; follower analytics)"

# One query, the whole mapping. P12361 = Bluesky handle, P106 = occupation,
# P108 = employer, P39 = position held.
PEOPLE_QUERY = """
SELECT ?handle ?item ?itemLabel ?sitelinks
       (GROUP_CONCAT(DISTINCT ?occLabel; separator="|") AS ?occupations)
       (GROUP_CONCAT(DISTINCT ?empLabel; separator="|") AS ?employers)
       (GROUP_CONCAT(DISTINCT ?posLabel; separator="|") AS ?positions)
WHERE {
  ?item wdt:P12361 ?handle .
  ?item wikibase:sitelinks ?sitelinks .
  OPTIONAL { ?item wdt:P106 ?occ . ?occ rdfs:label ?occLabel . FILTER(LANG(?occLabel)="en") }
  OPTIONAL { ?item wdt:P108 ?emp . ?emp rdfs:label ?empLabel . FILTER(LANG(?empLabel)="en") }
  OPTIONAL { ?item wdt:P39  ?pos . ?pos rdfs:label ?posLabel . FILTER(LANG(?posLabel)="en") }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?handle ?item ?itemLabel ?sitelinks
"""


async def _sparql(query: str, timeout: float = 180.0) -> list[dict]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            ENDPOINT,
            params={"query": query},
            headers={"Accept": "application/sparql-results+json",
                     "User-Agent": USER_AGENT},
        )
    resp.raise_for_status()
    return resp.json()["results"]["bindings"]


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
        }
    return out


async def refresh(fetch=None) -> dict:
    """Pull the mapping and join it locally. One HTTP call."""
    run_id = await store.start_run("wikidata")
    try:
        rows = await (fetch or _sparql)(PEOPLE_QUERY)
        mapping = parse_people(rows)
        matched = await store.apply_wikidata(mapping)
        await store.rescore_all()
        await store.commit()
    except Exception as exc:  # noqa: BLE001
        await store.finish_run(run_id, status="failed", error=str(exc), api_calls=1)
        log.exception("wikidata refresh failed")
        raise

    await store.finish_run(run_id, status="ok", completed=1,
                           actors_seen=matched, api_calls=1)
    log.info("wikidata: %d entities fetched, %d followers matched", len(mapping), matched)
    return {"status": "ok", "kind": "wikidata", "entities": len(mapping),
            "matched": matched, "api_calls": 1}
