from pathlib import Path

from atlas.dashboard import chart_tooltip_fields


def test_tooltip_uses_only_fields_on_the_bar():
    bar = {
        "time": "2026-11-06T23:49:00+00:00",
        "open": 735.29,
        "high": 736.10,
        "low": 734.80,
        "close": 735.83,
        "volume": 0,
        "sma": 730.5,
        "ema": None,
    }
    rows = chart_tooltip_fields(bar, {"sma": 20, "ema": 20, "rsi": 14})
    by_label = {row["label"]: row["value"] for row in rows}

    assert by_label["Time"] == bar["time"]
    assert by_label["Open"] == 735.29
    assert by_label["High"] == 736.10
    assert by_label["Low"] == 734.80
    assert by_label["Close"] == 735.83
    assert by_label["Volume"] == 0
    assert by_label["SMA 20"] == 730.5
    assert "EMA 20" not in by_label
    assert "RSI 14" not in by_label
    assert set(by_label) <= {"Time", "Open", "High", "Low", "Close", "Volume", "SMA 20"}


def test_tooltip_omits_a_missing_volume_instead_of_zero():
    rows = chart_tooltip_fields({
        "time": "2026-11-06T00:00:00+00:00",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": None,
        "sma": None,
        "ema": None,
        "rsi": None,
    })
    labels = [row["label"] for row in rows]
    assert labels == ["Time", "Open", "High", "Low", "Close"]
    assert all(row["value"] != 0 for row in rows)


def test_page_has_the_floating_tooltip_and_cache_version():
    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert 'id="chart-tooltip"' in page
    assert 'name="atlas-dashboard-version" content="27"' in page
    assert "#action-buy, #action-sell" in page
    assert "padding: 14px 28px" in page
    assert "function tooltipRows" in page
    assert "hideTooltip" in page
    assert "bar.volume || 0" not in page
    assert "bar.open || 0" not in page
    assert 'buyOn ? "" : "disabled"' in page
    assert 'sellOn ? "" : "disabled"' in page
