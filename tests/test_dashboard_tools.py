from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from atlas.broker import PaperBroker
from atlas.dashboard import (
    PaperDashboard,
    _risk_usage,
    _state_payload,
    describe_technicals,
)
from atlas.database import AtlasDatabase
from atlas.market_hours import session_clock
from atlas.technical import calculate_ema, calculate_rsi, calculate_sma
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


NEW_YORK = ZoneInfo("America/New_York")


def _bars(start, step):
    frame = recent_history(start, step)
    return {"1h": frame, "1d": frame.copy()}


def _service(tmp_path, histories, symbols=("XYZ",)):
    database = AtlasDatabase(tmp_path / "tools.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(instruments=symbols, paper_initial_cash=10_000, live_trading_enabled=False),
        database=database,
        histories_by_symbol=histories,
        news_events=[],
        hard_stop=False,
        candle_loader=lambda symbol, timeframe: histories[symbol][timeframe],
    )
    return service, database


def test_session_clock_counts_to_the_next_regular_boundary():
    opened = session_clock(datetime(2026, 11, 4, 10, 0, tzinfo=NEW_YORK))
    assert opened["timezone"] == "America/New_York"
    assert opened["status"] == "open"
    assert opened["status_label"] == "Regular session is open"
    assert opened["next_event"] == "close"
    assert opened["seconds_until_next"] == 6 * 60 * 60
    assert opened["next_event_at"].startswith("2026-11-04T16:00:00")

    before = session_clock(datetime(2026, 11, 4, 9, 0, tzinfo=NEW_YORK))
    assert before["status"] == "before_open"
    assert before["status_label"] == "Before the open"
    assert before["next_event"] == "open"
    assert before["seconds_until_next"] == 30 * 60

    after = session_clock(datetime(2026, 11, 4, 16, 0, tzinfo=NEW_YORK))
    assert after["status"] == "after_close"
    assert after["next_event"] == "open"
    assert after["seconds_until_next"] == 17 * 60 * 60 + 30 * 60
    assert after["next_event_at"].startswith("2026-11-05T09:30:00")

    weekend = datetime(2026, 11, 7, 12, 0, tzinfo=NEW_YORK)
    saturday = session_clock(weekend)
    monday = datetime(2026, 11, 9, 9, 30, tzinfo=NEW_YORK)
    assert saturday["status"] == "weekend"
    assert saturday["seconds_until_next"] == int((monday - weekend).total_seconds())

    friday = session_clock(datetime(2026, 11, 6, 16, 0, tzinfo=NEW_YORK))
    assert friday["status"] == "after_close"
    assert friday["next_event_at"].startswith("2026-11-09T09:30:00")
    assert "not marked" in friday["holiday_note"]
    assert friday["status"] in {"weekend", "before_open", "open", "after_close"}


def test_chart_indicators_match_the_returned_bars(tmp_path):
    frame = recent_history(100, 1, rows=40)
    service, database = _service(tmp_path, {"XYZ": {"1h": frame, "1d": frame.copy()}})
    payload = service.candles("XYZ", "1h")
    shown = frame.tail(300)
    assert payload["indicators"] == {"sma": 20, "ema": 20, "rsi": 14}
    assert payload["error"] is None
    for key, series in (
        ("sma", calculate_sma(shown)),
        ("ema", calculate_ema(shown)),
        ("rsi", calculate_rsi(shown)),
    ):
        for candle, value in zip(payload["candles"], series):
            if pd.isna(value):
                assert candle[key] is None
            else:
                assert candle[key] == pytest.approx(float(value))
    assert payload["candles"][0]["sma"] is None
    assert payload["candles"][-1]["sma"] == pytest.approx(float(calculate_sma(shown).iloc[-1]))
    assert payload["candles"][-1]["volume"] == 1_000_000
    assert payload["candles"][-1]["close"] == frame["Close"].iloc[-1]
    database.close()


def test_technicals_repeat_scan_fields_and_name_the_gaps():
    copy = describe_technicals({
        "timeframes": {
            "1h": {
                "trend": "BEARISH",
                "rsi": None,
                "macd": "NEUTRAL",
                "bullish_score": 2,
                "bearish_score": 3,
                "breakout": None,
                "vwap": "BELOW_VWAP",
            },
        },
    })
    text = " ".join(copy["lines"])
    assert "Trend on the 1 hour chart: BEARISH." in copy["lines"]
    assert "The 1 hour scan did not return RSI." in copy["lines"]
    assert "The 1 hour scan did not return Breakout." in copy["lines"]
    assert "Reasons to buy on the 1 hour chart: 2." in copy["lines"]
    assert "Reasons to sell on the 1 hour chart: 3." in copy["lines"]
    assert "99.99" not in text

    assert describe_technicals({"timeframes": {}})["lines"] == [
        "The scan did not return technical observations for this symbol.",
    ]


def test_risk_panel_uses_the_paper_account_and_configured_limits(tmp_path):
    usage = _risk_usage({"cash": 9_000, "equity": 10_000}, -100, 500, False)
    assert usage["per_trade"] == 0.01
    assert usage["max_exposure"] == 0.10
    assert usage["daily_loss"] == 0.03
    assert usage["loss_limit"] == pytest.approx(300)
    assert usage["loss_used_fraction"] == pytest.approx(0.01)
    assert usage["exposure_fraction"] == pytest.approx(0.05)
    assert usage["halted"] is False

    histories = {"XYZ": _bars(100, 1)}
    service, database = _service(tmp_path, histories)
    broker = PaperBroker(initial_cash=10_000, database=database)
    broker.submit_order(
        "XYZ",
        "BUY",
        1,
        100.0,
        "tools-risk-1",
        approved=True,
        stop_loss=90.0,
        profit_target=120.0,
    )
    book = service.portfolio()
    assert book["risk"]["exposure"] == pytest.approx(100)
    assert book["risk"]["equity"] == pytest.approx(10_000)
    assert book["risk"]["per_trade"] == 0.01
    assert book["risk"]["max_exposure"] == 0.10
    assert book["risk"]["daily_loss"] == 0.03
    assert book["live_trading"] is False
    database.close()


def test_tools_do_not_enable_a_wait_or_invent_a_buy(tmp_path):
    histories = {"XYZ": {"1h": recent_history(100, 1), "1d": recent_history(140, -1)}}
    service, database = _service(tmp_path, histories)
    ranked = service.candidates()
    card = ranked["scans"][0]
    assert card["symbol"] == "XYZ"
    assert card["decision"] == "WAIT"
    assert card["buyable"] is False
    assert card["technical_copy"]["lines"]
    assert any("did not return" in line or "chart:" in line for line in card["technical_copy"]["lines"])
    payload = _state_payload(service)
    assert payload["clock"]["timezone"] == "America/New_York"
    assert payload["clock"]["status"] in {"weekend", "before_open", "open", "after_close"}
    assert payload["portfolio"]["risk"]["per_trade"] == 0.01
    with pytest.raises(ValueError, match="not a current BUY"):
        service.buy(service.token, "XYZ")
    assert service._broker().positions() == {}
    database.close()

    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert 'id="refresh-now"' in page
    assert 'id="clock"' in page
    assert 'id="technicals"' in page
    assert 'id="risk"' in page
    assert 'id="chart-hover"' in page
    assert 'id="company"' in page
    assert 'id="action-buy"' in page
    assert 'id="action-sell"' in page
    assert 'buyOn ? "" : "disabled"' in page
    assert 'sellOn ? "" : "disabled"' in page
    assert "No open paper position." in page
    assert "company_copy" in page
