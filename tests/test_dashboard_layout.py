from pathlib import Path

from atlas.dashboard_layout import (
    DEFAULT_LAYOUT,
    DEFAULT_TAB,
    LAYOUT_KEY,
    TAB_IDS,
    TAB_KEY,
    clamp_layout,
    clamp_tab,
)


def test_chart_is_not_sized_by_a_lower_panel_ratio():
    layout = clamp_layout(None)
    assert layout == DEFAULT_LAYOUT
    assert "chart_ratio" not in layout
    assert layout["left"] >= 200
    assert layout["right"] >= 240


def test_saved_widths_are_clamped_and_junk_falls_back():
    assert clamp_layout({"left": 10, "right": 9000, "reader": 20}) == {
        "left": 200,
        "right": 560,
        "reader": 280,
    }
    assert clamp_layout({"left": "wide", "right": None, "reader": "tall"}) == DEFAULT_LAYOUT
    assert clamp_layout({"chart_ratio": 0.2, "chartRatio": 0.9}) == DEFAULT_LAYOUT
    assert clamp_layout({"left": 400, "right": 360, "reader": 420}) == {
        "left": 400,
        "right": 360,
        "reader": 420,
    }


def test_extra_tabs_start_closed_and_reject_unknown_names():
    assert clamp_tab(None) == DEFAULT_TAB
    assert DEFAULT_TAB["open"] is False
    assert clamp_tab({"open": True, "tab": "company"}) == {"open": True, "tab": "company"}
    assert clamp_tab({"open": True, "tab": "sells"}) == {"open": True, "tab": "sells"}
    assert clamp_tab({"open": "yes", "tab": "buy"}) == DEFAULT_TAB
    assert clamp_tab({"open": False, "tab": "technicals"}) == {
        "open": False,
        "tab": "technicals",
    }
    assert "buy" not in TAB_IDS
    assert "sell" not in TAB_IDS


def test_page_keeps_the_chart_and_trade_buttons_outside_the_tabs():
    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert LAYOUT_KEY in page
    assert TAB_KEY in page
    assert "left: 260" in page
    assert "right: 320" in page
    assert "reader: 380" in page
    assert 'data-split="left"' in page
    assert 'data-split="right"' in page
    assert 'data-split="chart"' not in page
    assert 'id="pane-lower"' not in page
    assert 'id="reader-close"' in page
    assert "scheduleChartRedraw" in page
    for tab in TAB_IDS:
        assert f'data-tab="{tab}"' in page
        assert f'data-panel="{tab}"' in page
    chart = page.index('id="pane-chart"')
    reader = page.index('id="pane-reader"')
    actions = page.index('id="trade-actions"')
    watch = page.index('id="watchlist"')
    assert watch < chart < reader
    assert actions > reader
    reader_html = page[reader:actions]
    assert 'id="signals"' in reader_html
    assert 'id="technicals"' in reader_html
    assert 'id="company"' in reader_html
    assert 'id="risk"' in reader_html
    assert 'id="alerts"' in reader_html
    assert "trade-actions" not in reader_html
    assert 'id="action-buy"' in page
    assert 'id="action-sell"' in page
    assert 'buyOn ? "" : "disabled"' in page
    assert 'sellOn ? "" : "disabled"' in page
    assert "No open paper position." in page
    assert "function decisionClass" in page
    assert 'decision === "BUY"' in page
    assert "decisionClass(scan.decision)" in page
    assert "NEXT STEP · ${scan.symbol}" in page
    assert 'id="clock"' in page
    assert 'id="refresh-now"' in page
