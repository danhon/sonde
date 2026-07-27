"""Institutional affiliation — where a follower works.

Bluesky exposes no employer field, so affiliation is inferred from four paths
ranked by what they actually prove. Measured on 2026-07-26: only 14 of 147
verified followers (10%) were verified BY an institution; the other 134 were
verified by Bluesky itself, which says nothing about employment. So bio text
carries most of the weight, and its confidence has to be conditional.

Full reasoning in SCORING.md#institution.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("sonde.institutions")

# Confidence by evidence path. Best evidence wins; they do not accumulate.
ATTESTED = 1.0    # verification issuer IS the institution
DOMAIN = 0.95     # handle is a subdomain of the institution
ROSTER = 0.95     # listed in the institution's verification records
CORROBORATED = 0.95   # bio claim + an independent external source agree
CLAIMED_VERIFIED = 0.85   # bio claim, account is verified
CLAIMED_PLAIN = 0.40      # bio claim, account is not verified

# Seniority multipliers. No match defaults to staff level — an absent job title
# is not evidence of juniority.
SENIORITY = [
    (1.0, r"\b(columnist|editor[- ]in[- ]chief|executive editor|managing editor|"
          r"op[- ]?ed|bureau chief|founder|co[- ]?founder|professor|director|"
          r"editor at large|chief \w+ officer|principal|partner)\b"),
    (0.85, r"\b(reporter|correspondent|journalist|writer|producer|engineer|"
           r"analyst|researcher|scientist|designer|developer|editor|lecturer|"
           r"staff)\b"),
    (0.6, r"\b(intern|fellow|contributor|freelance|freelancer|student|"
          r"candidate|apprentice)\b"),
]
DEFAULT_SENIORITY = 0.85

# Seeded so the first run has something to match. Everything else is discovered
# from verification issuers seen during sweeps.
SEED_INSTITUTIONS: list[dict] = [
    {"name": "The New York Times", "weight": 1.0, "domains": ["nytimes.com", "nyt.com"],
     "aliases": ["New York Times", "NYT", "N.Y. Times", "NYTimes"]},
    {"name": "The Washington Post", "weight": 1.0, "domains": ["washingtonpost.com", "wapo.st"],
     "aliases": ["Washington Post", "WaPo"]},
    {"name": "Financial Times", "weight": 1.0, "domains": ["ft.com", "financialtimes.com"],
     "aliases": ["Financial Times", "the FT"]},
    {"name": "Wired", "weight": 0.95, "domains": ["wired.com"], "aliases": ["WIRED"]},
    {"name": "The Guardian", "weight": 1.0, "domains": ["theguardian.com", "guardian.co.uk"],
     "aliases": ["The Guardian", "Guardian US", "Guardian UK"]},
    {"name": "NBC News", "weight": 0.95, "domains": ["nbcnews.com"], "aliases": ["NBC News"]},
    {"name": "The Atlantic", "weight": 0.95, "domains": ["theatlantic.com"],
     "aliases": ["The Atlantic"]},
    {"name": "Reuters", "weight": 0.95, "domains": ["reuters.com"], "aliases": ["Reuters"]},
    {"name": "Associated Press", "weight": 0.95, "domains": ["ap.org"],
     "aliases": ["Associated Press", "the AP"]},
    {"name": "BBC", "weight": 1.0, "domains": ["bbc.co.uk", "bbc.com"],
     "aliases": ["BBC News", "the BBC"]},
    {"name": "404 Media", "weight": 0.85, "domains": ["404media.co"], "aliases": ["404 Media"]},
    {"name": "The Verge", "weight": 0.9, "domains": ["theverge.com"], "aliases": ["The Verge"]},
    {"name": "Bloomberg", "weight": 0.95, "domains": ["bloomberg.com"], "aliases": ["Bloomberg"]},
    {"name": "ProPublica", "weight": 0.95, "domains": ["propublica.org"], "aliases": ["ProPublica"]},
    {"name": "MSNBC", "weight": 0.9, "domains": ["msnbc.com", "ms.now"], "aliases": ["MSNBC"]},
]


@dataclass
class Match:
    institution_id: int | None
    name: str
    weight: float
    confidence: float
    method: str
    seniority: float = DEFAULT_SENIORITY
    role: str | None = None

    @property
    def score(self) -> float:
        return self.weight * self.confidence * self.seniority


def seniority_of(text: str | None) -> tuple[float, str | None]:
    """Multiplier from a bio. 'NYT columnist' and 'NYT intern' shouldn't tie."""
    if not text:
        return DEFAULT_SENIORITY, None
    lowered = text.lower()
    for multiplier, pattern in SENIORITY:
        found = re.search(pattern, lowered)
        if found:
            return multiplier, found.group(0)
    return DEFAULT_SENIORITY, None


def _handle_matches_domain(handle: str | None, domains: list[str]) -> str | None:
    """True for `tomhannen.ft.com` and for `ft.com` itself, not for `notft.com`."""
    if not handle:
        return None
    handle = handle.lower().strip(".")
    for domain in domains:
        domain = domain.lower()
        if handle == domain or handle.endswith("." + domain):
            return domain
    return None


# A bare substring match is not an employment claim. Inspecting the first real
# run turned up three distinct false-positive classes, all common:
#
#   past employment  "Ex-Microsoft", "Previously at BBC R&D", "20+ yrs at Apple"
#   products         "NYT bestselling writer", "better than NYT Wordle"
#   bare mentions    "borrowing from the BBC's Genome project"
#
# So a claim needs employment context before it counts, and either of the other
# two patterns disqualifies it.

