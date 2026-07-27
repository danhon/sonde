"""The influence score.

Design, evidence and worked examples live in SCORING.md. The rules that matter
when reading this file:

* Weights sum to 100 and live in ONE dict, surfaced read-only in the UI.
* Every component returns its own 0–1 value plus the measurement it used, so a
  row can always explain itself. A score nobody can interrogate is a score
  nobody should trust.
* Components DEGRADE rather than lie. Affinity is exact for the enriched set and
  sampled below it; liveness is real for the enriched set and a lifetime average
  below it. The UI marks which.
* Unavailable components are EXCLUDED from the denominator rather than scored 0,
  so a follower isn't punished for data sonde hasn't fetched. `out_of` reports
  the achievable maximum.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

# Sums to 100. See SCORING.md for why each weight is what it is.
WEIGHTS: dict[str, int] = {
    "reach": 18,
    "institution": 18,
    "affinity": 16,
    "verified_affinity": 13,
    "public_profile": 12,
    "selectivity": 11,
    "liveness": 7,
    "verification": 5,
}

# Below this, followers ÷ following is noise: an account with 5 followers that
# follows 1 would otherwise out-score a working journalist with 20k who follows
# 8k. The gate is load-bearing, not a tuning detail.
SELECTIVITY_FLOOR = 500

# log10(followers) / REACH_LOG_CEILING, clamped. 100k followers ≈ 0.8, 1M ≈ 1.0.
REACH_LOG_CEILING = 5.0
SELECTIVITY_LOG_CEILING = 2.0

# Weighted overlap counting as maximum affinity. Hits are weighted by how
# selective their source is, so this is not a raw count. Calibrated against the
# real index at 6d — see evals/affinity_check.py.
AFFINITY_SAMPLED_FULL = 40
AFFINITY_EXACT_FULL = 250     # knownFollowers count that counts as maximum
LIVENESS_HALF_LIFE_DAYS = 30.0

# A lifetime posts-per-day average cannot exceed this. It is a proxy for
# "is this account alive" and it cannot prove recency — an account that posted
# furiously and died in 2024 still scores well on it. Capping below 1.0 means
# only a real last-post date can max the component out. Found by the live eval:
# a follow-back farm posting 19.5/day was taking full liveness marks.
LIVENESS_PROXY_CEILING = 0.6
# Sustained posting above this rate is not more "alive", just louder.
LIVENESS_SATURATION_PER_DAY = 3.0


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass
class Component:
    """One signal's contribution, with enough context to explain itself."""

    key: str
    value: float                  # 0–1
    weight: int
    source: str                   # which measurement produced it
    detail: str = ""
    available: bool = True

    @property
    def points(self) -> float:
        return round(self.value * self.weight, 2) if self.available else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": round(self.value, 4),
            "weight": self.weight,
            "points": self.points,
            "source": self.source,
            "detail": self.detail,
            "available": self.available,
        }


@dataclass
class Score:
    total: float
    out_of: int
    components: list[Component] = field(default_factory=list)

    @property
    def normalised(self) -> float:
        """Scaled to /100 so partially-enriched actors remain comparable."""
        if self.out_of <= 0:
            return 0.0
        return round(self.total / self.out_of * 100, 1)

    def as_json(self) -> str:
        return json.dumps(
            {
                "total": self.total,
                "out_of": self.out_of,
                "normalised": self.normalised,
                "components": [c.as_dict() for c in self.components],
            }
        )


def _reach(followers: int | None) -> Component:
    if followers is None:
        return Component("reach", 0.0, WEIGHTS["reach"], "not hydrated", available=False)
    value = clamp(math.log10(max(followers, 1)) / REACH_LOG_CEILING)
    return Component(
        "reach", value, WEIGHTS["reach"], "followersCount",
        f"{followers:,} followers, log-scaled",
    )


def _selectivity(followers: int | None, follows: int | None) -> Component:
    if followers is None or follows is None:
        return Component(
            "selectivity", 0.0, WEIGHTS["selectivity"], "not hydrated", available=False
        )
    if followers < SELECTIVITY_FLOOR:
        return Component(
            "selectivity", 0.0, WEIGHTS["selectivity"], "gated",
            f"under {SELECTIVITY_FLOOR:,} followers — the ratio is noise here",
        )
    ratio = max(followers, 1) / max(follows, 1)
    value = clamp(math.log10(ratio) / SELECTIVITY_LOG_CEILING)
    return Component(
        "selectivity", value, WEIGHTS["selectivity"], "followers ÷ follows",
        f"{followers:,} ÷ {follows:,} = {ratio:.1f}×",
    )


def _verification(verified_status: str | None, trusted: str | None) -> Component:
    if trusted == "valid":
        return Component("verification", 1.0, WEIGHTS["verification"], "trustedVerifierStatus",
                         "trusted verifier")
    if verified_status == "valid":
        return Component("verification", 0.7, WEIGHTS["verification"], "verifiedStatus",
                         "verified")
    return Component("verification", 0.0, WEIGHTS["verification"], "verifiedStatus",
                     "not verified")


def _affinity(sampled: int | None, exact: int | None,
              scale: float | None = None) -> Component:
    """Exact where tier 3b has run, sampled index otherwise — labelled either way."""
    if exact is not None:
        value = clamp(exact / AFFINITY_EXACT_FULL)
        return Component("affinity", value, WEIGHTS["affinity"], "knownFollowers (exact)",
                         f"{exact:,} accounts you follow also follow them")
    if sampled is not None:
        # Scale is the observed 99th percentile, not a constant: weighted overlap
        # is proportional to how many sources the index used, so a fixed ceiling
        # would silently rescale every score whenever the source count changed.
        value = clamp(sampled / max(scale or AFFINITY_SAMPLED_FULL, 1))
        return Component("affinity", value, WEIGHTS["affinity"], "sampled index",
                         f"weighted overlap {sampled:g} with the accounts you follow")
    return Component("affinity", 0.0, WEIGHTS["affinity"], "index not built",
                     available=False)


