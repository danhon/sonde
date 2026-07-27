"""How much an organisation is worth.

Editorial by nature, so the default is derived rather than asserted: an
organisation's weight comes from how many language Wikipedias carry an article
about it. That is editorially reviewed by other people, hard to game, and needs
no list maintained here — which is the whole point, because the alternative is
that Signal only counts if somebody remembered to add Signal.

Any weight can be overridden in the UI, and an overridden weight is never moved
by a later automatic pass.
"""

from __future__ import annotations

import math

# Kinds that carry weight on their own, before notability is considered.
KIND_FLOOR: dict[str, float] = {
    "news": 0.75,
    "government": 0.8,
    "academic": 0.7,
    "nonprofit": 0.65,
    "tech": 0.6,
    "publisher": 0.6,
    "newsletter": 0.5,
}

DEFAULT = 0.7
# ~150 language editions is about as notable as an organisation gets.
NOTABILITY_CEILING = 150


def default_weight(sitelinks: int | None, kind: str | None = None) -> float:
    """0.3–1.0. Log-scaled, because notability is a power law like everything else."""
    floor = KIND_FLOOR.get(kind or "", DEFAULT)
    if not sitelinks or sitelinks <= 0:
        return round(floor, 3)
    scaled = math.log10(sitelinks + 1) / math.log10(NOTABILITY_CEILING + 1)
    return round(max(0.3, min(1.0, max(floor, scaled))), 3)
