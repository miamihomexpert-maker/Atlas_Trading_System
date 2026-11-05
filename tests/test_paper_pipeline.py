from datetime import datetime, timezone

import pandas as pd
import pytest

from atlas.broker import PaperBroker
from atlas.config import LIVE_TRADING
from atlas.news import NewsEvent
from atlas.pipeline import run_paper_pipeline
from atlas.risk import calculate_position_size


def recent_history(start, step, rows=40):
    closes = [start + index * step for index in range(rows)]
    index = pd.date_range(
        end=datetime.now(timezone.utc),
        periods=rows,
        freq="min",
    )
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1_000_000] * rows,
        },
        index=index,
    )


def test_paper_pipeline_submits_an_approved_recommendation_inside_risk_caps():
    histories = {
        "5m": recent_history(100, 1),
        "1h": recent_history(100, 1),
    }
    broker = PaperBroker(initial_cash=10_000)

    result = run_paper_pipeline(
        "xyz",
        histories,
        broker,
        "paper-1",
        approved=True,
    )

    order = result["order"]
    assert result["execution"] == "FILLED"
    assert result["recommendation"]["direction"] == "BUY"
    assert order["status"] == "FILLED"
    assert order["symbol"] == "XYZ"
    capped = calculate_position_size(
        10_000,
        order["price"],
        result["recommendation"]["stop_loss"],
    )
    assert order["quantity"] == capped
    assert order["quantity"] > 0
    assert broker.positions()["XYZ"]["quantity"] == capped
    assert not LIVE_TRADING

    with pytest.raises(PermissionError, match="no shares"):
        run_paper_pipeline(
            "XYZ",
            histories,
            broker,
            "paper-2",
            approved=True,
        )
    assert broker.positions()["XYZ"]["quantity"] == capped


def test_paper_pipeline_does_not_trade_without_approval_or_a_signal():
    broker = PaperBroker(initial_cash=10_000)
    rising = recent_history(100, 1)
    falling = recent_history(140, -1)

    with pytest.raises(PermissionError, match="approval"):
        run_paper_pipeline(
            "XYZ",
            {"5m": rising, "1h": rising},
            broker,
            "needs-approval",
        )

    waited = run_paper_pipeline(
        "XYZ",
        {"5m": rising, "1d": falling},
        broker,
        "no-signal",
        approved=True,
    )

    assert waited["recommendation"] is None
    assert waited["order"] is None
    assert waited["execution"] == "NOT_SUBMITTED"
    assert broker.positions() == {}
    assert broker.trade_history() == []


def test_remote_approval_can_be_required_without_enabling_live_trading(monkeypatch):
    histories = {
        "5m": recent_history(100, 1),
        "1h": recent_history(100, 1),
    }
    broker = PaperBroker(initial_cash=10_000)

    with pytest.raises(PermissionError, match="Remote approval"):
        run_paper_pipeline(
            "XYZ",
            histories,
            broker,
            "remote-required",
            approved=True,
            require_remote_approval=True,
        )

    monkeypatch.setenv("ATLAS_REQUIRE_REMOTE_APPROVAL", "true")
    with pytest.raises(PermissionError, match="Remote approval"):
        run_paper_pipeline(
            "XYZ",
            histories,
            broker,
            "remote-env",
            approved=True,
        )

    monkeypatch.setattr("atlas.pipeline.LIVE_TRADING", True)
    with pytest.raises(PermissionError, match="Live trading"):
        run_paper_pipeline(
            "XYZ",
            histories,
            broker,
            "live-blocked",
            approved=True,
        )
    assert broker.positions() == {}


def test_news_and_a_disagreeing_model_withhold_the_paper_order():
    histories = {
        "1h": recent_history(100, 1),
        "1d": recent_history(100, 1),
    }
    broker = PaperBroker(initial_cash=10_000)
    news = [
        NewsEvent(
            event_id="adverse-1",
            title="Operator supplied opposing item",
            category="EARNINGS",
            published_at=datetime.now(timezone.utc),
            sentiment=-1.0,
            impact=1.0,
            instruments=("XYZ",),
        )
    ]

    vetoed = run_paper_pipeline(
        "XYZ",
        histories,
        broker,
        "news-veto",
        approved=True,
        news_events=news,
    )

    assert vetoed["technical_signal"] == "BUY"
    assert vetoed["decision"] == "WAIT"
    assert vetoed["news"]["status"] == "AVAILABLE"
    assert vetoed["order"] is None
    assert broker.positions() == {}

    class BuyModel:
        def active_model(self):
            return {
                "version": "buy-only",
                "model": {
                    "coefficients": [8, 0, 0, 0, 0, 0, 0, 0],
                    "mean": [0] * 7,
                    "scale": [1] * 7,
                    "threshold": 0.55,
                },
            }

    falling = {
        "1h": recent_history(140, -1),
        "1d": recent_history(140, -1),
    }
    broker.submit_order(
        "XYZ",
        "BUY",
        10,
        100,
        "seed-long",
        approved=True,
        stop_loss=90,
    )
    blocked = run_paper_pipeline(
        "XYZ",
        falling,
        broker,
        "model-blocks-sell",
        approved=True,
        database=BuyModel(),
    )

    assert blocked["technical_signal"] == "SELL"
    assert blocked["decision"] == "WAIT"
    assert blocked["adaptive"]["prediction"]["signal"] == "BUY"
    assert blocked["order"] is None
    assert broker.positions()["XYZ"]["quantity"] == 10
