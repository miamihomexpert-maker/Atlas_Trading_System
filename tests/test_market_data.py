from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from atlas.market_data import (
    get_multi_timeframe_history,
    normalize_ohlcv,
    resample_ohlcv,
)
from atlas.news import NewsEvent, apply_news_context, assess_news_impact


def test_normalize_ohlcv_sorts_and_keeps_last_duplicate():
    times = pd.to_datetime(
        [
            "2025-01-02 09:35",
            "2025-01-02 09:30",
            "2025-01-02 09:35",
        ]
    )
    history = pd.DataFrame(
        {
            "open": [102, 100, 103],
            "high": [103, 101, 104],
            "low": [101, 99, 102],
            "close": [102, 100, 103],
            "volume": [20, 10, 30],
        },
        index=times,
    )

    result = normalize_ohlcv(history)

    assert result.index.is_monotonic_increasing
    assert len(result) == 2
    assert result.iloc[-1]["Close"] == 103
    assert result.iloc[-1]["Volume"] == 30


def test_resample_ohlcv_aggregates_correct_bar_fields():
    index = pd.date_range("2025-01-02 09:30", periods=10, freq="min")
    history = pd.DataFrame(
        {
            "Open": range(100, 110),
            "High": range(101, 111),
            "Low": range(99, 109),
            "Close": range(100, 110),
            "Volume": [100] * 10,
        },
        index=index,
    )

    result = resample_ohlcv(history, "5m")

    assert len(result) == 2
    assert result.iloc[0].to_dict() == {
        "Open": 100,
        "High": 105,
        "Low": 99,
        "Close": 104,
        "Volume": 500,
    }


def test_normalize_keeps_valid_bars_and_drops_an_invalid_one():
    index = pd.date_range("2026-11-10 14:00", periods=2, freq="h", tz="UTC")
    history = pd.DataFrame(
        {
            "Open": [10, 12],
            "High": [11, 11],
            "Low": [9, 13],
            "Close": [10.5, 12],
            "Volume": [100, 50],
        },
        index=index,
    )

    result = normalize_ohlcv(history)

    assert len(result) == 1
    assert result.iloc[0].to_dict() == {
        "Open": 10,
        "High": 11,
        "Low": 9,
        "Close": 10.5,
        "Volume": 100,
    }


def test_normalize_rejects_bad_ohlcv():
    history = pd.DataFrame(
        {
            "Open": [10],
            "High": [9],
            "Low": [8],
            "Close": [9],
            "Volume": [100],
        }
    )
    with pytest.raises(ValueError, match="invalid OHLCV"):
        normalize_ohlcv(history)


def test_stock_history_returns_valid_bars_when_one_bar_is_invalid(monkeypatch):
    index = pd.date_range("2026-11-10 14:00", periods=2, freq="h", tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [10, 12],
            "High": [11, 11],
            "Low": [9, 13],
            "Close": [10.5, 12],
            "Volume": [100, 50],
        },
        index=index,
    )

    class FakeTicker:
        def __init__(self, symbol):
            assert symbol == "XYZ"

        def history(self, period, interval):
            return frame

    monkeypatch.setattr("atlas.market_data.yf.Ticker", FakeTicker)
    from atlas.market_data import get_stock_history

    result = get_stock_history("XYZ", period="5d", interval="1h")

    assert list(result["Close"]) == [10.5]


def test_stock_history_returns_none_when_every_bar_is_invalid(monkeypatch):
    frame = pd.DataFrame(
        {"Open": [10], "High": [9], "Low": [8], "Close": [9], "Volume": [100]}
    )

    class FakeTicker:
        def __init__(self, symbol):
            pass

        def history(self, period, interval):
            return frame

    monkeypatch.setattr("atlas.market_data.yf.Ticker", FakeTicker)
    from atlas.market_data import get_stock_history

    assert get_stock_history("XYZ", period="5d", interval="1h") is None


def test_multi_timeframe_fetch_normalizes_each_requested_interval(monkeypatch):
    received = []
    history = pd.DataFrame(
        {
            "Open": [10],
            "High": [11],
            "Low": [9],
            "Close": [10],
            "Volume": [100],
        },
        index=pd.date_range("2025-01-02", periods=1),
    )

    class FakeTicker:
        def __init__(self, symbol):
            assert symbol == "XYZ"

        def history(self, period, interval):
            received.append(interval)
            return history

    monkeypatch.setattr("atlas.market_data.yf.Ticker", FakeTicker)
    result = get_multi_timeframe_history(
        "xyz",
        timeframes=("5m", "1h"),
        period="5d",
    )

    assert received == ["5m", "1h"]
    assert set(result) == {"5m", "1h"}
    assert all(list(bars.columns) == ["Open", "High", "Low", "Close", "Volume"] for bars in result.values())


def test_news_context_ignores_stale_events_and_abstains_on_adverse_impact():
    now = datetime.now(timezone.utc)
    event = NewsEvent(
        event_id="fed-1",
        title="Unexpected policy tightening",
        category="FED",
        published_at=now - timedelta(hours=1),
        sentiment=-0.9,
        impact=0.9,
        instruments=(),
    )
    impact = assess_news_impact([event], "SPY", now=now)
    analysis = apply_news_context(
        {"signal": "BUY", "confidence": 0.8, "reason": "Technical setup."},
        impact,
    )

    assert impact["event_count"] == 1
    assert analysis["signal"] == "WAIT"
    assert analysis["confidence"] < 0.8
    assert "opposing news" in analysis["reason"]
