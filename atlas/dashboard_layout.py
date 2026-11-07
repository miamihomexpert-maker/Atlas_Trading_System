"""Pane sizes for the paper dashboard.

The page stores the same numbers in localStorage under ``LAYOUT_KEY``.
Invalid saved values fall back to the defaults or the nearest limit.
Nothing here is market data.
"""

import math


LAYOUT_KEY = "atlas-paper-layout"
DEFAULT_LAYOUT = {
    "left": 260,
    "right": 300,
    "chart_ratio": 0.68,
}
LAYOUT_LIMITS = {
    "left": (200, 520),
    "right": (240, 560),
    "chart_ratio": (0.35, 0.88),
}


def clamp_layout(saved=None):
    """Return left width, right width, and the chart's share of the center.

    ``chart_ratio`` is the fraction of the center column given to the candle
    chart. The default is larger than an even split with the panels below it.
    """
    source = saved if isinstance(saved, dict) else {}
    return {
        "left": _clamp(source, "left", "left"),
        "right": _clamp(source, "right", "right"),
        "chart_ratio": _clamp(source, "chart_ratio", "chartRatio"),
    }


def _clamp(source, key, alias):
    default = DEFAULT_LAYOUT[key]
    low, high = LAYOUT_LIMITS[key]
    raw = source.get(key, source.get(alias, default))
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(high, max(low, number))
