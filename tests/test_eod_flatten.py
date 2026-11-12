"""End-of-day paper flatten and the Today's trades tab.

Prices and symbols in this file are fixture data, not market quotes.
"""

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from atlas.broker import PaperBroker
from atlas.config import LIVE_TRADING
from atlas.dashboard import PaperDashboard
from atlas.database import AtlasDatabase
from atlas.day_trades import EMPTY_DAY_NOTE, build_day_trades
from atlas.execution import execute_paper_signal
from atlas.flatten import (
    EOD_FLATTEN_REASON,
    EOD_NO_PRICE_REASON,
    flatten_open_paper_positions,
)
from atlas.market_hours import seconds_until_regular_close
from atlas.risk import RiskLimits, calculate_position_size, validate_daily_loss
from atlas.session import resolve_timeframes, run_paper_loop
from config.settings import Settings
from tests.test_paper_pipeline import recent_history
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")


def _at(day, hour, minute, second=0):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=NEW_YORK)


def session_bars(close, day=4, hour=15, minute=59):
    """Synthetic bars. ``close`` is fixture data, not a live price."""
    end = datetime(2026, 11, day, hour, minute, tzinfo=NEW_YORK)
    index = pd.date_range(end=end, periods=40, freq="min")
    closes = [float(close)] * 40
    frame = pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1_000_000] * 40,
        },
        index=index,
    )
    return {"1h": frame, "1d": frame.copy()}


def _settings():
    return Settings(instruments=("XYZ", "ABC"), paper_initial_cash=10_000)


def _book(tmp_path, name="eod.sqlite3"):
    database = AtlasDatabase(tmp_path / name)
    broker = PaperBroker(initial_cash=10_000, database=database)
    return broker, database


def _seed_long(broker, quantity=1, price=100.0, order_id="seed-long"):
    broker.submit_order(
        "XYZ",
        "BUY",
        quantity,
        price,
        order_id,
        approved=True,
        stop_loss=price - 10,
        profit_target=price + 20,
    )


def _seed_short(broker, symbol="ABC", quantity=1, price=100.0, order_id="seed-short"):
    broker.submit_order(
        symbol,
        "SELL",
        quantity,
        price,
        order_id,
        approved=True,
        stop_loss=price + 10,
        profit_target=price - 20,
    )


def _flatten(broker, database, histories, now=None):
    return flatten_open_paper_positions(
        settings=_settings(),
        broker=broker,
        database=database,
        histories_by_symbol=histories,
        now=now or _at(4, 16, 0),
        timeframes=("1h", "1d"),
    )


def test_open_long_is_sold_at_the_close_without_a_dashboard_confirm(tmp_path):
    broker, database = _book(tmp_path)
    _seed_long(broker)
    database.upsert_symbol_scan("XYZ", {
        "symbol": "XYZ",
        "decision": "WAIT",
        "buyable": False,
        "reason": "test data",
    })
    observations = len(database.list_scan_observations())

    report = _flatten(broker, database, {"XYZ": session_bars(101.0)})

    assert report["live_trading"] is False
    assert report["skipped"] is None
    order = report["flattened"][0]
    assert order["side"] == "SELL"
    assert order["effect"] == "CLOSE_LONG"
    assert order["quantity"] == 1
    assert order["price"] == 101.0
    assert order["reason"] == EOD_FLATTEN_REASON
    assert broker.positions() == {}
    stored = next(
        row for row in database.list_paper_orders()
        if str(row["order_id"]).startswith("eod-flatten-")
    )
    assert stored["order_id"].startswith("eod-flatten-")
    assert stored["reason"] == EOD_FLATTEN_REASON
    assert stored["price"] == 101.0
    scans = database.list_symbol_scans()
    assert scans[0]["decision"] == "WAIT"
    assert len(database.list_scan_observations()) == observations
    audits = [event for event in database.list_audit_events() if event["action"] == "EOD_FLATTEN"]
    assert audits[0]["detail"]["scan_decision_unchanged"] is True
    assert audits[0]["detail"]["reason"] == EOD_FLATTEN_REASON

    service = PaperDashboard(
        token="local-test-token",
        settings=_settings(),
        database=database,
        histories_by_symbol={"XYZ": session_bars(101.0)},
        news_events=[],
        hard_stop=False,
    )
    portfolio = service.portfolio()
    assert portfolio["positions"] == []
    assert portfolio["live_trading"] is False
    shown = [row for row in portfolio["recent_fills"] if row.get("reason")]
    assert shown[-1]["reason"] == EOD_FLATTEN_REASON
    assert shown[-1]["side"] == "SELL"
    trades = service.day_trades(now=_at(4, 16, 0))
    assert trades["rows"][0]["symbol"] == "XYZ"
    assert trades["rows"][0]["closed"] is True
    assert trades["rows"][0]["action"] is None
    assert trades["rows"][0]["status"] == "closed"
    assert trades["rows"][0]["price"] == 100.0
    assert trades["rows"][0]["close_price"] == 101.0
    assert trades["rows"][0]["reason"] == EOD_FLATTEN_REASON
    database.close()


