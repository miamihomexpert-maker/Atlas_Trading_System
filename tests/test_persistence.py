import pytest

from atlas.broker import PaperBroker
from atlas.database import AtlasDatabase


def test_paper_fills_and_performance_persist_across_database_instances(tmp_path):
    database_path = tmp_path / "atlas.sqlite3"
    database = AtlasDatabase(database_path)
    broker = PaperBroker(initial_cash=4_000, database=database)
    broker.submit_order(
        "XYZ",
        "BUY",
        4,
        100,
        "entry",
        approved=True,
        stop_loss=90,
    )
    broker.update_market_price("XYZ", 110)
    broker.submit_order(
        "XYZ",
        "SELL",
        2,
        110,
        "partial-exit",
        approved=True,
        stop_loss=120,
    )
    database.close()

    reopened = AtlasDatabase(database_path)
    report = reopened.performance_report()

    assert report["fill_count"] == 2
    assert report["closed_fill_count"] == 1
    assert report["win_rate"] == 1
    assert report["net_realized_pnl"] == 20
    assert report["maximum_drawdown"] == 0
    assert report["trading_costs"] is None
    assert len(reopened.list_paper_orders()) == 2

    restored = PaperBroker(initial_cash=4_000, database=reopened)
    assert restored.positions()["XYZ"]["quantity"] == 2
    assert restored.account_info()["cash"] == 3_820
    assert restored.account_info()["realized_pnl"] == 20
    with pytest.raises(ValueError, match="risk limit"):
        restored.submit_order(
            "QQQ",
            "BUY",
            4,
            100,
            "second-book",
            approved=True,
            stop_loss=90,
        )
    assert "QQQ" not in restored.positions()
    reopened.close()


def test_failed_persistence_rolls_back_paper_broker_state():
    class BrokenDatabase:
        def record_initial_account(self, account):
            pass

        def record_paper_fill(self, order, realized_pnl, account):
            raise OSError("disk unavailable")

    broker = PaperBroker(initial_cash=1_000, database=BrokenDatabase())

    with pytest.raises(OSError, match="disk unavailable"):
        broker.submit_order(
            "XYZ",
            "BUY",
            1,
            100,
            "not-persisted",
            approved=True,
            stop_loss=90,
        )

    assert broker.account_info()["cash"] == 1_000
    assert broker.positions() == {}
    assert broker.trade_history() == []


def test_equity_snapshots_track_mark_to_market_drawdown(tmp_path):
    database = AtlasDatabase(tmp_path / "drawdown.sqlite3")
    broker = PaperBroker(initial_cash=1_000, database=database)
    broker.submit_order(
        "XYZ",
        "BUY",
        1,
        100,
        "entry",
        approved=True,
        stop_loss=90,
    )
    broker.update_market_price("XYZ", 50)

    report = database.performance_report()

    assert report["maximum_drawdown"] == 0.05
    database.close()


def test_model_promotion_requires_performance_gates_and_rollback(tmp_path):
    database = AtlasDatabase(tmp_path / "models.sqlite3")
    poor = {
        "sample_count": 100,
        "accuracy": 0.60,
        "net_return_pct": -1,
        "maximum_drawdown": 0.05,
    }
    database.save_model_candidate("failed", poor, {"weights": []})
    assert not database.promote_model("failed")["promoted"]
    assert database.active_model() is None

    first = {
        "sample_count": 100,
        "accuracy": 0.60,
        "net_return_pct": 5,
        "maximum_drawdown": 0.10,
    }
    database.save_model_candidate("first", first, {"weights": [1]})
    assert database.promote_model("first")["promoted"]

    second = {
        "sample_count": 100,
        "accuracy": 0.61,
        "net_return_pct": 6,
        "maximum_drawdown": 0.09,
        "candidate_comparison": {
            "sample_count": 100,
            "accuracy": 0.60,
            "net_return_pct": 6,
            "maximum_drawdown": 0.09,
        },
        "incumbent_comparison": {
            "version": "first",
            "sample_count": 100,
            "accuracy": 0.58,
            "net_return_pct": 5,
            "maximum_drawdown": 0.10,
        },
    }
    database.save_model_candidate("second", second, {"weights": [2]})
    assert database.promote_model("second")["active_version"] == "second"
    assert database.rollback_model("first") == "first"
    assert database.active_model()["version"] == "first"
    database.close()


def test_restart_restores_position_and_blocks_another_full_buy(tmp_path):
    database_path = tmp_path / "restore.sqlite3"
    database = AtlasDatabase(database_path)
    broker = PaperBroker(initial_cash=10_000, database=database)
    broker.submit_order(
        "XYZ",
        "BUY",
        10,
        100,
        "entry",
        approved=True,
        stop_loss=90,
    )
    broker.update_market_price("XYZ", 70)
    database.close()

    reopened = AtlasDatabase(database_path)
    restored = PaperBroker(initial_cash=10_000, database=reopened)

    assert restored.positions()["XYZ"]["quantity"] == 10
    assert restored.account_info()["cash"] == 9_000
    assert restored.positions()["XYZ"]["last_price"] == pytest.approx(70)
    assert restored.daily_pnl() == pytest.approx(-300)
    with pytest.raises(PermissionError, match="Daily loss"):
        restored.submit_order(
            "XYZ",
            "BUY",
            1,
            70,
            "second",
            approved=True,
            stop_loss=69,
        )
    replayed = restored.submit_order(
        "XYZ",
        "BUY",
        10,
        100,
        "entry",
        approved=True,
        stop_loss=90,
    )
    assert replayed["order_id"] == "entry"
    assert restored.positions()["XYZ"]["quantity"] == 10
    reopened.close()
