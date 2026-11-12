from datetime import datetime, timezone

import pandas as pd

from atlas.dashboard import PaperDashboard
from atlas.database import AtlasDatabase
from atlas.learning import (
    DISCLAIMER,
    describe_behavior,
    learning_bias_for,
    load_public_behavior,
    outcome_from_observation,
    remember_outcomes,
    remember_public,
    stamp_learning_bias,
    summarize_outcomes,
)
from atlas.pipeline import run_paper_pipeline
from atlas.ranking import rank_scans
from atlas.broker import PaperBroker
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


def _bars():
    return [
        {"time": "2026-11-01T00:00:00+00:00", "close": 100.0},
        {"time": "2026-11-02T00:00:00+00:00", "close": 110.0},
    ]


def test_a_wait_is_graded_and_left_as_a_wait():
    record = outcome_from_observation(
        {
            "decision": "WAIT",
            "price": 100.0,
            "bar_time": "2026-11-01T00:00:00+00:00",
            "timeframe": "1d",
        },
        _bars(),
    )
    assert record["effect"] == "none"
    assert record["detail"]["decision"] == "WAIT"
    assert record["detail"]["aligned"] is None
    assert "left as a wait" in record["summary"]
    assert learning_bias_for("WAIT", summarize_outcomes([{"source": "later_market_bar", "detail": record["detail"]}])) == 0


def test_learning_reorders_only_inside_the_same_decision():
    summary = {
        "buy_checked": 2,
        "buy_followed_by_rise": 2,
        "sell_checked": 0,
        "sell_followed_by_fall": 0,
    }
    cards = stamp_learning_bias(
        [
            {"symbol": "WAIT", "decision": "WAIT", "confidence": 0.9, "buyable": False},
            {"symbol": "LOW", "decision": "BUY", "confidence": 0.5, "buyable": True},
            {"symbol": "HIGH", "decision": "BUY", "confidence": 0.5, "buyable": True},
        ],
        {
            "WAIT": [{"source": "later_market_bar", "detail": {"decision": "BUY", "aligned": True}}] * 5,
            "HIGH": [{"source": "later_market_bar", "detail": {"decision": "BUY", "aligned": True}}] * 2,
            "LOW": [],
        },
    )
    assert all(card["decision"] != "BUY" or card["symbol"] != "WAIT" for card in cards)
    assert cards[0]["decision"] == "WAIT"
    assert cards[0]["learning_bias"] == 0
    assert cards[0]["buyable"] is False
    ranked = rank_scans(cards)
    assert [card["symbol"] for card in ranked] == ["HIGH", "LOW", "WAIT"]
    assert learning_bias_for("BUY", summary) == 2
    assert learning_bias_for("SELL", summary) == 0


def test_public_rows_keep_only_returned_fields_and_do_not_add_a_motive():
    frame = pd.DataFrame(
        {
            "Insider": ["Ada Example", None],
            "Position": ["Director", "CEO"],
            "Transaction": ["Sale", "Purchase"],
            "Shares": [10, float("nan")],
            "Value": [float("nan"), 5],
            "URL": ["https://example.com/form4", "not a url"],
        }
    )
    holders = pd.DataFrame({"Holder": ["Example Fund"], "Shares": [1000], "Date Reported": ["2026-09-30"]})

    class FakeTicker:
        insider_transactions = frame
        institutional_holders = holders

    payload = load_public_behavior("xyz", ticker_factory=lambda _symbol: FakeTicker())
    assert payload["status"] == "FETCHED"
    assert payload["insiders"][0]["insider"] == "Ada Example"
    assert payload["insiders"][0]["transaction"] == "Sale"
    assert "value" not in payload["insiders"][0]
    assert payload["insiders"][0]["url"] == "https://example.com/form4"
    assert "motive" not in payload["insiders"][0]
    assert payload["insiders"][1]["transaction"] == "Purchase"
    assert "shares" not in payload["insiders"][1]
    assert payload["holders"][0]["holder"] == "Example Fund"
    text = describe_behavior("XYZ", [], "FETCHED")["lines"]
    assert any("do not change a wait" in line for line in text)
    assert DISCLAIMER in text


