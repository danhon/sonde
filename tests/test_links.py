"""Link-derived signals — what a bio URL points at, with no network calls."""

import pytest

from sonde.db import store
from sonde.external.links import classify, extract_urls, host_of, signals_for
from tests.fakes import actor


def test_urls_are_extracted_and_deduplicated():
    urls = extract_urls("see https://a.example and https://a.example again, plus http://b.example.")
    assert urls == ["https://a.example", "http://b.example"]


def test_trailing_punctuation_is_not_part_of_the_url():
    assert extract_urls("read https://example.com/post.") == ["https://example.com/post"]


def test_no_urls_is_not_an_error():
    assert extract_urls(None) == [] and extract_urls("no links here") == []


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://foo.substack.com", "newsletter"),
        ("https://buttondown.com/someone", "newsletter"),
        ("https://github.com/someone", "code"),
        ("https://cs.stanford.edu/~x", "academic"),
        ("https://www.gov.uk/thing", "government"),
        ("https://eff.org", "organisation"),
        ("https://patreon.com/x", "supported"),
    ],
)
def test_platforms_and_suffixes_are_recognised(url, kind):
    assert classify(url)["kind"] == kind


def test_aggregators_and_shorteners_say_nothing():
    """A linktr.ee hides the destination, so it is not evidence of anything."""
    for url in ("https://linktr.ee/x", "https://bit.ly/abc", "https://bsky.app/profile/x"):
        assert classify(url) is None


def test_www_is_stripped_when_matching_hosts():
    assert host_of("https://www.github.com/x") == "github.com"
    assert classify("https://www.substack.com/@x")["kind"] == "newsletter"


def test_an_unknown_domain_is_a_weak_site_signal():
    signal = classify("https://danhon.com")
    assert signal["kind"] == "site"
    assert "danhon.com" in signal["note"]


def test_signals_are_deduplicated_by_host():
    signals = signals_for("https://github.com/a and https://github.com/b")
    assert len(signals) == 1


def test_a_self_declared_handle_domain_counts():
    """tomhannen.ft.com says more than any bio link; x.bsky.social says nothing."""
    academic = signals_for(None, handle="someone.cam.ac.uk")
    assert any(s["kind"] == "academic" for s in academic)
    assert signals_for(None, handle="someone.bsky.social") == []


@pytest.fixture
async def db(tmp_path):
    store.set_db_path(str(tmp_path / "links.db"))
    await store.connect()
    yield store
    await store.close()
    store.set_db_path(None)


async def test_signals_are_derived_without_network_calls(db):
    profile = actor("did:plc:n", handle="writer.example")
    profile["description"] = "I write https://foo.substack.com weekly"
    await store.upsert_actor(profile)
    await store.mark_seen("did:plc:n", 0)

    result = await store.refresh_link_signals()

    assert result["with_signals"] == 1
    assert result["by_kind"]["newsletter"] == 1
    detail = await store.follower_detail("did:plc:n")
    assert detail["link_signals"][0]["kind"] == "newsletter"


async def test_rescanning_replaces_rather_than_accumulates(db):
    profile = actor("did:plc:n", handle="x.bsky.social")
    profile["description"] = "https://foo.substack.com"
    await store.upsert_actor(profile)
    await store.mark_seen("did:plc:n", 0)
    await store.refresh_link_signals()

    profile["description"] = "moved on, nothing here"
    await store.upsert_actor(profile)
    await store.refresh_link_signals()

    assert (await store.follower_detail("did:plc:n"))["link_signals"] == []
