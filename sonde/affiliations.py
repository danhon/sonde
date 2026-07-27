"""Affiliations — who someone is connected to, and how.

The Institution component this replaces was built from a journalism-shaped
dataset: all seven verification issuers found in the follower set are news
outlets. It handled "employed by a masthead" and nothing else. Three real cases
broke it:

  Meredith Whittaker   President of Signal    -> not a news outlet, and
                                                 leadership is not employment
  Anne Helen Petersen  writes a newsletter    -> the influential thing is hers;
                                                 there is no employer to look up
  Christopher Mims     writes for the WSJ     -> plain employment, and the WSJ
                                                 was not even in the seed list

So affiliation is a *kind* as well as a name. Leading an organisation is worth
more than working at one; running your own publication is a different claim
again, and one no employer lookup will ever find.

Evidence is ranked by what it proves, and every affiliation records the method,
a confidence, and a source — so any claim can be argued with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Kinds, and what each is worth relative to plain employment. Leading an
# organisation says more about someone than being employed by it.
KIND_WEIGHT: dict[str, float] = {
    "leadership": 1.15,
    "founder": 1.1,
    "employment": 1.0,
    "own_publication": 0.95,
    "academic": 0.95,
    "board": 0.85,
    "former": 0.35,
}

# Confidence by evidence path — same ladder as M6a, extended for Wikidata.
ATTESTED = 1.0        # the organisation issued the verification itself
WIKIDATA = 0.97       # an independent, editorially reviewed source says so
DOMAIN = 0.95         # the handle is on their domain
ROSTER = 0.95         # listed in their published verification records
CORROBORATED = 0.95   # bio claim and an external source agree
CLAIMED_VERIFIED = 0.85
LINK = 0.8            # a platform link in their own bio
CLAIMED_PLAIN = 0.40

# Role phrasings that signal a *kind* rather than just seniority.
LEADERSHIP = re.compile(
    r"\b(president|chief execut\w*|ceo|cto|coo|cfo|executive director|"
    r"director of|head of|chair(?:man|woman|person)?|managing director|"
    r"editor[- ]in[- ]chief|dean|provost|principal)\b", re.I)
FOUNDER = re.compile(r"\b(co[- ]?founder|founder|founding \w+)\b", re.I)
BOARD = re.compile(r"\b(board (?:member|of directors)|trustee|advisor[y]?|non[- ]exec\w*)\b", re.I)
ACADEMIC = re.compile(
    r"\b(professor|lecturer|reader|research fellow|postdoc\w*|phd (?:student|candidate)|"
    r"assistant professor|associate professor|emerit\w+)\b", re.I)
FORMER = re.compile(r"\b(ex[-\s]|former(?:ly)?|previous(?:ly)?|retired|alum\w*)\b", re.I)


@dataclass
class Affiliation:
    org_name: str
    kind: str
    method: str
    confidence: float
    role: str | None = None
    note: str | None = None
    url: str | None = None
    source_url: str | None = None
    org_id: int | None = None

    @property
    def strength(self) -> float:
        """0–1. Multiplied by the organisation's weight when scoring."""
        return min(self.confidence * KIND_WEIGHT.get(self.kind, 1.0), 1.0)


def kind_from_role(role: str | None, default: str = "employment") -> str:
    """What sort of relationship a role phrase describes."""
    if not role:
        return default
    if FORMER.search(role):
        return "former"
    if FOUNDER.search(role):
        return "founder"
    if LEADERSHIP.search(role):
        return "leadership"
    if BOARD.search(role):
        return "board"
    if ACADEMIC.search(role):
        return "academic"
    return default


def role_near(text: str | None, org_alias: str) -> str | None:
    """Pull the role phrase sitting next to an organisation mention.

    "President of Signal" and "Signal's president" both need to yield
    "president", because the kind depends on it.
    """
    if not text:
        return None
    lowered = text.lower()
    alias = org_alias.lower()
    index = lowered.find(alias)
    if index < 0:
        return None
    before = lowered[max(0, index - 60):index]
    after = lowered[index + len(alias):index + len(alias) + 60]
    for pattern in (LEADERSHIP, FOUNDER, BOARD, ACADEMIC):
        found = pattern.search(before) or pattern.search(after)
        if found:
            return found.group(0).strip()
    # Fall back to a plain seniority word so "reporter" is still captured.
    plain = re.search(
        r"\b(columnist|reporter|editor|writer|correspondent|journalist|producer|"
        r"engineer|analyst|researcher|scientist|designer|developer|intern|fellow)\b",
        before + " " + after, re.I)
    return plain.group(0).strip() if plain else None


def from_wikidata(employers: list[str], positions: list[str],
                  qid: str | None = None) -> list[Affiliation]:
    """Employers and positions from Wikidata — independent and reviewed."""
    source = f"https://www.wikidata.org/wiki/{qid}" if qid else None
    out: list[Affiliation] = []
    for employer in employers:
        out.append(Affiliation(
            org_name=employer, kind="employment", method="wikidata",
            confidence=WIKIDATA, note=f"Wikidata records this employer",
            source_url=source,
        ))
    for position in positions:
        kind = kind_from_role(position, default="leadership")
        out.append(Affiliation(
            org_name=position, kind=kind, method="wikidata",
            confidence=WIKIDATA, role=position,
            note="Wikidata records this position", source_url=source,
        ))
    return out


def from_links(signals: list[dict]) -> list[Affiliation]:
    """A newsletter or code platform in someone's own bio is an affiliation
    with a thing they made, which no employer lookup would ever find."""
    kinds = {"newsletter": "own_publication", "writing": "own_publication",
             "code": "own_publication", "video": "own_publication",
             "music": "own_publication", "games": "own_publication",
             "streaming": "own_publication"}
    out = []
    for signal in signals:
        kind = kinds.get(signal.get("kind"))
        if not kind:
            continue
        out.append(Affiliation(
            org_name=signal["host"], kind=kind, method="link",
            confidence=LINK, note=signal.get("note"),
            url=signal.get("url"), source_url=signal.get("url"),
        ))
    return out