# Looks backward from the alias. Sentence enders bound the window, so a claim in
# a later clause isn't tainted by an earlier one.
PAST_EMPLOYMENT = re.compile(
    r"(?:\bex[-\s]|\bformer(?:ly)?\b|\bprevious(?:ly)?\b|\balum\w*\b|\bretired\b|"
    r"\bwas\b|\byrs?\s+at\b|\byears?\s+at\b|\bpast\b|"
    r"\bused\s+to\s+(?:work|write|be|report)\w*\b)[^.;|]{0,40}$"
)

# "Ex-WIRED, now a reporter at the NYT" — a present-tense marker after the past
# one means the past marker belongs to an earlier employer, not this alias.
PRESENT_OVERRIDE = re.compile(r"\b(?:now|currently|presently|these days|today)\b")

# Consuming an outlet is not working for one — "avid listener of the BBC".
CONSUMPTION_BEFORE = re.compile(
    r"\b(?:listener|viewer|reader|fan|subscriber|watching|listening|reading|"
    r"binge\w*)\b[^.;|]{0,25}$"
)

# Looks forward from the alias — the institution as a product, not an employer.
PRODUCT_CONTEXT = re.compile(
    r"^\W{0,3}(?:best[-\s]?sell\w*|bestseller|crossword|wordle|connections|"
    r"spelling bee|games|puzzle|subscri\w*|reader|subscriber|app\b|genome)"
)

# Looks backward — an employment preposition just before the alias, allowing an
# article ("editor at the BBC").
EMPLOYMENT_BEFORE = re.compile(
    r"(?:\bat\b|@|\bfor\b|\bwith\b|\bof\b|\bjoin\w*\b|\bwork\w*\s+(?:at|for))"
    r"\s*(?:the\s+)?\W{0,3}$"
)

# Looks forward — a role immediately after the alias ("NYT columnist").
ROLE_AFTER = re.compile(
    r"^\W{0,3}(?:columnist|reporter|editor|writer|correspondent|journalist|"
    r"producer|engineer|analyst|researcher|scientist|designer|developer|"
    r"staff|contributor|newsroom|opinion|graphics|investigations|"
    r"intern|fellow|freelance\w*|photographer|editor[- ]in[- ]chief)\b"
)


def _alias_in_bio(description: str | None, aliases: list[str]) -> str | None:
    """Return the alias only if the bio reads as a *current* employment claim."""
    if not description:
        return None
    lowered = description.lower()
    for alias in aliases:
        # Word-boundary match so "AP" doesn't hit inside "apple".
        for found in re.finditer(rf"(?<![\w]){re.escape(alias.lower())}(?![\w])", lowered):
            before, after = lowered[: found.start()], lowered[found.end():]
            past = PAST_EMPLOYMENT.search(before)
            if past and not PRESENT_OVERRIDE.search(before[past.start():]):
                continue          # they used to work there
            if PRODUCT_CONTEXT.match(after):
                continue          # NYT bestseller, BBC Genome, the crossword
            if CONSUMPTION_BEFORE.search(before):
                continue          # a listener of the BBC, not an employee
            if EMPLOYMENT_BEFORE.search(before) or ROLE_AFTER.match(after):
                return alias
    return None


def match_actor(
    actor: dict,
    institutions: list[dict],
    *,
    roster_ids: set[int] | None = None,
    corroborated_ids: set[int] | None = None,
) -> Match | None:
    """Best-evidence institutional match for one actor, or None.

    `roster_ids` are institutions whose published verification roster contains
    this actor. `corroborated_ids` are institutions an external source (M7)
    independently associates with them.
    """
    roster_ids = roster_ids or set()
    corroborated_ids = corroborated_ids or set()
    verified = actor.get("verified_status") == "valid"
    issuers = {
        (v.get("issuerHandle") or "").lower()
        for v in actor.get("verification_records") or []
    }
    multiplier, role = seniority_of(actor.get("description"))

    best: Match | None = None

    def consider(candidate: Match) -> None:
        nonlocal best
        if best is None or candidate.score > best.score:
            best = candidate

    for inst in institutions:
        inst_id = inst.get("id")
        name = inst["name"]
        weight = float(inst.get("weight", 1.0))
        domains = inst.get("domains") or []
        aliases = (inst.get("aliases") or []) + [name]

        # 1. Attested — the institution issued the verification itself.
        if issuers & {d.lower() for d in domains}:
            consider(Match(inst_id, name, weight, ATTESTED, "attested", multiplier, role))
            continue

        # 2. Domain — the handle is theirs.
        if _handle_matches_domain(actor.get("handle"), domains):
            consider(Match(inst_id, name, weight, DOMAIN, "domain", multiplier, role))
            continue

        # 3. Roster — listed in their published verification records.
        if inst_id is not None and inst_id in roster_ids:
            consider(Match(inst_id, name, weight, ROSTER, "roster", multiplier, role))
            continue

        # 4/5. Bio claim, corroborated or not.
        if _alias_in_bio(actor.get("description"), aliases):
            if inst_id is not None and inst_id in corroborated_ids:
                confidence, method = CORROBORATED, "corroborated"
            elif verified:
                # Bluesky verification already involved an identity check, so a
                # verified account claiming an employer is unlikely to be lying.
                confidence, method = CLAIMED_VERIFIED, "claimed (verified)"
            else:
                confidence, method = CLAIMED_PLAIN, "claimed"
            consider(Match(inst_id, name, weight, confidence, method, multiplier, role))

    return best
