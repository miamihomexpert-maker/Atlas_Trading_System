from pathlib import Path

import pytest

from atlas.dashboard import PaperDashboard
from atlas.database import AtlasDatabase
from atlas.review import build_review
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


def _service(tmp_path, histories):
    database = AtlasDatabase(tmp_path / "review.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(instruments=("XYZ",), paper_initial_cash=10_000, live_trading_enabled=False),
        database=database,
        histories_by_symbol=histories,
        news_events=[],
        candle_loader=lambda symbol, timeframe: histories[symbol][timeframe],
    )
    return service, database


def test_empty_ledger_has_no_scoreboard(tmp_path):
    histories = {"XYZ": {"1h": recent_history(100, 1), "1d": recent_history(100, 1)}}
    service, database = _service(tmp_path, histories)
    report = service.review()
    assert report["fills"] == []
    assert report["scoreboard"] is None
    assert report["scoreboard_note"] == "No paper fills to review yet."
    assert report["model"]["graded"] == 0
    database.close()


def test_fill_without_a_later_bar_does_not_invent_pnl():
    report = build_review(
        [{
            "order_id": "buy-1",
            "symbol": "XYZ",
            "side": "BUY",
            "quantity": 2,
            "price": 100.0,
            "timestamp": "2099-01-01T00:00:00+00:00",
            "realized_pnl": 0.0,
        }],
        [],
        {"XYZ": {"1h": [{"time": "2026-11-07T15:00:00+00:00", "close": 130.0}]}},
    )
    assert report["fills"][0]["result"] is None
    assert report["fills"][0]["note"] == "Not enough later bars yet."
    assert report["scoreboard"] is None
    assert report["scoreboard_note"] == "Not enough later bars yet."


def test_fill_with_a_later_bar_uses_that_close(tmp_path):
    frame = recent_history(110, 1, rows=5)
    histories = {"XYZ": {"1h": frame, "1d": frame.copy()}}
    service, database = _service(tmp_path, histories)
    database.record_paper_fill(
        {
            "order_id": "buy-1",
            "symbol": "XYZ",
            "side": "BUY",
            "quantity": 2,
            "price": 100.0,
        },
        0.0,
        {"equity": 10_000.0, "realized_pnl": 0.0},
        timestamp="2020-01-01T00:00:00+00:00",
    )
    report = service.review()
    fill = report["fills"][0]
    assert fill["result"] == pytest.approx((110.0 - 100.0) * 2)
    assert fill["result_kind"] == "next_bar"
    assert fill["counts"] is True
    assert report["scoreboard"]["reviewed_count"] == 1
    assert report["scoreboard"]["hit_count"] == 1
    assert report["scoreboard"]["hit_rate"] == 1
    assert report["scoreboard"]["average_gain"] == pytest.approx(20)
    stored = database.get_metadata("paper_review")
    assert "buy-1" in stored
    assert any(event["action"] == "PAPER_REVIEW" for event in database.list_audit_events())
    database.close()


def test_wait_review_stays_a_wait_and_does_not_enable_buy(tmp_path):
    up = recent_history(100, 1)
    down = recent_history(140, -1)
    histories = {"XYZ": {"1h": up, "1d": down}}
    service, database = _service(tmp_path, histories)
    database.record_scan_observation(
        "XYZ",
        "WAIT",
        100.0,
        "2020-01-01T00:00:00+00:00",
        "1h",
        "CANDIDATE_NOT_PROMOTED",
        "BUY",
        "candidate-1",
    )
    report = service.review()
    reviewed = [row for row in report["decisions"] if row["bar_time"].startswith("2020-01-01")]
    assert reviewed[0]["decision"] == "WAIT"
    assert reviewed[0]["next_return"] is not None
    assert "stayed a wait" in reviewed[0]["note"]
    assert reviewed[0]["model_matched"] is None
    assert report["model"]["note"] == "A stored model was not used for the decision."
    ranked = service.candidates()
    assert ranked["scans"][0]["decision"] == "WAIT"
    assert ranked["scans"][0]["buyable"] is False
    with pytest.raises(ValueError, match="not a current BUY"):
        service.buy(service.token, "XYZ")
    database.close()


def test_sell_realized_pnl_comes_from_the_ledger_only():
    report = build_review(
        [{
            "order_id": "sell-1",
            "symbol": "XYZ",
            "side": "SELL",
            "quantity": 1,
            "price": 110.0,
            "timestamp": "2026-11-07T16:00:00+00:00",
            "realized_pnl": 10.0,
        }],
        [],
        {},
    )
    assert report["fills"][0]["result"] == 10.0
    assert report["fills"][0]["result_kind"] == "realized"
    assert report["scoreboard"]["average_gain"] == 10.0
    assert report["scoreboard"]["average_loss"] is None


def test_review_tab_is_present_and_buy_rules_stay():
    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert 'data-tab="review"' in page
    assert 'data-panel="review"' in page
    assert "No paper fills to review yet." in page
    assert "scoreboard_note" in page
    assert 'buyOn ? "" : "disabled"' in page
    assert 'sellOn ? "" : "disabled"' in page
    assert "No open paper position." in page
