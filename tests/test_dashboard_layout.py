from pathlib import Path

from atlas.dashboard_layout import DEFAULT_LAYOUT, LAYOUT_KEY, clamp_layout


def test_default_chart_is_larger_than_an_even_split():
    layout = clamp_layout(None)
    assert layout == DEFAULT_LAYOUT
    assert layout["chart_ratio"] == 0.68
    assert layout["chart_ratio"] > 0.5


def test_saved_sizes_are_clamped_and_junk_falls_back():
    assert clamp_layout({"left": 10, "right": 9000, "chart_ratio": 2}) == {
        "left": 200,
        "right": 560,
        "chart_ratio": 0.88,
    }
    assert clamp_layout({"left": "wide", "right": None, "chartRatio": "tall"}) == DEFAULT_LAYOUT
    assert clamp_layout({"chartRatio": 0.5}) == {
        "left": DEFAULT_LAYOUT["left"],
        "right": DEFAULT_LAYOUT["right"],
        "chart_ratio": 0.5,
    }
    kept = clamp_layout({"left": 400, "right": 360, "chart_ratio": 0.8})
    assert kept == {"left": 400, "right": 360, "chart_ratio": 0.8}


def test_page_has_the_splitters_and_keeps_the_trade_buttons():
    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert LAYOUT_KEY in page
    assert "chartRatio: 0.68" in page
    assert "left: 260" in page
    assert "right: 300" in page
    assert 'data-split="chart"' in page
    assert 'data-split="left"' in page
    assert 'data-split="right"' in page
    assert "localStorage" in page
    assert "scheduleChartRedraw" in page
    assert 'id="pane-chart"' in page
    assert 'id="company"' in page
    assert 'id="clock"' in page
    assert 'id="refresh-now"' in page
    assert 'id="technicals"' in page
    assert 'id="risk"' in page
    assert 'id="action-buy"' in page
    assert 'id="action-sell"' in page
    assert 'scan.buyable ? "" : "disabled"' in page
    assert "No open paper position." in page
