"""M18 — the game industry group, and the target-set widening it needed.

Every fixture bio here is a real one from the follower list, abbreviated. The
false-positive tests are the point: a validation pass over 10,041 followers
produced 105 members of which 18 were wrong, and each of those became a case
below.
"""

from __future__ import annotations

import pytest

from sonde.groups import BY_SLUG, classify, sweep_match


def member(description: str, **extra) -> str | None:
    """The tier this bio earns in game-industry, or None."""
    actor = {"description": description, "post_texts": [], "affiliations": [],
             "wikidata_occupations": [], "wikidata_positions": [],
             "link_signals": []}
    actor.update(extra)
    hits = [m for m in classify(actor) if m.slug == "game-industry"]
    return hits[0].tier if hits else None


def test_the_group_exists_and_is_named_game_industry():
    assert BY_SLUG["game-industry"]["name"] == "Game industry"


# ------------------------------------------------- who must be in

@pytest.mark.parametrize("bio,expected", [
    ("narrative director at Firaxis. emergent/systemic storytelling.", "studio"),
    ("Union Organizer & Game Producer on Diablo IV at Blizzard.", "studio"),
    ("they/them | localization writer at nintendo, opinions my own", "studio"),
    ("principal ux @ Riot Games.", "studio"),
    ("Senior Writer @ Failbetter Games, working on Mandrake", "studio"),
    ("Innovation and Engineering leadership @ SIEA/PlayStation", "studio"),
    ("Narrative Designer and Game Writer looking for work.", "text"),
    ("17 year QA game dev veteran.", "text"),
    ("Video game producer with a passion for art history", "text"),
    ("Lecturer in Game Design | Technical Artist", "text"),
])
def test_practitioners_are_in(bio, expected):
    assert member(bio) == expected


def test_the_worked_example():
    """Cat Manning, the account the request named."""
    assert member("narrative director at Firaxis. emergent/systemic "
                  "storytelling. nominated for awards and things. lit PhD.")


# ------------------------------------------------- who must be out

@pytest.mark.parametrize("bio", [
    # Fandom. 164 people on the live list matched vocabulary like this, against
    # 92 practitioners — which is why none of it is a tier.
    "1980. He/him. NoVA/DC area. I read a lot. I play videogames.",
    "Colorado Lawyer. Gamer. Dad. He/Him/His.",
    "Just a disabled geek and board game hoarder, I love movies, and TTRPG",
    "games, video games. famed quality of life enjoyer.",
    "AMAB, ENBY, Progressive, Gamer, Sci Fi lover, Grandparent.",
    "Streamer • NSFW • She/her • gamer girl",
])
def test_playing_games_is_not_working_in_games(bio):
    assert member(bio) is None


@pytest.mark.parametrize("bio,why", [
    ("Watcher of Sports, Player of Games. Victory, Carlton, PlayStation",
     "a studio named as a hobby, not an employer"),
    ("Into Marvel, Riot Games properties and comics",
     "a fan of the studio's output"),
    ("Basher of Nazis | Partnered with Epic Games | he/him",
     "a creator programme is not employment"),
    ("Level 38 Lover and maker of things. Former art daddy at Obsidian "
     "Entertainment",
     "a past employer, with words in between"),
    ("UX Design Lead @ Launchpad Previously: Adobe/Substance, Rockstar Games",
     "a past employer in a run-on list"),
])
def test_a_studio_name_alone_is_not_employment(bio, why):
    assert member(bio) is None, why


@pytest.mark.parametrize("bio", [
    "#design #web #frontend & #designsystems Design systems designer",
    "Award winning bog hag, platform and systems designer in tech",
    "Design Leader: Service & Systems Design",
    "Systems designer, translator, bridge builder.",
])
def test_systems_design_is_not_game_design(bio):
    """Six live false positives. 'Systems design' is service- and UX-design."""
    assert member(bio) is None


@pytest.mark.parametrize("bio", [
    "At work I'm a UX Architect. My recreational hats: Hobbyist Game Developer",
    "amateur game designer, mostly weekends",
])
def test_hobbyists_are_not_the_industry(bio):
    assert member(bio) is None


def test_common_word_studios_are_not_in_the_list():
    """'Valve' matched a digital-ethics consultant; 'Telltale' a comic studio."""
    studios = {s.lower() for s in BY_SLUG["game-industry"]["studios"]}
    assert "valve" not in studios
    assert "telltale" not in studios


def test_unity_is_never_bare():
    """All 14 live bios containing 'unity' meant community or impunity."""
    assert member("I do all in my power to aid any community I'm a part of") is None
    assert member("what can be done with impunity to the least powerful") is None
    assert member("gameplay programmer, unity engine, 6 years") == "text"


def test_a_sentence_boundary_ends_a_negation_run():
    """'ex-Twitter. Now at Bungie' is a current job, not a former one."""
    assert member("ex-Twitter. Now at Bungie shipping things") == "studio"


# ------------------------------------------------- the widening

def test_sweep_match_finds_practitioners():
    assert sweep_match("narrative director at Firaxis")[0] == "game-industry"
    assert sweep_match("Senior Narrative Designer, freelance")[0] == "game-industry"


def test_sweep_match_ignores_fans_and_empty_bios():
    assert sweep_match("I read a lot. I play videogames.") is None
    assert sweep_match("Design systems designer") is None
    assert sweep_match(None) is None
    assert sweep_match("") is None


def test_only_precise_groups_opt_into_the_sweep():
    """'writer' matches 626 of 7,976 bios and would make the target set moot."""
    from sonde.groups import GROUPS, SWEEP_GROUPS

    assert [g["slug"] for g in SWEEP_GROUPS] == ["game-industry"]
    assert len(SWEEP_GROUPS) < len(GROUPS)
    assert sweep_match("Writer. Essayist. Dad.") is None


def test_link_and_wikidata_tiers():
    assert member("Solo game dev", link_signals=[{"kind": "games"}]) == "domain"
    assert member("I make things",
                  wikidata_occupations=["video game developer"]) == "wikidata"
