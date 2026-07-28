"""The relationship score — who I actually talk to.

Deliberately separate from influence. Influence asks "does this person matter";
relationship asks "do we know each other". A 700k-follower columnist I have
never spoken to should rank low here, and a 400-follower friend I talk to weekly
should rank high. Averaging the two would destroy both.

Weighted by what an interaction costs the person giving it, not by volume — a
bot that likes everything would otherwise win.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

# What each interaction is worth. A like is nearly free to give; a reply in a
# thread you both took part in is not.
KIND_WEIGHT: dict[str, float] = {
    "reply": 3.0,
    "quote": 2.5,
    "repost": 1.2,
    "mention": 0.8,
    "like": 0.3,
}

# Outbound counts for slightly more than inbound: choosing to reply to someone
# says more about the relationship than being replied to.
DIRECTION_WEIGHT = {"outbound": 1.15, "inbound": 1.0}

# Interactions decay; a conversation last week beats one in 2023.
HALF_LIFE_DAYS = 120.0

# A thread we both posted in more than once is the strongest single signal.
CONVERSATION_BONUS = 6.0

# Normalisation ceiling — the raw weighted total that counts as a full score.
# Calibrated against the observed distribution rather than guessed; see
# store.relationship_scale.
DEFAULT_SCALE = 40.0


def decay(occurred_at: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    try:
        when = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    days = max((now - when).total_seconds() / 86400, 0)
    return 0.5 ** (days / HALF_LIFE_DAYS)


@dataclass
class Relationship:
    did: str
    raw: float = 0.0
    inbound: int = 0
    outbound: int = 0
    by_kind: dict = field(default_factory=dict)
    conversations: int = 0
    days_active: int = 0
    last_at: str | None = None
    # Attention scarcity (M17) — added, not multiplied, so it can register a
    # relationship on its own. Someone who follows 200 people including me and
    # has never replied is not a stranger, and the interaction terms cannot see
    # that. Capped well below 100 so it never outranks actual conversation.
    attention: float = 0.0
    attention_note: str | None = None
    # The working, stored rather than recomputed at render time so the profile
    # always shows the scale the score was actually calculated with.
    attention_detail: dict | None = None

    @property
    def reciprocity(self) -> float:
        """1.0 for balanced, near 0 for one-directional.

        Fifty likes from someone I have never answered is an audience, not a
        relationship — so this multiplies rather than adds.
        """
        if not self.inbound and not self.outbound:
            return 0.0
        low, high = sorted((self.inbound, self.outbound))
        return round((low / high) if high else 0.0, 3)

    def interaction_score(self, scale: float = DEFAULT_SCALE) -> float:
        """The part earned by actually talking, 0–100."""
        if self.raw <= 0:
            return 0.0
        # Reciprocity is a multiplier with a floor, so a purely one-way
        # relationship still registers — just much lower.
        balance = 0.45 + 0.55 * self.reciprocity
        # Breadth: interacting across many separate days beats a single burst.
        # One argument is not a relationship.
        breadth = min(math.log10(self.days_active + 1) / math.log10(30), 1.0)
        spread = 0.6 + 0.4 * breadth
        value = self.raw * balance * spread
        return round(min(value / max(scale, 1.0), 1.0) * 100, 1)

    def score(self, scale: float = DEFAULT_SCALE) -> float:
        # Attention is additive and headroom-limited: it cannot push a real
        # conversation down the list, only lift a silent follower into view.
        total = self.interaction_score(scale) + max(self.attention, 0.0)
        return round(min(total, 100.0), 1)

    def as_dict(self, scale: float = DEFAULT_SCALE) -> dict:
        return {
            "score": self.score(scale), "raw": round(self.raw, 2),
            "interaction_score": self.interaction_score(scale),
            "attention": round(self.attention, 1),
            "attention_note": self.attention_note,
            "attention_detail": self.attention_detail,
            "inbound": self.inbound, "outbound": self.outbound,
            "reciprocity": self.reciprocity, "conversations": self.conversations,
            "days_active": self.days_active, "by_kind": self.by_kind,
            "last_at": self.last_at,
        }


def summarise(rows: list[dict], conversations: int = 0,
              now: datetime | None = None) -> Relationship:
    """Fold one person's interactions into a relationship."""
    if not rows:
        return Relationship(did="")
    rel = Relationship(did=rows[0]["did"])
    days: set[str] = set()
    for row in rows:
        weight = KIND_WEIGHT.get(row["kind"], 0.3)
        weight *= DIRECTION_WEIGHT.get(row["direction"], 1.0)
        weight *= decay(row["occurred_at"], now)
        rel.raw += weight
        if row["direction"] == "outbound":
            rel.outbound += 1
        else:
            rel.inbound += 1
        rel.by_kind[row["kind"]] = rel.by_kind.get(row["kind"], 0) + 1
        days.add((row["occurred_at"] or "")[:10])
        if not rel.last_at or row["occurred_at"] > rel.last_at:
            rel.last_at = row["occurred_at"]
    rel.days_active = len({d for d in days if d})
    rel.conversations = conversations
    # A real back-and-forth is worth more than the sum of its messages.
    rel.raw += conversations * CONVERSATION_BONUS
    return rel