def test_open_short_is_covered_at_the_close_without_a_dashboard_confirm(tmp_path):
    broker, database = _book(tmp_path, "short.sqlite3")
    _seed_short(broker)

    report = _flatten(broker, database, {"ABC": session_bars(99.0)})

    order = report["flattened"][0]
    assert order["side"] == "BUY"
    assert order["effect"] == "CLOSE_SHORT"
    assert order["quantity"] == 1
    assert order["price"] == 99.0
    assert order["reason"] == EOD_FLATTEN_REASON
    assert broker.positions() == {}
    assert next(
        row for row in database.list_paper_orders()
        if str(row["order_id"]).startswith("eod-flatten-")
    )["effect"] == "CLOSE_SHORT"
    trades = build_day_trades(
        broker.positions(),
        database.list_paper_orders(),
        _at(4, 16, 0),
    )
    assert trades["rows"][0]["side"] == "SHORT"
    assert trades["rows"][0]["closed"] is True
    assert trades["rows"][0]["action"] is None
    assert trades["rows"][0]["reason"] == EOD_FLATTEN_REASON
    database.close()


def test_no_open_position_means_no_fill(tmp_path):
    broker, database = _book(tmp_path, "flat.sqlite3")

    report = _flatten(broker, database, {"XYZ": session_bars(101.0)})

    assert report["flattened"] == []
    assert report["unfilled"] == []
    assert database.list_paper_orders() == []
    assert broker.positions() == {}
    assert broker.account_info()["cash"] == 10_000
    database.close()


def test_a_position_already_closed_by_the_user_is_not_flattened_again(tmp_path):
    broker, database = _book(tmp_path, "closed.sqlite3")
    _seed_long(broker)
    broker.submit_order(
        "XYZ",
        "SELL",
        1,
        100.0,
        "user-sell",
        approved=True,
        stop_loss=101.0,
        profit_target=90.0,
    )
    before = [row["order_id"] for row in database.list_paper_orders()]

    report = _flatten(broker, database, {"XYZ": session_bars(101.0)})

    assert report["flattened"] == []
    assert [row["order_id"] for row in database.list_paper_orders()] == before
    assert not any(order_id.startswith("eod-flatten-") for order_id in before)
    database.close()


def test_flatten_is_idempotent(tmp_path):
    broker, database = _book(tmp_path, "twice.sqlite3")
    _seed_long(broker)
    histories = {"XYZ": session_bars(101.0)}

    first = _flatten(broker, database, histories)
    second = _flatten(broker, database, histories)

    assert len(first["flattened"]) == 1
    assert second["flattened"] == []
    eod = [
        row for row in database.list_paper_orders()
        if str(row["order_id"]).startswith("eod-flatten-")
    ]
    assert len(eod) == 1
    database.close()


def test_missing_session_price_leaves_the_position_open(tmp_path):
    broker, database = _book(tmp_path, "noprice.sqlite3")
    _seed_long(broker)

    report = _flatten(broker, database, {"XYZ": session_bars(101.0, day=3)})

    assert report["flattened"] == []
    assert report["unfilled"] == [{"symbol": "XYZ", "reason": EOD_NO_PRICE_REASON}]
    assert broker.positions()["XYZ"]["quantity"] == 1
    assert len(database.list_paper_orders()) == 1
    audit = database.list_audit_events()[-1]
    assert audit["action"] == "EOD_FLATTEN_UNFILLED"
    assert audit["detail"]["reason"] == EOD_NO_PRICE_REASON
    assert audit["detail"]["scan_decision_unchanged"] is True
    database.close()


