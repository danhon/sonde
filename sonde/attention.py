"""What my slot in someone's attention budget is worth.

If an account has 150,000 followers but follows only 1,037 people, and I am one
of them, that is a deliberate and scarce choice. It is relationship evidence
even with no interaction history at all — which is exactly the gap in the
interaction-only relationship score, where a person who follows 200 accounts
including mine but has never replied scores zero.

**This is not the followers/follows ratio**, and the distinction is the whole
point. The influence score's `selectivity` component is that ratio, and a ratio
collapses two separate facts into one number:

    A:   1,000 followers /    10 follows  -> ratio 100
    B: 500,000 followers / 5,000 follows  -> ratio 100

Selectivity scores these identically. But a slot in B's list is worth far more:
the same share of an audience 500 times larger. So the two terms are kept
separate here and multiplied, which also means **both** conditions have to hold
— the way the question was actually asked.

They answer different questions, too. Selectivity asks whether *they* are
discriminating, a property of them, which is why it belongs to influence. This
asks what *my place in their attention* is worth, a property of us, which is the
relationship score's question.
"""

from __future__ import annotations

import math

# Above this many follows, following is not a curated act — nobody reads 5,000
# accounts, so a slot in that list says nothing about me.
SCARCITY_CEILING = 5000.0
# Below this, being more selective stops meaning more.
SCARCITY_FLOOR = 50.0

# Standing is gated at 1,000 followers. An earlier draft reused the ungated
# log10(followers)/5 from `reach` and ranked a 434-follower account above Cory
# Doctorow: a small account following 45 people is an ordinary small account,
# not scarce attention. The gate is what makes this match the request.
STANDING_GATE = 3.0      # log10(1,000)
STANDING_DECADES = 2.0   # full standing at 100,000 followers

# Most of a relationship score should still come from actually talking. This
# caps what scarcity alone can contribute, so it lifts a silent notable follower
# into view without ever outranking a real conversation.
MAX_POINTS = 30.0

# Fallback until enough followers are measured to calibrate against.
DEFAULT_SCALE = 0.25


def scarcity(follows_count: int | None) -> float:
    """How scarce a slot in their following list is, 0–1."""
    if follows_count is None or follows_count < 0:
        return 0.0
    clamped = max(float(follows_count), SCARCITY_FLOOR)
    span = math.log10(SCARCITY_CEILING / SCARCITY_FLOOR)
    return max(0.0, min(math.log10(SCARCITY_CEILING / clamped) / span, 1.0))


def standing(followers_count: int | None) -> float:
    """How much their attention is worth, 0–1, gated at 1,000 followers."""
    if not followers_count or followers_count < 1:
        return 0.0
    decades = math.log10(float(followers_count)) - STANDING_GATE
    return max(0.0, min(decades / STANDING_DECADES, 1.0))


def raw(followers_count: int | None, follows_count: int | None) -> float:
    """Unscaled attention signal, 0–1. Multiplicative: both must hold."""
    if followers_count is None or follows_count is None:
        return 0.0
    return scarcity(follows_count) * standing(followers_count)


def calibrate(raws: list[float], *, floor: float = 0.05) -> float:
    """Self-calibrating divisor: the most scarce follower I actually have.

    A fixed divisor would silently rescale the component as the follower list
    grows. Only non-zero values are considered — roughly three quarters of
    followers score zero by design, and letting them set the scale would
    collapse it.

    This is the **maximum**, not a percentile, and that is deliberate. A p99
    over ~400 non-zero values leaves four people above the line, and every one
    of them lands on the cap: on real data alt18f, doublepulsar, edoggthered and
    mattbors all came out at exactly 30.0, destroying the ordering at precisely
    the top of the list this exists to rank. The trade is that one extreme
    account compresses everyone below it, which is acceptable because `raw` is
    bounded at 1.0 and the observed distribution has no cliff.
    """
    values = [v for v in raws if v > 0]
    if len(values) < 20:
        return DEFAULT_SCALE
    return max(max(values), floor)


def points(followers_count: int | None, follows_count: int | None,
           scale: float = DEFAULT_SCALE) -> float:
    """Relationship points contributed by attention scarcity, 0–MAX_POINTS."""
    value = raw(followers_count, follows_count)
    if value <= 0:
        return 0.0
    return round(min(value / max(scale, 0.01), 1.0) * MAX_POINTS, 1)


def explain(followers_count: int | None, follows_count: int | None,
            scale: float = DEFAULT_SCALE) -> dict | None:
    """Every number behind the score, for showing the working on a profile."""
    value = raw(followers_count, follows_count)
    if value <= 0:
        return None
    ratio = max(int(followers_count or 0), 1) / max(int(follows_count or 1), 1)
    return {
        "scarcity": round(scarcity(follows_count), 3),
        "standing": round(standing(followers_count), 3),
        "raw": round(value, 3),
        "scale": round(scale, 3),
        "points": points(followers_count, follows_count, scale),
        "ratio": round(ratio, 1),
        "ratio_label": f"{ratio:,.0f}×",
        "counts_label": f"{followers_count:,} ÷ {follows_count:,}",
        "share": round(100.0 / max(int(follows_count or 1), 1), 4),
        "floor": f"{int(SCARCITY_FLOOR):,}",
        "ceiling": f"{int(SCARCITY_CEILING):,}",
        "max_points": int(MAX_POINTS),
    }


def describe(followers_count: int | None, follows_count: int | None) -> str | None:
    """One plain sentence of evidence, or None when the signal is absent.

    Stated as the share of their attention rather than the score, because the
    share is the checkable fact and the score is our interpretation of it.
    """
    if raw(followers_count, follows_count) <= 0:
        return None
    share = 100.0 / max(int(follows_count or 1), 1)
    shown = f"{share:.2f}%" if share < 1 else f"{share:.1f}%"
    return (
        f"Follows only {follows_count:,} accounts while {followers_count:,} "
        f"follow them — I am {shown} of what they chose to read."
    )
