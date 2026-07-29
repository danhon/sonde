"""What can be run, and in what order.

The settings page had grown sixteen buttons, several of which only make sense
in sequence — enrichment needs hydrated profiles, grouping needs enrichment,
propagation needs the affinity index. That ordering lived in my head and in
release notes, which is the wrong place for it.

Batches encode it. Each runs its steps in order, stopping if one fails, so the
common case is one button rather than five clicked in the right sequence.
Individual jobs stay available for when you want exactly one thing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    key: str
    label: str
    cost: str
    note: str = ""


@dataclass(frozen=True)
class Batch:
    key: str
    label: str
    steps: tuple[str, ...]
    cost: str
    why: str


JOBS: tuple[Job, ...] = (
    Job("head", "Head sweep", "1–2 calls", "new followers only"),
    Job("full", "Full sweep", "116 calls", "whole list; the only source of departures"),
    Job("hydrate", "Hydrate profiles", "up to 40 calls", "follower counts"),
    Job("follows", "Sync my follows", "46 calls", "mutuals, and affinity sources"),
    Job("posts", "Fetch posts", "~600 calls", "top 500 plus verified"),
    Job("external", "External reputation", "~100 calls", "Wikidata and pageviews"),
    Job("affiliations", "Rebuild affiliations", "none", "who works where"),
    Job("relevance", "Exact relevance", "~1,000 calls", "needs the app password"),
    Job("groups", "Reclassify circles", "none", ""),
    Job("discover", "Find new circles", "none", "proposals only"),
    Job("latent", "Find latent communities", "none",
        "clusters the follow graph; proposals only"),
    Job("propagate", "Propagate circles", "none", "needs a fresh affinity index"),
    Job("affinity", "Rebuild affinity index", "~1,700 calls", "slow; monthly is enough"),
    Job("interactions", "Sync interactions", "~500 calls", "needs the app password"),
    Job("rescore-relationships", "Rescore relationships", "none", ""),
    Job("moderation", "Sync moderation lists", "~1,000 calls", ""),
    Job("rescore", "Rescore influence", "none", ""),
    Job("backup", "Backup now", "none", ""),
    Job("digest", "Send digest now", "none", "forces a send"),
)

BY_KEY = {job.key: job for job in JOBS}

BATCHES: tuple[Batch, ...] = (
    Batch("refresh", "Refresh followers", ("full", "hydrate", "follows"),
          "~200 calls",
          "Sweep the list, fill in follower counts, then update mutuals. Start "
          "here — everything else builds on it."),
    Batch("enrich", "Enrich profiles", ("posts", "external", "affiliations"),
          "~700 calls",
          "Recent posts, Wikidata and Wikipedia, then work out who is affiliated "
          "with what. Needs hydrated profiles."),
    Batch("regroup", "Rebuild circles", ("groups", "discover", "latent", "propagate"),
          "none",
          "Classify, propose circles nobody named, then fill the gaps rules "
          "cannot reach. Needs enrichment first."),
    Batch("relationships", "Update relationships",
          ("interactions", "rescore-relationships"), "~500 calls",
          "Pull interactions and rescore who you actually talk to."),
    Batch("housekeeping", "Housekeeping",
          ("moderation", "rescore", "backup"), "~1,000 calls",
          "Refresh moderation lists, recompute influence, take a snapshot."),
)

BATCH_BY_KEY = {batch.key: batch for batch in BATCHES}

# Everything a batch already covers, so the individual list can say so.
IN_A_BATCH = {step for batch in BATCHES for step in batch.steps}