def test_flatten_closes_the_whole_position_without_changing_order_risk_caps(tmp_path):
    broker, database = _book(tmp_path, "whole.sqlite3")
    _seed_long(broker, quantity=10, price=100.0, order_id="seed-ten")

    with pytest.raises(ValueError, match="risk limit"):
        broker.submit_order(
            "XYZ",
            "SELL",
            10,
            150.0,
            "capped-sell",
            approved=True,
            stop_loss=151.0,
        )

    report = _flatten(broker, database, {"XYZ": session_bars(150.0)})

    assert report["flattened"][0]["quantity"] == 10
    assert report["flattened"][0]["price"] == 150.0
    assert broker.positions() == {}
    database.close()


def test_daily_loss_halt_still_blocks_a_new_order_while_the_close_flattens(tmp_path):
    broker, database = _book(tmp_path, "halt.sqlite3")
    _seed_long(broker, quantity=10, price=100.0, order_id="seed-halt")
    broker.update_market_price("XYZ", 70.0)
    assert validate_daily_loss(
        broker.account_info()["equity"],
        broker.daily_pnl(),
    ) is False
    with pytest.raises(PermissionError, match="Daily loss"):
        broker.submit_order(
            "QQQ",
            "BUY",
            1,
            50.0,
            "blocked",
            approved=True,
            stop_loss=40.0,
            profit_target=80.0,
        )

    report = _flatten(broker, database, {"XYZ": session_bars(70.0)})

    assert report["flattened"][0]["quantity"] == 10
    assert report["flattened"][0]["price"] == 70.0
    assert broker.positions() == {}
    limits = RiskLimits()
    assert limits.max_daily_loss_fraction == 0.03
    assert validate_daily_loss(10_000, -300, limits) is False
    database.close()


