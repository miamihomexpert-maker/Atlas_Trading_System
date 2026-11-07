"""Pane sizes and the extra-reading tab for the paper dashboard.

The chart fills the center. Side widths are the watchlist and the trade list.
The reading pane is closed until a tab is opened, so Signals and Technicals
cannot take the chart's height. Nothing here is market data.
"""

import math


LAYOUT_KEY = "atlas-paper-layout"
TAB_KEY = "atlas-paper-tab"
DEFAULT_LAYOUT = {
    "left": 260,
    "right": 320,
    "reader": 380,
}
LAYOUT_LIMITS = {
    "left": (200, 520),
    "right": (240, 560),
    "reader": (280, 640),
}
TAB_IDS = (
    "signals",
    "technicals",
    "news",
    "model",
    "company",
    "risk",
    "sells",
    "review",
)
DEFAULT_TAB = {"open": False, "tab": "signals"}


def clamp_layout(saved=None):
    """Return watchlist width, trade-list width, and reading-pane width."""
    source = saved if isinstance(saved, dict) else {}
    return {
        "left": _clamp_number(source, "left", "left"),
        "right": _clamp_number(source, "right", "right"),
        "reader": _clamp_number(source, "reader", "reader"),
    }


def clamp_tab(saved=None):
    """Return which extra panel is selected, and whether it is open.

    The default is closed. An unknown tab name falls back to Signals and
    stays closed. Buy, Sell, the chart, and the watchlist are not tabs.
    """
    source = saved if isinstance(saved, dict) else {}
    tab = source.get("tab")
    if tab not in TAB_IDS:
        tab = DEFAULT_TAB["tab"]
    opened = source.get("open")
    if not isinstance(opened, bool):
        opened = False
    return {"open": opened, "tab": tab}


def _clamp_number(source, key, alias):
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
