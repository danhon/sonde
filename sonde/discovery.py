"""Finding groups nobody thought to name.

The twelve seeded groups were written by hand, which means the interesting ones
are whatever nobody thought of. Everything here proposes **candidates for
review** rather than creating groups: a group nobody wanted is worse than a
missing one, because it makes every other count untrustworthy.

Four sources, all free, all reading data already on disk:

  occupation   Wikidata occupations no group claims. Measured: blogger (9),
               orator (3), then entrepreneur, video game developer, wikipedian,
               technologist, literary critic, podcaster.
  link         Link-signal kinds with no group — organisation (22), supported (3).
  organisation Any organisation with several people is already a group.
  phrase       Bigrams common across bios and posts but absent from every
               existing definition.
"""

from __future__ import annotations

import re
from collections import Counter

MIN_MEMBERS = 2
PHRASE_MIN = 4

# Words that carry no signal in a bio. Deliberately short: the interesting
# phrases are domain words, and over-filtering hides them.
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "you", "your", "from", "are",
    "all", "not", "但", "was", "have", "has", "但是", "his", "her", "they",
    "them", "our", "out", "who", "what", "when", "how", "why", "can", "will",
    "just", "about", "into", "more", "some", "than", "then", "now", "also",
    "here", "there", "over", "very", "much", "one", "two", "new", "old",
    "get", "got", "make", "made", "like", "love", "hate", "good", "bad",
    "she", "him", "its", "it's", "i'm", "don't", "doesn't", "isn't",
    "posts", "post", "opinions", "own", "views", "mine", "he", "at", "in",
    "on", "of", "to", "a", "an", "is", "be", "by", "or", "as", "my", "we",
    "us", "me", "it", "do", "if", "no", "so", "up", "am", "via", "http",
    "https", "com", "www",
    # Added after reading what discovery actually proposed against the live
    # list: "But Only", "Does Anyone", "Every Time" and "Last Week" were all
    # offered as groups. Every word here is a pure function word — the list
    # stays deliberately short, and nothing domain-bearing goes in it, because
    # over-filtering hides the phrases worth finding.
    "but", "only", "does", "did", "doing", "done", "anyone", "everyone",
    "every", "any", "because", "been", "being", "were", "would", "could",
    "should", "still", "even", "back", "well", "want", "wants", "know",
    "knows", "think", "thinks", "really", "actually", "maybe", "probably",
    "many", "most", "other", "others", "same", "such", "these", "those",
    "their", "theirs", "yours", "ours", "which", "while", "where", "after",
    "before", "again", "always", "never", "sometimes", "often",
}

WORD = re.compile(r"[a-z][a-z'&-]{1,}", re.I)
URLS = re.compile(r"https?://\S+|\b[\w.-]+\.(?:com|org|net|social|app|io|co|uk|me|dev)\b", re.I)
# Bigrams must not span punctuation. Without this, "digital rights, human
# dignity" produced "rights human", a phrase nobody wrote.
CLAUSE = re.compile(r"[.,;:!?()\[\]{}|/\n\r•·—–\-]+")


def _clauses(text: str) -> list[list[str]]:
    """Tokens grouped by clause, with URLs removed first.

    Bio URLs otherwise dominate: 'bsky social', 'bsky app', 'mastodon social'
    and 'app profile' were the top phrase candidates on the first run, all of
    them fragments of links rather than anything anyone wrote.
    """
    cleaned = URLS.sub(" ", text or "")
    out = []
    for clause in CLAUSE.split(cleaned):
        words = [w.lower() for w in WORD.findall(clause)
                 if w.lower() not in STOPWORDS and len(w) > 2]
        if len(words) > 1:
            out.append(words)
    return out


def _tokens(text: str) -> list[str]:
    return [w for clause in _clauses(text) for w in clause]