def test_mid_session_call_does_not_flatten_and_orders_still_need_approval(tmp_path):
    broker, database = _book(tmp_path, "mid.sqlite3")
    _seed_long(broker)

    report = _flatten(broker, database, {"XYZ": session_bars(101.0)}, now=_at(4, 10, 0))

    assert report["skipped"] == "open"
    assert report["flattened"] == []
    assert broker.positions()["XYZ"]["quantity"] == 1
    with pytest.raises(PermissionError, match="approval"):
        execute_paper_signal(
            {
                "instrument": "XYZ",
                "direction": "BUY",
                "entry_price": 100.0,
                "stop_loss": 90.0,
                "profit_target": 120.0,
                "market_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            broker,
            "needs-approval",
        )
    database.close()


def test_live_trading_stays_off(monkeypatch, tmp_path):
    assert LIVE_TRADING is False
    broker, database = _book(tmp_path, "live.sqlite3")
    _seed_long(broker)
    monkeypatch.setattr("atlas.flatten.LIVE_TRADING", True)

    with pytest.raises(PermissionError, match="Live trading"):
        _flatten(broker, database, {"XYZ": session_bars(101.0)})

    assert broker.positions()["XYZ"]["quantity"] == 1
    monkeypatch.setattr("atlas.flatten.LIVE_TRADING", False)

    class Enabled:
        live_trading_enabled = True
        paper_initial_cash = 10_000

    with pytest.raises(PermissionError, match="Live trading"):
        flatten_open_paper_positions(
            settings=Enabled(),
            broker=broker,
            database=database,
            histories_by_symbol={"XYZ": session_bars(101.0)},
            now=_at(4, 16, 0),
        )
    assert broker.positions()["XYZ"]["quantity"] == 1
    database.close()


def test_risk_limits_and_decision_timeframes_are_unchanged():
    limits = RiskLimits()
    assert limits.risk_per_trade_fraction == 0.01
    assert limits.max_position_fraction == 0.10
    assert limits.max_daily_loss_fraction == 0.03
    assert calculate_position_size(10_000, 100, 90, limits) == 10
    assert validate_daily_loss(10_000, -299, limits) is True
    assert validate_daily_loss(10_000, -300, limits) is False
    assert resolve_timeframes() == ("1h", "1d")
    assert "1m" not in resolve_timeframes()


def test_loop_flattens_when_it_is_running_through_the_close(tmp_path):
    broker, database = _book(tmp_path, "loop.sqlite3")
    _seed_long(broker)
    times = [_at(4, 15, 59), _at(4, 16, 0)]
    state = {"i": 0}
    sleeps = []

    def clock():
        return times[min(state["i"], len(times) - 1)]

    def sleeper(seconds):
        sleeps.append(seconds)
        if state["i"] < len(times) - 1:
            state["i"] += 1

    summary = run_paper_loop(
        max_cycles=2,
        interval_seconds=300,
        now=clock,
        sleep=sleeper,
        settings=_settings(),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": session_bars(101.0)},
        news_events=[],
        approved=False,
        timeframes=("1h", "1d"),
    )

    assert summary["live_trading"] is False
    assert sleeps[0] == pytest.approx(60)
    assert summary["cycles"][0]["ran"] is True
    assert "flatten" not in summary["cycles"][0]
    assert summary["cycles"][1]["ran"] is False
    assert summary["cycles"][1]["market"] == "after_close"
    assert summary["cycles"][1]["flatten"]["flattened"][0]["effect"] == "CLOSE_LONG"
    assert summary["cycles"][1]["flatten"]["flattened"][0]["price"] == 101.0
    assert broker.positions() == {}
    assert database.list_symbol_scans()
    assert all(
        card["decision"] != "EOD_FLATTEN"
        for card in database.list_symbol_scans()
    )
    database.close()


def test_loop_started_after_the_close_does_not_flatten(tmp_path):
    broker, database = _book(tmp_path, "late.sqlite3")
    _seed_long(broker)

    summary = run_paper_loop(
        max_cycles=1,
        interval_seconds=300,
        now=lambda: _at(4, 16, 5),
        sleep=lambda _seconds: None,
        settings=_settings(),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": session_bars(101.0)},
        news_events=[],
        approved=False,
        timeframes=("1h", "1d"),
    )

    assert summary["cycles"][0]["ran"] is False
    assert "flatten" not in summary["cycles"][0]
    assert broker.positions()["XYZ"]["quantity"] == 1
    assert not any(
        str(row["order_id"]).startswith("eod-flatten-")
        for row in database.list_paper_orders()
    )
    database.close()


def test_loop_wakes_at_the_close_and_does_not_flatten_mid_session(tmp_path):
    broker, database = _book(tmp_path, "wake.sqlite3")
    _seed_long(broker)
    sleeps = []

    summary = run_paper_loop(
        max_cycles=2,
        interval_seconds=300,
        now=lambda: _at(4, 15, 58),
        sleep=lambda seconds: sleeps.append(seconds),
        settings=_settings(),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": session_bars(101.0)},
        news_events=[],
        approved=False,
        timeframes=("1h", "1d"),
    )

    assert sleeps == [pytest.approx(120)]
    assert [cycle["ran"] for cycle in summary["cycles"]] == [True, True]
    assert all("flatten" not in cycle for cycle in summary["cycles"])
    assert broker.positions()["XYZ"]["quantity"] == 1
    assert seconds_until_regular_close(_at(4, 15, 58)) == pytest.approx(120)
    assert seconds_until_regular_close(_at(4, 16, 0)) is None
    assert seconds_until_regular_close(_at(7, 12, 0)) is None
    database.close()


def test_today_trades_lists_open_positions_separately_and_hides_sell_when_closed(tmp_path):
    broker, database = _book(tmp_path, "rows.sqlite3")
    _seed_long(broker)
    _seed_short(broker)
    now = datetime.now(timezone.utc)

    open_rows = build_day_trades(broker.positions(), database.list_paper_orders(), now)
    assert open_rows["note"] != EMPTY_DAY_NOTE
    by_symbol = {row["symbol"]: row for row in open_rows["rows"]}
    assert set(by_symbol) == {"ABC", "XYZ"}
    assert by_symbol["XYZ"]["side"] == "LONG"
    assert by_symbol["XYZ"]["action"] == "sell"
    assert by_symbol["XYZ"]["closed"] is False
    assert by_symbol["XYZ"]["quantity"] == 1
    assert by_symbol["XYZ"]["price"] == 100.0
    assert by_symbol["ABC"]["side"] == "SHORT"
    assert by_symbol["ABC"]["action"] == "cover"
    assert by_symbol["ABC"]["price"] == 100.0

    broker.submit_order(
        "XYZ",
        "SELL",
        1,
        100.0,
        "user-closed-xyz",
        approved=True,
        stop_loss=101.0,
        profit_target=90.0,
    )
    mixed = build_day_trades(broker.positions(), database.list_paper_orders(), now)
    mixed_rows = {row["symbol"]: row for row in mixed["rows"]}
    assert mixed_rows["XYZ"]["closed"] is True
    assert mixed_rows["XYZ"]["action"] is None
    assert mixed_rows["XYZ"]["status"] == "closed"
    assert mixed_rows["ABC"]["closed"] is False
    assert mixed_rows["ABC"]["action"] == "cover"
    database.close()


def test_empty_day_is_honest():
    report = build_day_trades({}, [], _at(4, 12, 0))
    assert report["rows"] == []
    assert report["note"] == "No paper positions or same-day trades."
    assert report["note"] == EMPTY_DAY_NOTE


def test_today_trades_sell_and_cover_still_require_confirm_and_do_not_flip(tmp_path, monkeypatch):
    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert 'name="atlas-dashboard-version" content="29"' in page
    assert 'data-tab="trades"' in page
    assert 'data-panel="trades"' in page
    assert "No paper positions or same-day trades." in page
    assert "data-day-sell" in page
    assert "data-day-cover" in page
    assert "askSell(button.dataset.daySell, true)" in page
    assert "askBuy(button.dataset.dayCover, true)" in page
    sell = page[page.index("async function askSell"):page.index("async function askAlert")]
    buy = page[page.index("async function askBuy"):page.index("async function askSell")]
    assert sell.index("confirmTrade") < sell.index('post("/api/sell"')
    assert buy.index("confirmTrade") < buy.index('post("/api/buy"')
    assert "close_only: true" in sell
    assert "cover_only: true" in buy

    frame = recent_history(100, 0)
    database = AtlasDatabase(tmp_path / "confirm.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(instruments=("XYZ",), paper_initial_cash=10_000),
        database=database,
        histories_by_symbol={"XYZ": {"1h": frame, "1d": frame.copy()}},
        news_events=[],
        hard_stop=False,
    )
    with pytest.raises(PermissionError, match="token"):
        service.sell("wrong-token", "XYZ", close_only=True)
    with pytest.raises(PermissionError, match="token"):
        service.buy("wrong-token", "XYZ", cover_only=True)

    monkeypatch.setattr(
        service,
        "_preview",
        lambda symbol, broker, histories: {
            "decision": "SELL",
            "recommendation": {
                "instrument": "XYZ",
                "direction": "SELL",
                "entry_price": 100.0,
                "stop_loss": 110.0,
                "profit_target": 80.0,
                "market_timestamp": datetime.now(timezone.utc).isoformat(),
            },
        },
    )
    with pytest.raises(ValueError, match="does not open a short"):
        service.sell(service.token, "XYZ", close_only=True)
    assert service._broker().positions() == {}

    opened = service.sell(service.token, "XYZ")
    assert opened["order"]["effect"] == "OPEN_SHORT"
    covered = service.buy(service.token, "XYZ", cover_only=True)
    assert covered["order"]["effect"] == "CLOSE_SHORT"
    assert service._broker().positions() == {}

    monkeypatch.setattr(
        service,
        "_preview",
        lambda symbol, broker, histories: {
            "decision": "BUY",
            "recommendation": {
                "instrument": "XYZ",
                "direction": "BUY",
                "entry_price": 100.0,
                "stop_loss": 90.0,
                "profit_target": 120.0,
                "market_timestamp": datetime.now(timezone.utc).isoformat(),
            },
        },
    )
    with pytest.raises(ValueError, match="does not open a long"):
        service.buy(service.token, "XYZ", cover_only=True)
    assert service._broker().positions() == {}
    database.close()
