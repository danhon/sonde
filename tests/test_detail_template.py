"""The profile template renders what it claims to.

Added after two copies of the Relationship section were found in `detail.html`,
one of them nested inside the "fetch posts" `<button>`. Nothing caught it,
because no test rendered a populated profile — only empty-state route smoke
tests existed. These render a real context and assert on the output.
"""

from __future__ import annotations

import re

import pytest

from sonde import attention
from sonde.config import settings
from sonde.web.app import TEMPLATES

TEMPLATE = TEMPLATES.env.get_template("detail.html")


def render(person: dict) -> str:
    base = {
        "did": "did:plc:x", "handle": "someone.bsky.social", "display_name": "Someone",
        "avatar_url": None, "description": "", "followers_count": None,
        "follows_count": None, "posts_count": None, "influence_score": None,
        "components": {}, "posts": [], "moderation_lists": [], "affiliations": [],
        "groups": [], "interactions": [], "relationship": {}, "verified_status": None,
        "verifications": [], "first_indexed_at": None, "ignored_at": None,
        "posts_fetched_at": None, "followed_at": None, "list_rank": None,
        "account_created_at": None, "last_post_at": None, "labels": [],
        "institution_name": None, "wikipedia_title": None, "link_signals": [],
        "wikidata_id": None, "wikipedia_views_30d": None, "is_mutual": False,
        "trusted_verifier_status": None, "affinity_exact": None,
        "affinity_sampled": None, "relationship_score": None,
    }
    base.update(person)
    return TEMPLATE.render(p=base, settings=settings, request=None)


def relationship_for(followers: int, follows: int, **extra) -> dict:
    scale = attention.DEFAULT_SCALE
    rel = {
        "score": attention.points(followers, follows, scale),
        "interaction_score": 0.0,
        "attention": attention.points(followers, follows, scale),
        "attention_note": attention.describe(followers, follows),
        "attention_detail": attention.explain(followers, follows, scale),
        "inbound": 0, "outbound": 0, "conversations": 0, "days_active": 0,
        "reciprocity": 0.0, "by_kind": {}, "last_at": None,
    }
    rel.update(extra)
    return rel


def test_the_relationship_section_appears_exactly_once():
    """The bug: it was rendered twice, once inside a button."""
    html = render({"relationship": relationship_for(150_735, 1_037)})
    assert html.count(">Relationship<") == 1


def test_no_section_is_nested_inside_a_button():
    html = render({"relationship": relationship_for(150_735, 1_037)})
    for match in re.finditer(r"<button\b.*?</button>", html, re.S):
        assert "<section" not in match.group(0)


def test_attention_shows_its_working():
    """Requested: the score *and* the ratio *and* how it is calculated."""
    html = render({"relationship": relationship_for(150_735, 1_037)})

    assert "Attention scarcity" in html
    assert "150,735 ÷ 1,037" in html          # the counts
    assert "145×" in html                      # the ratio
    assert "Scarcity" in html and "Standing" in html
    assert "How this is calculated" in html
    assert "0.10%" in html                     # my share of their attention


def test_attention_block_is_absent_when_there_is_no_signal():
    html = render({"relationship": relationship_for(434, 45)
                   | {"score": 12.0, "interaction_score": 12.0,
                      "inbound": 3, "outbound": 2}})
    assert "Attention scarcity" not in html
    assert ">Relationship<" in html


def test_a_silent_scarce_follower_gets_no_nonsense_reciprocity_line():
    """With zero interactions, 'mostly one-directional' would be meaningless."""
    html = render({"relationship": relationship_for(150_735, 1_037)})
    assert "one-directional" not in html
    assert "No interactions recorded yet" in html


@pytest.mark.parametrize("followers,follows", [(150_735, 1_037), (None, None)])
def test_template_renders_either_way(followers, follows):
    rel = relationship_for(followers or 0, follows or 1) if followers else {}
    assert render({"relationship": rel})
