"""Latent groups — communities in the follow graph rather than words in bios.

M15b looked for groups by ranking *vocabulary by frequency*. On the live list
that produced "Send Tips", "Personal Account", "Dog Lover" and "Born Ppm". A
phrase being common is not evidence that a community exists; boilerplate is the
most common text there is.

A community is a shape in the follow graph. `affinity_edges` already holds
11,038 of them — which sampled account I follow follows which of my followers —
collected for the affinity score and read for nothing else.

The method, all of it measured on real data before being written:

  1. Represent each follower by the set of sources that follow them.
  2. Weight each source by IDF, so "followed by someone half my list follows"
     counts for almost nothing and a rare source counts for a lot.
  3. Cosine similarity between followers over that weighted set.
  4. Keep each follower's K nearest neighbours only, symmetrised.
  5. Label propagation over that graph.

**Step 4 is load-bearing.** The first version kept the globally strongest edges
instead, and label propagation collapsed into a single 465-node hairball, a 67
and a 5. Keeping edges per node is what stops hub followers swallowing the
graph. It is not a tuning detail and should not be "simplified" away.

Validation: on the real graph this produces a 25-person cluster that names
itself *narrative / game / games / designer* — the game industry, which M18
built independently from bio rules. Two methods sharing no inputs agreeing on a
group is the strongest evidence available here. It also finds law professors,
futures and foresight, data journalism and comics, none of which were seeded.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict

# A follower seen by fewer sources than this cannot be placed: one or two shared
# sources is coincidence, not a community.
MIN_SOURCES = 3

# A source followed by more than this share of the placeable population tells us
# almost nothing about who clusters with whom. IDF already discounts it; this
# also keeps the pair enumeration from going quadratic on the densest sources.
MAX_SOURCE_SHARE = 0.4
MIN_SOURCE_CEILING = 25

# Nearest neighbours kept per follower. Measured: K=5 gives 79 clusters of
# 4–120 covering 927 of 967 placeable followers; K=8 and K=12 start merging
# distinct communities into 192- and 227-person blobs.
NEIGHBOURS = 5

# Clusters outside this are not proposable. Below 4 is an anecdote; above this
# it is "people I follow" rather than a group.
MIN_MEMBERS = 4
MAX_MEMBERS = 120

PROPAGATION_ROUNDS = 40
SEED = 11


def _idf(sources_by_did: dict[str, set[str]]) -> dict[str, float]:
    counts: Counter = Counter()
    for sources in sources_by_did.values():
        counts.update(sources)
    total = max(len(sources_by_did), 1)
    return {s: math.log(total / c) for s, c in counts.items() if c}


def neighbour_graph(sources_by_did: dict[str, set[str]], *,
                    neighbours: int = NEIGHBOURS,
                    max_source_share: float = MAX_SOURCE_SHARE,
                    ) -> dict[str, dict[str, float]]:
    """Symmetric K-nearest-neighbour graph over cosine similarity."""
    if not sources_by_did:
        return {}
    idf = _idf(sources_by_did)
    norm = {
        did: math.sqrt(sum(idf.get(s, 0.0) ** 2 for s in sources)) or 1.0
        for did, sources in sources_by_did.items()
    }
    inverted: dict[str, list[str]] = defaultdict(list)
    for did, sources in sources_by_did.items():
        for source in sources:
            inverted[source].append(did)

    # A floor as well as a share. Expressed only as a fraction, a small
    # population makes *every* source "too common" — at 16 placeable followers
    # the ceiling was 6, every source had 8 members, and the graph came back
    # empty. Below roughly 60 people no source is uninformative.
    ceiling = max(int(len(sources_by_did) * max_source_share), MIN_SOURCE_CEILING)
    dot: dict[tuple[str, str], float] = defaultdict(float)
    for source, members in inverted.items():
        if len(members) > ceiling:
            continue
        weight = idf.get(source, 0.0) ** 2
        if weight <= 0:
            continue
        members = sorted(members)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                dot[(a, b)] += weight

    ranked: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for (a, b), value in dot.items():
        similarity = value / (norm[a] * norm[b])
        ranked[a].append((similarity, b))
        ranked[b].append((similarity, a))

    adjacency: dict[str, dict[str, float]] = defaultdict(dict)
    for did, candidates in ranked.items():
        # Sorted by (-similarity, did) so ties break identically every run — a
        # review queue that reshuffles itself is not reviewable.
        candidates.sort(key=lambda pair: (-pair[0], pair[1]))
        for similarity, other in candidates[:neighbours]:
            adjacency[did][other] = similarity
            adjacency[other][did] = similarity
    return dict(adjacency)


def label_propagation(adjacency: dict[str, dict[str, float]], *,
                      rounds: int = PROPAGATION_ROUNDS,
                      seed: int = SEED) -> dict[str, str]:
    """Assign each node a community label. Deterministic for a given graph."""
    labels = {did: did for did in adjacency}
    order = sorted(adjacency)
    rng = random.Random(seed)
    for _ in range(rounds):
        rng.shuffle(order)
        moved = 0
        for did in order:
            neighbours = adjacency.get(did)
            if not neighbours:
                continue
            tally: dict[str, float] = defaultdict(float)
            for other, weight in neighbours.items():
                tally[labels[other]] += weight
            # Break ties on the label itself, not on dict order.
            best = max(tally.items(), key=lambda kv: (kv[1], kv[0]))[0]
            if best != labels[did]:
                labels[did] = best
                moved += 1
        if not moved:
            break
    return labels


def cluster(sources_by_did: dict[str, set[str]], *,
            min_sources: int = MIN_SOURCES,
            neighbours: int = NEIGHBOURS,
            min_members: int = MIN_MEMBERS,
            max_members: int = MAX_MEMBERS) -> list[list[str]]:
    """Communities of followers, largest first. Members are sorted DIDs."""
    placeable = {
        did: sources for did, sources in sources_by_did.items()
        if len(sources) >= min_sources
    }
    adjacency = neighbour_graph(placeable, neighbours=neighbours)
    if not adjacency:
        return []
    labels = label_propagation(adjacency)
    grouped: dict[str, list[str]] = defaultdict(list)
    for did, label in labels.items():
        grouped[label].append(did)
    out = [
        sorted(members) for members in grouped.values()
        if min_members <= len(members) <= max_members
    ]
    out.sort(key=lambda members: (-len(members), members[0]))
    return out


# ------------------------------------------------------------------ naming

# Clustering succeeds more often than naming does: about 20 of 58 real clusters
# had no distinctive vocabulary at all. So naming is tiered, and a cluster with
# no label is still proposed — an unnamed real community is worth reviewing,
# and a wrong name is worse than none.
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "you", "your", "from", "are",
    "all", "not", "was", "have", "has", "his", "her", "they", "them", "our",
    "out", "who", "what", "when", "how", "why", "can", "will", "just", "about",
    "into", "more", "some", "than", "then", "now", "also", "here", "there",
    "over", "very", "much", "one", "two", "new", "old", "get", "got", "make",
    "made", "like", "love", "she", "him", "its", "posts", "post", "opinions",
    "own", "views", "mine", "via", "http", "https", "com", "www", "bsky",
    "social", "app", "profile", "here", "everything", "things", "stuff",
    "he", "she", "they", "him", "his", "hers", "theirs",
}
PRONOUNS = {"him", "her", "them", "she", "hers", "theirs", "his"}

# Bio filler. These are words people write *about having a profile* rather than
# about themselves, and they cluster with nothing. Measured: the first run
# labelled communities "Org", "Account · Senior" and "Person · Editor".
BOILERPLATE = {
    "account", "personal", "person", "senior", "junior", "former", "currently",
    "previously", "opinions", "retweets", "endorsement", "endorsements",
    "views", "employer", "dms", "email", "contact", "newsletter", "blog",
    "linktr", "linktree", "substack", "mastodon", "twitter", "org", "net",
    "www", "http", "https", "day", "time", "year", "years", "world", "life",
    "good", "best", "great", "thing", "things", "stuff", "work", "working",
    "guy", "dude", "folks", "human", "lover", "fan", "enthusiast",
}
WORD = re.compile(r"[a-z][a-z'&-]{2,}", re.I)

# URLs first, or a bio's own links become its vocabulary: "linktr" and "org"
# were both labelling communities before this existed.
URLS = re.compile(
    r"https?://\S+|\b[\w.-]+\.(?:com|org|net|social|app|io|co|uk|me|dev|fyi|wiki)\b",
    re.I)

MIN_LIFT = 2.5
MIN_SHARE = 0.2


def _tokens(text: str) -> set[str]:
    cleaned = URLS.sub(" ", text or "")
    return {
        word.lower() for word in WORD.findall(cleaned)
        if word.lower() not in STOPWORDS and word.lower() not in BOILERPLATE
    }


def _by_lift(members: list[str], values: dict[str, list[str]],
             corpus: Counter, total: int, *,
             min_share: float = MIN_SHARE,
             min_lift: float = MIN_LIFT) -> list[tuple[str, int]]:
    """Terms disproportionately common inside the cluster.

    Lift, never raw frequency. Frequency is what made M15b propose "Personal
    Account"; the question is not "what do these people say" but "what do they
    say that everyone else does not".
    """
    inside: Counter = Counter()
    for did in members:
        inside.update(set(values.get(did) or []))
    floor = max(3, int(len(members) * min_share))
    scored = []
    for term, count in inside.items():
        if count < floor or term in PRONOUNS or term in BOILERPLATE:
            continue
        if term in STOPWORDS:
            continue
        expected = corpus.get(term, 0) / max(total, 1)
        if expected <= 0:
            continue
        lift = (count / len(members)) / expected
        if lift >= min_lift:
            scored.append((lift * math.log(count + 1), term, count))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [(term, count) for _, term, count in scored]


def name_cluster(members: list[str], evidence: dict[str, dict[str, list[str]]],
                 corpora: dict[str, Counter], total: int) -> dict:
    """Best available label for a cluster, with the tier that produced it.

    `evidence` maps a tier name to {did: [terms]}. Tiers are tried strongest
    first: a resolved employer beats a Wikidata occupation beats a bio word.
    """
    tiers = [
        ("affiliation", "share an employer"),
        ("occupation", "share a Wikidata occupation"),
        ("link", "share a kind of link"),
        ("text", "share vocabulary no one else uses"),
    ]
    for tier, why in tiers:
        values = evidence.get(tier) or {}
        if not values:
            continue
        hits = _by_lift(members, values, corpora.get(tier, Counter()), total)
        if not hits:
            continue
        terms = [term for term, _ in hits[:3]]
        top, count = hits[0]
        return {
            "tier": tier,
            "label": " · ".join(t.title() for t in terms),
            "term": top,
            "why": f"{len(members)} people who follow the same accounts and "
                   f"{why} — {count} of them list {top!r}",
        }
    return {
        "tier": "unnamed",
        "label": "",
        "term": "",
        "why": f"{len(members)} people who follow strikingly similar accounts, "
               f"with nothing in common in their bios — worth a look precisely "
               f"because no rule would find them",
    }
