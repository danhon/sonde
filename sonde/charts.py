"""Chart geometry — pure functions, no database and no markup.

Charts are server-rendered inline SVG. sonde has no build step and loads
Tailwind from a CDN, so a charting library would be its first frontend
dependency; the existing `_growth.html` sparkline already set this pattern.

Everything that could be wrong about a chart *arithmetically* — a scale that
inverts, a bin that swallows its neighbour, a log axis fed a zero — is decided
here, where it can be tested without rendering anything.

Palette validated with the dataviz checker rather than chosen by eye:

    lightness band · chroma floor · CVD separation · normal-vision floor  PASS
    contrast vs surface                                                   WARN

The warning covers aqua and yellow and is not dismissable: it obliges visible
labels or a table. Every chart ships with the table it was drawn from, which is
also the accessibility answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Categorical hues in fixed order. Never cycled: a ninth series folds into
# "other" rather than reusing slot 1, because a repeated hue reads as a repeated
# entity.
SERIES = ["#b4123c", "#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK = "#0f172a"
MUTED = "#94a3b8"
GRID = "#e2e8f0"
SURFACE = "#ffffff"


@dataclass(frozen=True)
class Box:
    """Plot area in SVG user units."""
    width: int = 720
    height: int = 200
    left: int = 44
    right: int = 12
    top: int = 12
    bottom: int = 28

    @property
    def inner_width(self) -> float:
        return self.width - self.left - self.right

    @property
    def inner_height(self) -> float:
        return self.height - self.top - self.bottom


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """Round tick values covering [lo, hi].

    Returns at least two ticks even for a flat series, because an axis with one
    label cannot be read as a scale.
    """
    if not (hi > lo):
        return [lo, lo + 1] if lo is not None else [0, 1]
    raw = (hi - lo) / max(count, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        step = magnitude * multiple
        if raw <= step:
            break
    start = math.floor(lo / step) * step
    ticks = []
    value = start
    while value <= hi + step * 0.5:
        ticks.append(round(value, 10))
        value += step
    return ticks


def linear(value: float, lo: float, hi: float, size: float) -> float:
    if hi == lo:
        return size / 2
    return (value - lo) / (hi - lo) * size


def log_scale(value: float | None, lo: float, hi: float, size: float) -> float:
    """Position on a log axis. Zero and None clamp to the floor.

    A log axis has no place for zero, and silently dropping those points would
    misrepresent the population — so they are pinned to the axis minimum and the
    caller is expected to say so.
    """
    if not value or value <= 0:
        return 0.0
    lo = max(lo, 1)
    if hi <= lo:
        return size / 2
    return (math.log10(max(value, lo)) - math.log10(lo)) / \
           (math.log10(hi) - math.log10(lo)) * size


def log_ticks(lo: float, hi: float) -> list[float]:
    """Decade ticks: 1, 10, 100, …"""
    lo = max(lo, 1)
    out, value = [], 10 ** math.floor(math.log10(lo))
    while value <= hi * 1.001:
        if value >= lo * 0.999:
            out.append(value)
        value *= 10
    return out or [lo, hi]


def compact(value: float) -> str:
    """1_234 -> '1.2k'. Axis labels have no room for thousands separators."""
    v = float(value)
    for limit, suffix in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if abs(v) >= limit:
            trimmed = v / limit
            return f"{trimmed:.0f}{suffix}" if trimmed >= 10 else f"{trimmed:.1f}{suffix}"
    return f"{v:,.0f}" if abs(v) >= 1 else f"{v:g}"


def columns(rows: list[tuple[str, float]], box: Box = Box(),
            *, gap: float = 2.0) -> dict:
    """Bars along an ordered categorical axis.

    Gaps are in surface colour and 2px wide, so adjacent bars read as separate
    marks rather than one shape.
    """
    if not rows:
        return {"bars": [], "ticks": [], "box": box, "max": 0}
    peak = max(v for _, v in rows) or 1
    ticks = nice_ticks(0, peak)
    top = max(ticks[-1], peak)
    slot = box.inner_width / len(rows)
    bars = []
    for index, (label, value) in enumerate(rows):
        height = linear(value, 0, top, box.inner_height)
        bars.append({
            "label": label, "value": value,
            "x": round(box.left + index * slot + gap / 2, 2),
            "width": round(max(slot - gap, 0.5), 2),
            "y": round(box.top + box.inner_height - height, 2),
            "height": round(max(height, 0.0), 2),
        })
    return {
        "bars": bars, "box": box, "max": top,
        "ticks": [{"value": t, "label": compact(t),
                   "y": round(box.top + box.inner_height
                              - linear(t, 0, top, box.inner_height), 2)}
                  for t in ticks],
    }


def scatter(points: list[dict], box: Box = Box(), *,
            x_key: str = "x", y_key: str = "y") -> dict:
    """Log–log scatter. Points with a zero on either axis are excluded.

    Returned as `dropped` rather than silently omitted: a scatter that quietly
    discards part of its population is lying about its denominator.
    """
    usable = [p for p in points
              if (p.get(x_key) or 0) > 0 and (p.get(y_key) or 0) > 0]
    dropped = len(points) - len(usable)
    if not usable:
        return {"points": [], "dropped": dropped, "box": box,
                "x_ticks": [], "y_ticks": []}
    xs = [p[x_key] for p in usable]
    ys = [p[y_key] for p in usable]
    x_lo, x_hi = max(min(xs), 1), max(xs)
    y_lo, y_hi = max(min(ys), 1), max(ys)
    placed = []
    for point in usable:
        placed.append({
            **point,
            "cx": round(box.left + log_scale(point[x_key], x_lo, x_hi,
                                             box.inner_width), 2),
            "cy": round(box.top + box.inner_height
                        - log_scale(point[y_key], y_lo, y_hi,
                                    box.inner_height), 2),
        })
    return {
        "points": placed, "dropped": dropped, "box": box,
        "x_ticks": [{"value": t, "label": compact(t),
                     "x": round(box.left + log_scale(t, x_lo, x_hi,
                                                     box.inner_width), 2)}
                    for t in log_ticks(x_lo, x_hi)],
        "y_ticks": [{"value": t, "label": compact(t),
                     "y": round(box.top + box.inner_height
                                - log_scale(t, y_lo, y_hi,
                                            box.inner_height), 2)}
                    for t in log_ticks(y_lo, y_hi)],
    }


def sparkline(values: list[float], width: int = 200, height: int = 40) -> dict:
    """A small multiple panel. Its own scale, and it says so.

    Small multiples with independent scales are the sanctioned alternative to a
    second y-axis, not a dual-axis chart in disguise — but only if each panel
    labels its own maximum, which the caller must render.
    """
    clean = [v or 0 for v in values]
    if len(clean) < 2:
        return {"path": "", "area": "", "max": max(clean, default=0),
                "width": width, "height": height, "last": None}
    peak = max(clean) or 1
    step = width / (len(clean) - 1)
    points = [(round(i * step, 2),
               round(height - (v / peak) * (height - 4), 2))
              for i, v in enumerate(clean)]
    path = " ".join(f"{x},{y}" for x, y in points)
    return {
        "path": path,
        "area": f"0,{height} {path} {width},{height}",
        "max": peak, "width": width, "height": height,
        "last": points[-1], "values": clean,
    }


def bars(rows: list[tuple[str, float]]) -> list[dict]:
    """Horizontal bars, longest first, as a **percentage** of the largest.

    Percentages, not pixels. A fixed 320px bar plus a fixed 160px label cannot
    fit a 320px phone, and the row could not shrink: it forced 542px and
    scrolled the whole page sideways. Caught by the browser eval only after it
    was pointed at a populated database — against an empty one the bars had no
    width and nothing overflowed.
    """
    if not rows:
        return []
    peak = max(v for _, v in rows) or 1
    return [{"label": label, "value": value,
             "percent": round(value / peak * 100, 2)}
            for label, value in rows]
