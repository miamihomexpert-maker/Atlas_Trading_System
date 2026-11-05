from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from atlas.broker import PaperBroker
from atlas.execution import execute_paper_signal
from atlas.risk import RiskLimits, calculate_position_size, validate_daily_loss
from atlas.signals import create_trade_signal, signal_from_analysis
from atlas.strategy import evaluate_market
from config.settings import Settings, load_settings


def current_timestamp(seconds_ago=0):
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat()


def make_buy_signal(**overrides):
    signal = {
        "instrument": "XYZ",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "profit_target": 120.0,
        "risk_reward_ratio": 2.0,
        "explanation": "Test recommendation",
        "timestamp": current_timestamp(),
        "market_timestamp": current_timestamp(),
    }
    signal.update(overrides)
    return signal


def test_evaluate_market_without_indicators_returns_result():
    history = pd.DataFrame(
        {
            "Open": range(100, 140),
            "High": range(100, 140),
            "Low": range(100, 140),
            "Close": range(100, 140),
            "Volume": [1_000_000] * 40,
        }
    )

    result = signal_from_analysis("xyz", evaluate_market(history))

    assert result is not None
    assert result["instrument"] == "XYZ"
    assert result["direction"] in {"BUY", "SELL"}
    assert result["risk_reward_ratio"] == pytest.approx(2.0)


def test_strategy_rejects_non_finite_ohlcv():
    history = pd.DataFrame(
        {
            "Open": [1.0] * 20,
            "High": [1.0] * 20,
            "Low": [1.0] * 20,
            "Close": [1.0] * 19 + [float("inf")],
            "Volume": [1_000] * 20,
        }
    )

    result = evaluate_market(history)

    assert result["status"] == "INVALID_DATA"
    assert result["signal"] == "WAIT"


def test_position_size_obeys_risk_and_exposure_caps():
    limits = RiskLimits(
        risk_per_trade_fraction=0.01,
        max_position_fraction=0.10,
    )

    quantity = calculate_position_size(
        equity=10_000,
        entry_price=100,
        stop_loss=90,
        limits=limits,
    )

    assert quantity == 10
    assert validate_daily_loss(10_000, -299, limits)
    assert not validate_daily_loss(10_000, -300, limits)


def test_paper_broker_requires_approval_and_prevents_duplicate_orders():
    broker = PaperBroker(initial_cash=10_000)

    with pytest.raises(PermissionError):
        broker.submit_order("XYZ", "BUY", 5, 100, "order-1")

    order = broker.submit_order(
        "XYZ",
        "BUY",
        5,
        100,
        "order-1",
        approved=True,
    )
    duplicate = broker.submit_order(
        "XYZ",
        "BUY",
        5,
        100,
        "order-1",
        approved=True,
    )

    assert order == duplicate
    assert broker.positions()["XYZ"]["quantity"] == 5
    assert broker.account_info()["cash"] == 9_500
    with pytest.raises(ValueError, match="different order"):
        broker.submit_order(
            "XYZ",
            "BUY",
            4,
            100,
            "order-1",
            approved=True,
        )


def test_execution_requires_approval_and_fresh_market_data():
    broker = PaperBroker(initial_cash=10_000)
    signal = make_buy_signal()

    with pytest.raises(PermissionError, match="approval"):
        execute_paper_signal(signal, broker, "order-1")

    with pytest.raises(PermissionError, match="stale"):
        execute_paper_signal(
            make_buy_signal(market_timestamp=current_timestamp(901)),
            broker,
            "order-2",
            approved=True,
        )

    order = execute_paper_signal(
        signal,
        broker,
        "order-3",
        approved=True,
    )
    assert order["status"] == "FILLED"
    assert order["quantity"] == 10
    assert broker.account_info()["trading_mode"] == "PAPER"


def test_risk_sized_sell_can_reduce_a_position_at_exposure_limit():
    broker = PaperBroker(initial_cash=1_000)
    broker.submit_order(
        "XYZ",
        "BUY",
        10,
        10,
        "entry",
        approved=True,
    )
    signal = {
        "instrument": "XYZ",
        "direction": "SELL",
        "entry_price": 10,
        "stop_loss": 11,
        "profit_target": 8,
        "market_timestamp": current_timestamp(),
    }

    order = execute_paper_signal(
        signal,
        broker,
        "exit",
        approved=True,
    )

    assert order["side"] == "SELL"
    assert order["quantity"] == 10
    assert broker.positions() == {}


def test_configuration_cannot_enable_live_trading(monkeypatch):
    with pytest.raises(ValueError, match="must remain disabled"):
        Settings(live_trading_enabled=True)

    monkeypatch.setenv("ATLAS_LIVE_TRADING", "true")
    with pytest.raises(ValueError, match="deliberately disabled"):
        load_settings()

    monkeypatch.setenv("ATLAS_LIVE_TRADING", "false")
    monkeypatch.setenv("ATLAS_INSTRUMENTS", ", ")
    with pytest.raises(ValueError, match="At least one instrument"):
        load_settings()


def test_trade_signal_rejects_invalid_stop_orientation():
    with pytest.raises(ValueError, match="BUY signals"):
        create_trade_signal(
            "XYZ",
            "BUY",
            100,
            105,
            120,
            "Invalid stop",
        )