def _verified_affinity(hits: int | None, source_total: int | None) -> Component:
    if hits is None:
        return Component("verified_affinity", 0.0, WEIGHTS["verified_affinity"],
                         "index not built", available=False)
    # A follower reaching a fifth of the verified sources is treated as maximal;
    # in the real data the top account reaches well under that.
    denominator = max(source_total or 147, 1)
    value = clamp(hits / max(denominator * 0.2, 1))
    return Component(
        "verified_affinity", value, WEIGHTS["verified_affinity"], "verified index",
        f"{hits} of the {denominator} verified accounts in your network",
    )


def _liveness(last_post_at: str | None, posts: int | None, account_created_at: str | None,
              now_iso: str | None = None) -> Component:
    """Real recency where available; a lifetime average is explicitly labelled."""
    from datetime import datetime, timezone

    def _parse(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    now = _parse(now_iso) if now_iso else datetime.now(timezone.utc)
    now = now or datetime.now(timezone.utc)

    if last_post_at:
        posted = _parse(last_post_at)
        if posted:
            days = max((now - posted).total_seconds() / 86400, 0)
            value = clamp(math.exp(-days / LIVENESS_HALF_LIFE_DAYS))
            return Component("liveness", value, WEIGHTS["liveness"], "last post",
                             f"posted {days:.0f} day(s) ago")

    if posts and account_created_at:
        created = _parse(account_created_at)
        if created:
            age_days = max((now - created).total_seconds() / 86400, 1)
            per_day = posts / age_days
            saturated = clamp(
                math.log10(per_day + 1) / math.log10(LIVENESS_SATURATION_PER_DAY + 1)
            )
            value = saturated * LIVENESS_PROXY_CEILING
            return Component(
                "liveness", value, WEIGHTS["liveness"], "lifetime average",
                f"{per_day:.1f} posts/day since {created.date()} — a lifetime "
                f"average, not recency (capped at {LIVENESS_PROXY_CEILING:g})",
            )

    return Component("liveness", 0.0, WEIGHTS["liveness"], "unknown", available=False)


def _institution(score: float | None, name: str | None, confidence: float | None,
                 method: str | None) -> Component:
    if score is None:
        return Component("institution", 0.0, WEIGHTS["institution"], "not matched",
                         available=False)
    return Component(
        "institution", clamp(score), WEIGHTS["institution"], method or "matched",
        f"{name} (confidence {confidence:.2f})" if name and confidence else (name or ""),
    )


def _public_profile(sitelinks: int | None, pageviews: int | None) -> Component:
    if sitelinks is None and pageviews is None:
        return Component("public_profile", 0.0, WEIGHTS["public_profile"],
                         "no external match", available=False)
    parts, detail = [], []
    if sitelinks is not None:
        parts.append(clamp(math.log10(sitelinks + 1) / math.log10(80)))
        detail.append(f"{sitelinks} Wikipedia language editions")
    if pageviews is not None:
        parts.append(clamp(math.log10(pageviews + 1) / math.log10(30000)))
        detail.append(f"{pageviews:,} views/30d")
    value = sum(parts) / len(parts)
    return Component("public_profile", value, WEIGHTS["public_profile"], "Wikidata/Wikipedia",
                     " · ".join(detail))


def score_actor(row: dict, *, verified_source_total: int | None = None,
                now_iso: str | None = None, active: set[str] | None = None) -> Score:
    """Score one actor row.

    `active` names the components whose backing data has been computed for the
    corpus as a whole. Availability has to be decided globally, not per actor:
    once the affinity index exists, an actor with zero hits must score 0 on it
    rather than being marked "unavailable" — otherwise not having been indexed
    yet beats being indexed and found wanting, and the accounts we know least
    about float to the top. Components outside `active` are excluded from
    everyone's denominator equally, which is fair because it is uniform.
    """
    components = [
        _reach(row.get("followers_count")),
        _institution(
            row.get("institution_score"), row.get("institution_name"),
            row.get("institution_confidence"), row.get("institution_method"),
        ),
        _affinity(row.get("affinity_sampled"), row.get("affinity_exact"),
                  row.get("affinity_scale")),
        _verified_affinity(row.get("verified_affinity"), verified_source_total),
        _public_profile(row.get("wikidata_sitelinks"), row.get("wikipedia_views_30d")),
        _selectivity(row.get("followers_count"), row.get("follows_count")),
        _liveness(row.get("last_post_at"), row.get("posts_count"),
                  row.get("account_created_at"), now_iso),
        _verification(row.get("verified_status"), row.get("trusted_verifier_status")),
    ]

    if active is not None:
        for c in components:
            if c.key in active and not c.available:
                # The corpus has this signal; this actor simply has none of it.
                # That is a real zero, not missing data.
                c.available = True
                c.value = 0.0
                c.source = c.source if c.source != "not matched" else "no match"
                c.detail = c.detail or "none found"
            elif c.key not in active:
                c.available = False

    total = round(sum(c.points for c in components), 2)
    out_of = sum(c.weight for c in components if c.available)
    return Score(total=total, out_of=out_of, components=components)


def active_components(coverage: dict[str, int]) -> set[str]:
    """Which components have backing data for the corpus.

    `coverage` maps a component key to how many actors carry data for it.
    Verification is always active — it rides on the follower sweep, so absence
    genuinely means "not verified" rather than "not fetched".
    """
    active = {"verification"}
    for key, count in coverage.items():
        if count > 0:
            active.add(key)
    return active
