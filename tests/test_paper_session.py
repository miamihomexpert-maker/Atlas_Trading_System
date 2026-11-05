from datetime import datetime, timezone

import pytest

from atlas.broker import PaperBroker
from atlas.database import AtlasDatabase
from atlas.main import main
from atlas.session import run_paper_session
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


def _watchlist(symbols=("XYZ",), cash=10_000.0):
    return Settings(
        instruments=symbols,
        paper_initial_cash=cash,
        live_trading_enabled=False,
    )


def _bars(start, step):
    frame = recent_history(start, step)
    return {"1h": frame, "1d": frame.copy()}


def _promote(database, version, intercept):
    database.save_model_candidate(
        version,
        {
            "sample_count": 80,
            "accuracy": 0.60,
            "net_return_pct": 1.0,
            "maximum_drawdown": 0.10,
        },
        {
            "coefficients": [intercept, 0, 0, 0, 0, 0, 0, 0],
            "mean": [0] * 7,
            "scale": [1] * 7,
            "threshold": 0.55,
        },
    )
    assert database.promote_model(version)["promoted"]


def test_paper_session_buys_from_the_watchlist_and_persists_the_fill(tmp_path):
    database = AtlasDatabase(tmp_path / "session.sqlite3")
    broker = PaperBroker(initial_cash=10_000, database=database)

    summary = run_paper_session(
        settings=_watchlist(),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": _bars(100, 1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
    )

    assert summary["mode"] == "paper"
    assert summary["live_trading"] is False
    assert summary["fill_count"] == 1
    order = summary["results"][0]["order"]
    assert order["side"] == "BUY"
    assert order["symbol"] == "XYZ"
    assert order["quantity"] > 0
    assert database.list_paper_orders()[0]["order_id"] == order["order_id"]
    assert summary["results"][0]["adaptive"]["status"] == "NO_ACTIVE_MODEL"
    assert summary["results"][0]["news"]["status"] == "NO_RELEVANT_EVENTS"
    database.close()


def test_paper_session_sells_when_the_active_model_agrees(tmp_path):
    database = AtlasDatabase(tmp_path / "sell.sqlite3")
    _promote(database, "sell-v1", intercept=-8)
    broker = PaperBroker(initial_cash=10_000, database=database)
    broker.submit_order(
        "XYZ",
        "BUY",
        10,
        100,
        "seed-long",
        approved=True,
        stop_loss=90,
    )

    summary = run_paper_session(
        settings=_watchlist(),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": _bars(140, -1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
    )

    assert summary["fill_count"] == 1
    assert summary["results"][0]["technical_signal"] == "SELL"
    assert summary["results"][0]["decision"] == "SELL"
    assert summary["results"][0]["adaptive"]["prediction"]["signal"] == "SELL"
    assert summary["results"][0]["order"]["side"] == "SELL"
    remaining = broker.positions().get("XYZ", {}).get("quantity", 0)
    assert remaining < 10
    database.close()


def test_unapproved_session_does_not_submit_and_remote_approval_is_not_bypassed(tmp_path):
    database = AtlasDatabase(tmp_path / "approval.sqlite3")
    broker = PaperBroker(initial_cash=10_000, database=database)
    histories = {"XYZ": _bars(100, 1)}

    held = run_paper_session(
        settings=_watchlist(),
        broker=broker,
        database=database,
        histories_by_symbol=histories,
        news_events=[],
        approved=False,
        timeframes=("1h", "1d"),
    )
    remote = run_paper_session(
        settings=_watchlist(),
        broker=broker,
        database=database,
        histories_by_symbol=histories,
        news_events=[],
        approved=True,
        require_remote_approval=True,
        timeframes=("1h", "1d"),
    )

    assert held["fill_count"] == 0
    assert "approval" in held["results"][0]["error"]
    assert remote["fill_count"] == 0
    assert "Remote approval" in remote["results"][0]["error"]
    assert broker.positions() == {}
    database.close()


def test_session_refuses_live_trading_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_LIVE_TRADING", "true")
    monkeypatch.setenv("ATLAS_DATABASE_PATH", str(tmp_path / "live.sqlite3"))

    with pytest.raises(ValueError, match="deliberately disabled"):
        run_paper_session()
    assert not (tmp_path / "live.sqlite3").exists()


@pytest.mark.parametrize("argv", [[], ["--paper"]])
def test_main_paper_command_runs_the_watchlist(monkeypatch, tmp_path, argv):
    monkeypatch.setenv("ATLAS_INSTRUMENTS", "XYZ")
    monkeypatch.setenv("ATLAS_PAPER_APPROVED", "true")
    monkeypatch.setenv("ATLAS_LIVE_TRADING", "false")
    monkeypatch.setenv("ATLAS_DATABASE_PATH", str(tmp_path / "main.sqlite3"))
    monkeypatch.setenv("ATLAS_TIMEFRAMES", "1h,1d")
    monkeypatch.setattr(
        "atlas.session.fetch_session_histories",
        lambda symbol, timeframes: _bars(100, 1),
    )

    summary = main(argv)

    assert summary["fill_count"] == 1
    assert summary["live_trading"] is False
    assert summary["results"][0]["order"]["symbol"] == "XYZ"
    reopened = AtlasDatabase(tmp_path / "main.sqlite3")
    assert reopened.list_paper_orders()
    reopened.close()


def test_status_command_does_not_fetch_or_create_a_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_LIVE_TRADING", "false")
    monkeypatch.setenv("ATLAS_DATABASE_PATH", str(tmp_path / "status.sqlite3"))

    def fail_fetch(symbol, timeframes):
        raise AssertionError("status must not fetch market data")

    monkeypatch.setattr("atlas.session.fetch_session_histories", fail_fetch)

    result = main(["--status"])

    assert result == {"mode": "status", "live_trading": False}
    assert not (tmp_path / "status.sqlite3").exists()


def test_news_file_withholds_a_buy(monkeypatch, tmp_path):
    news_path = tmp_path / "news.json"
    news_path.write_text(
        """
        [{
          "event_id": "n1",
          "title": "Supplied opposing headline",
          "category": "EARNINGS",
          "published_at": "%s",
          "sentiment": -1,
          "impact": 1,
          "instruments": ["XYZ"]
        }]
        """
        % datetime.now(timezone.utc).isoformat(),
        encoding="utf-8",
    )
    monkeypatch.setenv("ATLAS_NEWS_FILE", str(news_path))
    database = AtlasDatabase(tmp_path / "news.sqlite3")
    broker = PaperBroker(initial_cash=10_000, database=database)

    summary = run_paper_session(
        settings=_watchlist(),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": _bars(100, 1)},
        approved=True,
        timeframes=("1h", "1d"),
    )

    assert summary["fill_count"] == 0
    assert summary["results"][0]["decision"] == "WAIT"
    assert summary["results"][0]["technical_signal"] == "BUY"
    assert broker.positions() == {}
    database.close()
