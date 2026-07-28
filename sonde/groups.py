"""Groups — overlapping categories for the people worth categorising.

Applied to the top 500 by influence plus every verified follower. Membership is
many-to-many on purpose: a journalist who writes a newsletter is both.

Evidence is tiered, cheapest and strongest first. Every membership records which
tier decided it, what the evidence was, and a confidence — so a wrong answer can
be argued with rather than merely deleted.

  T1 affiliation  an org we already resolved implies the group   very high
  T2 wikidata     P106 occupation / P39 position                 very high
  T3 domain       .edu, substack.com, .gov from link signals     high
  T4 text         bio and recent post text                       moderate
  T5 propagation  overlap with a group's seeds in the follow graph  moderate
  T6 manual       a human said so                                 exact

T4 reuses M6a's rejection rules, because "I used to be a journalist" and "I read
the NYT" are not job descriptions.

T5 exists because rules cannot find "civic tech" or "privacy activist". Those
are communities, not job titles, and they are legible in who follows whom long
before they are legible in a bio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Confidence by tier.
AFFILIATION = 0.95
WIKIDATA = 0.95
DOMAIN = 0.9
TEXT = 0.65
PROPAGATION = 0.5

STUDIO = 0.9

# Named game studios. A studio name in a bio is a checkable fact rather than a
# self-description, which is why it scores above ordinary role text.
#
# Excludes studios whose names are ordinary English. Measured on the live list:
# "Valve" matched a digital-ethics consultant and "Telltale" matched a comic
# studio's "telltale" — both at 0.9 confidence. Rare, King, DICE and Sega are
# left out for the same reason. A studio that cannot be named unambiguously is
# left to the role patterns rather than allowed to poison an evidence tier.
STUDIOS: list[str] = [
    "Firaxis", "Bungie", "Riot Games", "Blizzard", "Naughty Dog",
    "Insomniac Games", "Double Fine", "Klei", "Failbetter", "inkle",
    "Obsidian Entertainment", "Larian", "Ubisoft", "BioWare", "Arkane",
    "Respawn", "Guerrilla Games", "Supergiant", "thatgamecompany",
    "Annapurna Interactive", "Devolver", "Rockstar Games", "Electronic Arts",
    "Nintendo", "PlayStation", "Xbox Game Studios", "id Software", "Mojang",
    "Epic Games", "Roblox", "Niantic", "Media Molecule",
    "The Chinese Room", "Hello Games", "Team17", "Paradox Interactive",
    "CD Projekt", "Remedy Entertainment", "IO Interactive",
    "Crystal Dynamics", "Square Enix", "Capcom", "Konami", "Bandai Namco",
    "FromSoftware", "PlatinumGames", "Infinity Ward", "343 Industries",
    "Playground Games", "Creative Assembly", "Sports Interactive",
    "Codemasters", "Frontier Developments", "Jagex", "Zynga", "Bethesda",
]

# A studio name only counts as employment when introduced like employment.
# Without this, "Into Marvel, Riot Games properties" (a fan), "Partnered with
# Epic Games" (a creator programme) and a bare "PlayStation" in a list of
# hobbies all scored 0.9. Deliberately only `at` and `@` — "with" readmitted
# the Epic Games partnership.
EMPLOYED_AT = r"(?:\bat\b|@)[^.|•\n]{0,20}?"

# Seeded definitions. Editable, and the aliases matter more than the name.
GROUPS: list[dict] = [
    {"slug": "journalists", "name": "Journalists",
     "occupations": ["journalist", "reporter", "news presenter", "columnist",
                     "war correspondent", "editor", "photojournalist"],
     "roles": [r"\b(journalist|reporter|correspondent|columnist|news editor|"
               r"editor[- ]in[- ]chief|bureau chief|staff writer)\b"],
     "org_kinds": ["news"]},
    {"slug": "writers", "name": "Writers",
     "occupations": ["writer", "author", "essayist", "non-fiction writer", "poet"],
     "roles": [r"\b(writer|author|essayist|poet)\b"]},
    {"slug": "novelists", "name": "Novelists",
     "occupations": ["novelist", "science fiction writer", "children's writer",
                     "screenwriter", "playwright", "comics writer"],
     "roles": [r"\b(novelist|sci[- ]?fi author|fiction writer|my (?:new )?novel)\b"]},
    {"slug": "newsletter-writers", "name": "Newsletter writers",
     "link_kinds": ["newsletter"],
     "roles": [r"\b(newsletter|substack|buttondown|beehiiv)\b"]},
    {"slug": "technologists", "name": "Technologists",
     "occupations": ["computer scientist", "engineer", "software engineer",
                     "programmer", "systems architect", "researcher"],
     "roles": [r"\b(technolog(?:y|ist)|cto|chief technolog\w+|infosec|"
               r"security research\w+|sre|devops)\b"]},
    {"slug": "developers", "name": "Developers",
     "occupations": ["programmer", "software developer", "software engineer"],
     "link_kinds": ["code"],
     "roles": [r"\b(developer|software engineer|programmer|coder|"
               r"full[- ]stack|front[- ]?end|back[- ]?end|ios dev\w*|rustacean)\b"]},
    {"slug": "designers", "name": "Designers",
     "occupations": ["designer", "graphic designer", "industrial designer",
                     "type designer", "illustrator"],
     "roles": [r"\b(designer|design lead|ux|ui|product design|typograph\w+|"
               r"illustrator|art director)\b"]},
    {"slug": "academics", "name": "Academics",
     "occupations": ["university teacher", "professor", "researcher",
                     "historian", "sociologist", "economist", "scientist"],
     "link_kinds": ["academic"],
     "roles": [r"\b(professor|lecturer|reader|phd|postdoc\w*|research fellow|"
               r"academic|assistant professor|associate professor|dean)\b"],
     "org_kinds": ["academic"]},
    {"slug": "politicians", "name": "Politicians",
     "occupations": ["politician", "diplomat", "civil servant"],
     "positions": ["member of", "senator", "representative", "councillor",
                   "mayor", "minister", "commissioner"],
     "roles": [r"\b(mp\b|senator|congress\w+|councill?or|mayor|candidate for|"
               r"running for|city council|state rep\w*|minister)\b"]},
    {"slug": "civic-tech", "name": "Civic tech",
     "link_kinds": ["government"],
     "roles": [r"\b(civic tech|govtech|gov\.uk|usds|18f|code for america|"
               r"public interest tech|digital service|open government)\b"]},
    {"slug": "privacy", "name": "Privacy & security",
     "roles": [r"\b(privacy|surveillance|encryption|digital rights|eff\b|"
               r"infosec|security research\w*|cryptograph\w+|anonymity)\b"]},
    {"slug": "apple", "name": "Apple", "org_names": ["Apple", "Apple Inc."]},
    {"slug": "google", "name": "Google",
     "org_names": ["Google", "Google LLC", "Alphabet Inc.", "DeepMind", "YouTube"]},
    {"slug": "microsoft", "name": "Microsoft",
     "org_names": ["Microsoft", "Microsoft Corporation", "GitHub", "LinkedIn"]},
    # M18. `sweep_all` because the top-500-by-influence target set finds only 11
    # of the 198 matches in the full list — see the milestone note. Every role
    # pattern here is a multi-word job title rather than a common noun, which is
    # what makes running it over 8,000 bios safe where "writer" would not be.
    {"slug": "game-industry", "name": "Game industry",
     "sweep_all": True,
     "occupations": ["video game developer", "video game designer",
                     "game designer", "game artist", "game programmer",
                     "game director"],
     "link_kinds": ["games"],
     "org_names": STUDIOS,
     "studios": STUDIOS,
     "roles": [
         r"\b(?:game\s?dev(?:eloper|elopment)?s?|gamedev)\b",
         r"\bgame\s(?:design(?:er|ing)?|writer|artist|producer|programmer|"
         r"director|audio|composer|studio|engineer)\b",
         # No `systems` here. "Systems design" is service- and UX-design
         # vocabulary: it matched six people on the live list, among them
         # "Design systems designer" and "Service & Systems Design".
         r"\b(?:narrative|level|combat|encounter|quest)\sdesign(?:er|ing)?\b",
         r"\bnarrative\sdirector\b",
         r"\btechnical\sartist\b",
         r"\bgames?\s(?:journalis\w+|critic\w*)\b",
         r"\bindie\sgames?\sdev\w*\b",
         r"\b(?:unity3d|unity\sengine|unreal\sengine|godot)\b",
     ]},
]

BY_SLUG = {g["slug"]: g for g in GROUPS}

# Same discipline as institution matching: a past or consuming mention is not a
# job description.
NEGATIVE = re.compile(
    r"\b(ex[-\s]|former(?:ly)?|previous(?:ly)?|retired|aspiring|wannabe|"
    r"used to be|recovering|not a|hobbyist|amateur)\s*$", re.I)


@dataclass
class Membership:
    slug: str
    tier: str
    confidence: float
    evidence: str
    source_url: str | None = None


def _term_in(term: str, occupation: str) -> bool:
    """Is `term` a whole-word part of `occupation`?

    "researcher" is in "artificial intelligence researcher"; "editor" is not in
    "editorial assistant", which is why this is word-bounded rather than a
    plain substring test.
    """
    return re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)",
                     occupation.lower()) is not None


def _matches_role(patterns: list[str], text: str | None) -> str | None:
    """A role phrase in text, unless it is negated just before."""
    if not text:
        return None
    for pattern in patterns:
        for found in re.finditer(pattern, text, re.I):
            before = text[max(0, found.start() - 24):found.start()]
            if NEGATIVE.search(before):
                continue
            return found.group(0)
    return None


# The role rule requires a negation immediately before the match. Studio names
# are listed differently — "Former art daddy at Obsidian Entertainment" and
# "Previously: Adobe/Substance, Rockstar Games" both put words in between, and
# both were credited as current employers at 0.9 until this existed.
NEGATIVE_ANYWHERE = re.compile(
    r"\b(ex|former(?:ly)?|previous(?:ly)?|retired|alum(?:ni|nus)?|prev)\b", re.I)
# ...but a sentence boundary ends the run, so "ex-Twitter. Now at Bungie" is not
# negated by the "ex-".
BOUNDARY = re.compile(r"[.|•\n]")


def _negated_before(text: str, start: int, window: int = 56) -> bool:
    before = text[max(0, start - window):start]
    last = None
    for match in BOUNDARY.finditer(before):
        last = match
    if last is not None:
        before = before[last.end():]
    return NEGATIVE_ANYWHERE.search(before) is not None


def _matches_studio(studios: list[str], text: str | None) -> str | None:
    """A named studio introduced as an employer, and not in the past tense."""
    if not text:
        return None
    for studio in studios:
        pattern = rf"{EMPLOYED_AT}(?<!\w){re.escape(studio)}(?!\w)"
        for found in re.finditer(pattern, text, re.I):
            if _negated_before(text, found.start()):
                continue
            return studio
    return None


def classify(actor: dict) -> list[Membership]:
    """All groups an actor belongs to, with the evidence for each.

    `actor` carries the already-stored fields: affiliations, wikidata
    occupations and positions, link signals, description, and post text.
    """
    out: dict[str, Membership] = {}

    def add(slug: str, tier: str, confidence: float, evidence: str,
            source_url: str | None = None) -> None:
        existing = out.get(slug)
        if existing is None or confidence > existing.confidence:
            out[slug] = Membership(slug, tier, confidence, evidence, source_url)

    affiliations = actor.get("affiliations") or []
    occupations = [o.lower() for o in actor.get("wikidata_occupations") or []]
    positions = [p.lower() for p in actor.get("wikidata_positions") or []]
    link_kinds = {s.get("kind") for s in actor.get("link_signals") or []}
    text = " ".join(filter(None, [
        actor.get("description") or "",
        " ".join(actor.get("post_texts") or []),
    ]))

    for group in GROUPS:
        slug = group["slug"]

        # T1 — an organisation we already resolved. A *former* affiliation is
        # explicitly not membership: Meredith Whittaker left Google in 2019 and
        # belongs in no Google group, however long Wikidata keeps the statement.
        for aff in affiliations:
            if aff.get("kind") == "former":
                continue
            name = (aff.get("org_name") or "").lower()
            if any(name == want.lower() for want in group.get("org_names", [])):
                add(slug, "affiliation", AFFILIATION,
                    f"{aff.get('kind', 'affiliated')} at {aff['org_name']}",
                    aff.get("source_url"))
            if aff.get("org_kind") in group.get("org_kinds", []):
                add(slug, "affiliation", AFFILIATION,
                    f"{aff.get('kind', 'affiliated')} at {aff['org_name']}",
                    aff.get("source_url"))

        # T2 — Wikidata occupation and position. Matched on whole words inside
        # the occupation rather than by equality: Wikidata says "artificial
        # intelligence researcher" and "site reliability engineer", so exact
        # matching missed researchers and engineers entirely.
        for occupation in group.get("occupations", []):
            hit = next((o for o in occupations if _term_in(occupation, o)), None)
            if hit:
                add(slug, "wikidata", WIKIDATA, f"Wikidata occupation: {hit}")
        for position in group.get("positions", []):
            if any(position.lower() in p for p in positions):
                add(slug, "wikidata", WIKIDATA,
                    f"Wikidata position: {position}")

        # T3 — what their own links point at.
        for kind in group.get("link_kinds", []):
            if kind in link_kinds:
                add(slug, "domain", DOMAIN, f"self-declared {kind} link")

        # T4a — a named employer in the bio. Above plain role text because a
        # studio name is a checkable fact, not a self-description.
        named = _matches_studio(group.get("studios", []), text)
        if named:
            add(slug, "studio", STUDIO, f"names {named} in bio or recent posts")

        # T4b — bio and post text.
        found = _matches_role(group.get("roles", []), text)
        if found:
            add(slug, "text", TEXT, f"matched {found!r} in bio or recent posts")

    return list(out.values())


# Groups whose rules are precise enough to run over every follower rather than
# only the top-N target set. Opting in is a measured decision per group, not a
# default: "writer" matches 626 bios and would make the target set meaningless,
# while "narrative designer" matches people who are one.
SWEEP_GROUPS = [g for g in GROUPS if g.get("sweep_all")]


def sweep_match(text: str | None) -> tuple[str, str] | None:
    """Evidence strong enough to pull someone into the grouping set at all.

    Returns `(slug, evidence)`, or None. This is the widening rule for M18: the
    top 500 by influence plus every verified follower contained just 11 of the
    198 game-industry accounts, because studio staff are not prominent the way
    journalists are and every influence component reads reach.
    """
    if not text:
        return None
    for group in SWEEP_GROUPS:
        named = _matches_studio(group.get("studios", []), text)
        if named:
            return group["slug"], f"names {named}"
        found = _matches_role(group.get("roles", []), text)
        if found:
            return group["slug"], f"matched {found!r}"
    return None
