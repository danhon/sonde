"""M7 — external reputation from Wikidata and Wikipedia."""

import httpx
import pytest

from sonde.db import store
from sonde.external import wikidata, wikipedia
from tests.fakes import actor


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    store.set_db_path(str(tmp_path / "ext.db"))
    await store.connect()
    yield
    await store.close()
    store.set_db_path(None)


async def follower(did: str, handle: str, rank: int = 0) -> None:
    await store.upsert_actor(actor(did, handle=handle))
    await store.mark_seen(did, rank)


def binding(**kw):
    return {k: {"value": str(v)} for k, v in kw.items()}


def fake_sparql(mapping_rows, detail_rows=None):
    calls = []

    async def query(q):
        calls.append(q)
        if "P12361" in q:
            return mapping_rows
        return detail_rows or []

    query.calls = calls
    return query


# --------------------------------------------------------- the join

async def test_the_bulk_mapping_joins_by_handle():
    await follower("did:plc:nk", "naomiaklein.bsky.social")
    await follower("did:plc:other", "someone.bsky.social")

    query = fake_sparql([
        binding(handle="naomiaklein.bsky.social",
                item="http://www.wikidata.org/entity/Q234110",
                itemLabel="Naomi Klein", sitelinks=67),
        binding(handle="notafollower.bsky.social",
                item="http://www.wikidata.org/entity/Q999", itemLabel="Someone", sitelinks=3),
    ])
    result = await wikidata.refresh(fetch=query)

    assert result["entities"] == 2
    assert result["matched"] == 1, "only followers are joined"
    detail = await store.follower_detail("did:plc:nk")
    assert detail["wikidata_id"] == "Q234110"
    assert detail["wikidata_sitelinks"] == 67


async def test_handles_match_case_insensitively():
    await follower("did:plc:x", "MixedCase.bsky.social")
    query = fake_sparql([binding(handle="@mixedcase.bsky.social",
                                 item="http://www.wikidata.org/entity/Q1",
                                 itemLabel="X", sitelinks=2)])
    assert (await wikidata.refresh(fetch=query))["matched"] == 1


async def test_detail_is_requested_only_for_matched_followers():
    """The combined query 504s over 10,500 entities; only ~1% are followers."""
    await follower("did:plc:a", "a.bsky.social")
    query = fake_sparql(
        [binding(handle="a.bsky.social", item="http://www.wikidata.org/entity/Q1",
                 itemLabel="A", sitelinks=5),
         binding(handle="b.bsky.social", item="http://www.wikidata.org/entity/Q2",
                 itemLabel="B", sitelinks=5)],
        [binding(item="http://www.wikidata.org/entity/Q1",
                 occupations="journalist|author", employers="The Guardian")],
    )
    await wikidata.refresh(fetch=query)

    detail_query = [q for q in query.calls if "VALUES" in q][0]
    assert "wd:Q1" in detail_query
    assert "wd:Q2" not in detail_query, "non-followers must not bloat the query"

    person = await store.follower_detail("did:plc:a")
    assert person["wikidata_occupations"] == ["author", "journalist"]
    assert person["wikidata_employers"] == ["The Guardian"]


async def test_a_missing_sitelink_count_does_not_crash():
    await follower("did:plc:x", "x.bsky.social")
    query = fake_sparql([{"handle": {"value": "x.bsky.social"},
                          "item": {"value": "http://www.wikidata.org/entity/Q1"}}])
    assert (await wikidata.refresh(fetch=query))["matched"] == 1
    assert (await store.follower_detail("did:plc:x"))["wikidata_sitelinks"] == 0


async def test_matching_activates_the_public_profile_component():
    """12 points were dormant while nothing populated these columns."""
    await follower("did:plc:x", "x.bsky.social")
    assert "public_profile" not in await store.active_score_components()

    query = fake_sparql([binding(handle="x.bsky.social",
                                 item="http://www.wikidata.org/entity/Q1",
                                 itemLabel="X", sitelinks=12)])
    await wikidata.refresh(fetch=query)

    assert "public_profile" in await store.active_score_components()


# ---------------------------------------------------------- pageviews

def views_transport(pages: dict[str, int]):
    def handler(request: httpx.Request) -> httpx.Response:
        title = request.url.path.split("/user/")[1].split("/daily/")[0]
        if title not in pages:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={
            "items": [{"views": pages[title] // 2}, {"views": pages[title] - pages[title] // 2}]
        })

    return httpx.MockTransport(handler)


async def prepare(did: str, handle: str, title: str, sitelinks: int = 10) -> None:
    await follower(did, handle)
    db = await store._db()
    await db.execute(
        "UPDATE actors SET wikipedia_title = ?, wikidata_id = 'Q1', "
        "wikidata_sitelinks = ? WHERE did = ?", (title, sitelinks, did),
    )
    await store.commit()


async def test_pageviews_are_summed_over_the_window():
    await prepare("did:plc:nk", "nk.bsky.social", "Naomi Klein")
    result = await wikipedia.refresh(transport=views_transport({"Naomi_Klein": 29_920}))

    assert result["fetched"] == 1
    assert (await store.follower_detail("did:plc:nk"))["wikipedia_views_30d"] == 29_920


async def test_pageviews_have_their_own_clock():
    """Sharing external_fetched_at with the Wikidata join meant the join always
    looked fresh, so pageviews never ran at all."""
    await prepare("did:plc:a", "a.bsky.social", "A")
    db = await store._db()
    await db.execute("UPDATE actors SET external_fetched_at = ? WHERE did = 'did:plc:a'",
                     (store.utcnow(),))
    await store.commit()

    assert len(await store.pageview_targets()) == 1, "a fresh join must not block views"


async def test_an_absent_article_is_not_retried_every_cycle():
    await prepare("did:plc:ghost", "ghost.bsky.social", "No Such Page")
    result = await wikipedia.refresh(transport=views_transport({}))

    assert result["no_article"] == 1
    assert await store.pageview_targets() == [], "stamped, so it stops being a target"


async def test_entities_with_no_wikipedia_article_are_skipped():
    """0 sitelinks means no article anywhere; asking would 404 by construction."""
    await prepare("did:plc:x", "x.bsky.social", "X", sitelinks=0)
    assert await store.pageview_targets() == []


async def test_a_failing_lookup_does_not_abort_the_run():
    await prepare("did:plc:ok", "ok.bsky.social", "OK")
    await prepare("did:plc:bad", "bad.bsky.social", "Bad", sitelinks=5)

    def handler(request):
        if "Bad" in str(request.url):
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"items": [{"views": 10}]})

    result = await wikipedia.refresh(transport=httpx.MockTransport(handler))
    assert result["status"] == "ok"
    assert result["fetched"] == 1
    assert result["failed"] == 1