def bigrams(text: str) -> set[str]:
    """Adjacent token pairs within one clause, after stopwords are dropped.

    The single definition of "this text contains that phrase", shared by the
    rule that proposes a phrase candidate and the rule that later finds its
    members. They used to differ, and both ways it went wrong were live:

    A bigram is built from the token stream, so "wrote a book" yields "wrote
    book" — a string nobody typed. The matcher looked for that with a literal
    LIKE and could never find it, so the candidate was proposed with 10 people
    and matched zero.

    And the proposer read bios *and* posts while the matcher read only bios, so
    "data center" — 21 people's posts, nobody's bio — proposed a group that
    would have been created empty.
    """
    return {f"{a} {b}"
            for clause in _clauses(text)
            for a, b in zip(clause, clause[1:])}


def occupation_candidates(occupations: Counter, covered: set[str],
                          min_members: int = MIN_MEMBERS) -> list[dict]:
    """Wikidata occupations held by several people that no group claims."""
    out = []
    for occupation, count in occupations.most_common():
        if count < min_members or occupation.lower() in covered:
            continue
        out.append({
            "kind": "occupation", "label": occupation.title(),
            "term": occupation, "count": count,
            "why": f"{count} people list this Wikidata occupation, "
                   f"and no group claims it",
        })
    return out


def link_candidates(kinds: Counter, covered: set[str],
                    min_members: int = MIN_MEMBERS) -> list[dict]:
    out = []
    for kind, count in kinds.most_common():
        # "site" is every self-hosted domain — too broad to be a group.
        if count < min_members or kind in covered or kind == "site":
            continue
        out.append({
            "kind": "link", "label": kind.title(), "term": kind, "count": count,
            "why": f"{count} people declare a {kind} link, with no group for it",
        })
    return out


def organisation_candidates(orgs: list[dict], covered: set[str],
                            min_members: int = MIN_MEMBERS) -> list[dict]:
    """An organisation with several current people in it is already a group."""
    out = []
    for org in orgs:
        members = org.get("members") or 0
        if members < min_members or org["name"].lower() in covered:
            continue
        out.append({
            "kind": "organisation", "label": org["name"], "term": org["name"],
            "count": members,
            "why": f"{members} people currently affiliated — an organisation "
                   f"this size is already a group",
        })
    return out


def phrase_candidates(documents: list[str], covered_terms: set[str],
                      min_count: int = PHRASE_MIN,
                      max_share: float = 0.15) -> list[dict]:
    """Bigrams common across bios and posts but in no group definition.

    Capped by share as well as floored by count: a phrase in 8 of 560 bios is a
    community, one in 300 is filler.
    """
    counts: Counter = Counter()
    for document in documents:
        counts.update(bigrams(document))

    # "trans rights human rights" yields both "human rights" and "rights human"
    # as adjacent bigrams. Keep whichever is commoner and drop the mirror.
    for phrase in list(counts):
        a, _, b = phrase.partition(" ")
        mirror = f"{b} {a}"
        if mirror in counts and counts[mirror] < counts[phrase]:
            del counts[mirror]

    ceiling = max(int(len(documents) * max_share), min_count + 1)
    out = []
    for phrase, count in counts.most_common(400):
        if count < min_count or count > ceiling:
            continue
        if any(term in phrase for term in covered_terms):
            continue
        out.append({
            "kind": "phrase", "label": phrase.title(), "term": phrase,
            "count": count,
            "why": f"appears in {count} bios or recent posts and matches no "
                   f"existing group",
        })
    return out[:40]


def covered_terms() -> set[str]:
    """Every word already claimed by a group, so candidates are genuinely new."""
    from sonde.groups import GROUPS

    terms: set[str] = set()
    for group in GROUPS:
        terms.add(group["slug"].replace("-", " "))
        terms.add(group["name"].lower())
        for occupation in group.get("occupations", []):
            terms.add(occupation.lower())
        for kind in group.get("link_kinds", []):
            terms.add(kind.lower())
        for name in group.get("org_names", []):
            terms.add(name.lower())
    return terms