def test_a_failed_feed_stores_nothing_and_a_repeat_is_not_audited_twice(tmp_path):
    database = AtlasDatabase(tmp_path / "learn.sqlite3")
    failed = load_public_behavior(
        "XYZ",
        ticker_factory=lambda _symbol: (_ for _ in ()).throw(RuntimeError("feed down")),
    )
    assert failed["status"] == "FETCH_FAILED"
    assert failed["insiders"] == []
    assert remember_public(database, "XYZ", failed) == []
    assert database.list_learning_events("XYZ") == []

    payload = {
        "status": "FETCHED",
        "source": "Yahoo Finance public holder data via yfinance",
        "fetched_at": "2026-11-10T15:00:00+00:00",
        "insiders": [{"insider": "Ada Example", "transaction": "Sale", "shares": 10}],
        "holders": [],
    }
    assert remember_public(database, "XYZ", payload) == ["insider=Ada Example|transaction=Sale|shares=10"]
    assert remember_public(database, "XYZ", payload) == []
    events = database.list_learning_events("XYZ")
    assert len(events) == 1
    assert events[0]["effect"] == "context_only"
    audits = [row for row in database.list_audit_events() if row["action"] == "LEARNING_RECORDED"]
    assert len(audits) == 1
    assert audits[0]["detail"]["source"] == "yahoo_insider_transaction"
    database.close()


def test_outcome_memory_does_not_create_a_trade(tmp_path):
    database = AtlasDatabase(tmp_path / "outcome.sqlite3")
    database.record_scan_observation(
        "XYZ",
        "WAIT",
        100.0,
        "2026-11-01T14:00:00+00:00",
        "1h",
        None,
        None,
        None,
    )
    histories = {
        "1h": recent_history(100, 0),
        "1d": recent_history(100, 0),
    }
    # Force a bar before the latest close so a later bar exists.
    frame = histories["1h"]
    database.record_scan_observation(
        "XYZ",
        "BUY",
        float(frame["Close"].iloc[0]),
        frame.index[0].isoformat(),
        "1h",
        None,
        None,
        None,
    )
    stored = remember_outcomes(database, "XYZ", histories)
    assert stored
    assert all(item["detail"]["decision"] != "WAIT" or item["effect"] == "none" for item in stored)
    broker = PaperBroker(initial_cash=10_000)
    result = run_paper_pipeline(
        "XYZ",
        {"1h": recent_history(100, 0), "1d": recent_history(140, -1)},
        broker,
        "learning-cannot-trade",
        approved=True,
        database=database,
        news_events=[],
    )
    assert result["order"] is None
    assert broker.positions() == {}
    database.close()


def test_dashboard_shows_behavior_without_promoting_a_wait(tmp_path):
    frame = recent_history(100, 0)
    database = AtlasDatabase(tmp_path / "panel.sqlite3")

    def loader(symbol):
        assert symbol == "XYZ"
        return {
            "status": "FETCHED",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "test feed",
            "insiders": [{"insider": "Ada Example", "transaction": "Purchase", "shares": 3}],
            "holders": [],
        }

    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(instruments=("XYZ",), paper_initial_cash=10_000),
        database=database,
        histories_by_symbol={"XYZ": {"1h": frame, "1d": frame.copy()}},
        news_events=[],
        hard_stop=False,
        behavior_loader=loader,
    )
    card = service.candidates()["scans"][0]
    assert card["decision"] != "BUY" or card["buyable"] is True
    if card["decision"] == "WAIT":
        assert card["buyable"] is False
    assert card["behavior"]["insiders"][0]["transaction"] == "Purchase"
    assert "not a forecast of profit" in card["behavior"]["disclaimer"]
    assert card["behavior"]["insiders"][0]["effect"] == "context_only"
    page = open("atlas/dashboard_page.html", encoding="utf-8").read()
    assert 'name="atlas-dashboard-version" content="29"' in page
    assert 'data-tab="behavior"' in page
    assert "#behavior a { color: #ffd56a; text-decoration: underline; }" in page
    assert "SEC Form 4 search" in page
    database.close()
