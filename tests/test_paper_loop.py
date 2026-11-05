from datetime import datetime
import os
from zoneinfo import ZoneInfo

import pytest

from atlas.broker import PaperBroker
from atlas.database import AtlasDatabase
from atlas.envfile import load_dotenv
from atlas.main import main
from atlas.market_hours import regular_session_status
from atlas.session import run_book_status, run_paper_loop, run_paper_session
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


NEW_YORK = ZoneInfo("America/New_York")


def _at(day, hour, minute):
    return datetime(2026, 11, day, hour, minute, tzinfo=NEW_YORK)


def _watchlist(cash=10_000.0):
    return Settings(instruments=("XYZ",), paper_initial_cash=cash)


def _bars(start, step):
    frame = recent_history(start, step)
    return {"1h": frame, "1d": frame.copy()}


def test_regular_session_is_weekday_new_york_hours_only():
    assert regular_session_status(_at(4, 9, 30)) == (True, "open")
    assert regular_session_status(_at(4, 15, 59)) == (True, "open")
    assert regular_session_status(_at(4, 9, 29)) == (False, "before_open")
    assert regular_session_status(_at(4, 16, 0)) == (False, "after_close")
    assert regular_session_status(_at(7, 12, 0)) == (False, "weekend")


def test_loop_sleeps_outside_the_session_and_does_not_fetch(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_DATABASE_PATH", str(tmp_path / "closed.sqlite3"))
    sleeps = []
    monkeypatch.setattr(
        "atlas.session.fetch_session_histories",
        lambda symbol, timeframes: (_ for _ in ()).throw(AssertionError("fetch")),
    )

    summary = run_paper_loop(
        max_cycles=2,
        interval_seconds=300,
        now=lambda: _at(7, 12, 0),
        sleep=lambda seconds: sleeps.append(seconds),
        settings=_watchlist(),
        histories_by_symbol={"XYZ": _bars(100, 1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
    )

    assert summary["live_trading"] is False
    assert summary["interval_seconds"] == 300
    assert [cycle["ran"] for cycle in summary["cycles"]] == [False, False]
    assert sleeps == [300]


def test_loop_refreshes_on_each_open_cycle_and_stops_on_interrupt(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_DATABASE_PATH", str(tmp_path / "open.sqlite3"))
    calls = {"n": 0}

    def fetch(symbol, timeframes):
        calls["n"] += 1
        return _bars(100, 0)

    monkeypatch.setattr("atlas.session.fetch_session_histories", fetch)

    sleeps = []
    summary = run_paper_loop(
        max_cycles=2,
        interval_seconds=300,
        now=lambda: _at(4, 10, 0),
        sleep=lambda seconds: sleeps.append(seconds),
        settings=_watchlist(),
        news_events=[],
        approved=False,
        timeframes=("1h", "1d"),
    )

    assert [cycle["ran"] for cycle in summary["cycles"]] == [True, True]
    assert calls["n"] == 2
    assert sleeps == [300]
    assert summary["live_trading"] is False

    def interrupt(_seconds):
        raise KeyboardInterrupt

    stopped = run_paper_loop(
        interval_seconds=300,
        now=lambda: _at(4, 10, 0),
        sleep=interrupt,
        settings=_watchlist(),
        news_events=[],
        approved=False,
        timeframes=("1h", "1d"),
    )
    assert stopped["stopped"] == "interrupt"


def test_stop_and_target_exit_and_survive_restart(tmp_path):
    database_path = tmp_path / "plans.sqlite3"
    database = AtlasDatabase(database_path)
    broker = PaperBroker(initial_cash=10_000, database=database)
    broker.submit_order(
        "XYZ",
        "BUY",
        1,
        100,
        "seed",
        approved=True,
        stop_loss=90,
        profit_target=120,
    )
    database.close()

    reopened = AtlasDatabase(database_path)
    restored = PaperBroker(initial_cash=10_000, database=reopened)
    assert restored.positions()["XYZ"]["stop_loss"] == 90
    assert restored.positions()["XYZ"]["profit_target"] == 120

    stopped = run_paper_session(
        settings=_watchlist(),
        broker=restored,
        database=reopened,
        histories_by_symbol={"XYZ": _bars(120, -1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
    )
    assert stopped["results"][0]["exit_reason"] == "stop"
    assert stopped["results"][0]["order"]["side"] == "SELL"
    assert restored.positions() == {}
    reopened.close()

    target_db = AtlasDatabase(tmp_path / "target.sqlite3")
    target_broker = PaperBroker(initial_cash=10_000, database=target_db)
    target_broker.submit_order(
        "XYZ",
        "BUY",
        1,
        100,
        "seed-target",
        approved=True,
        stop_loss=90,
        profit_target=110,
    )
    targeted = run_paper_session(
        settings=_watchlist(),
        broker=target_broker,
        database=target_db,
        histories_by_symbol={"XYZ": _bars(100, 1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
    )
    assert targeted["results"][0]["exit_reason"] == "target"
    assert targeted["results"][0]["order"]["side"] == "SELL"
    assert target_broker.positions() == {}
    target_db.close()


def test_status_book_prints_position_stop_and_fills(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATLAS_LIVE_TRADING", "false")
    monkeypatch.setenv("ATLAS_INSTRUMENTS", "XYZ")
    database = AtlasDatabase(tmp_path / "book.sqlite3")
    broker = PaperBroker(initial_cash=10_000, database=database)
    broker.submit_order(
        "XYZ",
        "BUY",
        1,
        100,
        "seed",
        approved=True,
        stop_loss=90,
        profit_target=120,
    )

    report = run_book_status(database=database)

    printed = capsys.readouterr().out
    assert "ATLAS paper book" in printed
    assert "XYZ" in printed
    assert "stop=90.00" in printed
    assert "target=120.00" in printed
    assert "cash:" in printed
    assert "equity:" in printed
    assert "daily_pnl:" in printed
    assert "BUY" in printed
    assert report["live_trading"] is False
    assert report["positions"]["XYZ"]["quantity"] == 1
    assert report["cash"] == 9_900
    database.close()


def test_main_routes_loop_and_book(monkeypatch):
    monkeypatch.setattr("atlas.main.run_paper_loop", lambda: {"mode": "paper-loop"})
    monkeypatch.setattr(
        "atlas.main.run_book_status",
        lambda: {"mode": "status-book", "live_trading": False},
    )

    assert main(["--paper", "--loop"])["mode"] == "paper-loop"
    assert main(["--loop", "--paper"])["mode"] == "paper-loop"
    assert main(["--status-book"])["mode"] == "status-book"


def test_telegram_notifies_only_when_configured(monkeypatch, tmp_path):
    sent = []

    def fake_send(message, **kwargs):
        sent.append(message)
        return {"message_id": 1}

    monkeypatch.setattr("atlas.telegram.send_telegram_message", fake_send)
    monkeypatch.delenv("ATLAS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ATLAS_TELEGRAM_CHAT_ID", raising=False)
    database = AtlasDatabase(tmp_path / "quiet.sqlite3")
    broker = PaperBroker(initial_cash=10_000, database=database)
    run_paper_session(
        settings=_watchlist(),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": _bars(100, 1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
    )
    assert sent == []
    database.close()

    monkeypatch.setenv("ATLAS_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ATLAS_TELEGRAM_CHAT_ID", "123")
    loud_db = AtlasDatabase(tmp_path / "loud.sqlite3")
    loud_broker = PaperBroker(initial_cash=10_000, database=loud_db)
    run_paper_session(
        settings=_watchlist(),
        broker=loud_broker,
        database=loud_db,
        histories_by_symbol={"XYZ": _bars(100, 1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
    )
    assert any("opened" in message for message in sent)
    loud_db.close()


def test_daily_loss_halt_notifies_without_failing_the_session(monkeypatch, tmp_path):
    sent = []

    def fake_send(message, **kwargs):
        sent.append(message)
        raise RuntimeError("telegram down")

    monkeypatch.setattr("atlas.telegram.send_telegram_message", fake_send)
    monkeypatch.setenv("ATLAS_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ATLAS_TELEGRAM_CHAT_ID", "123")
    database = AtlasDatabase(tmp_path / "halt.sqlite3")
    broker = PaperBroker(initial_cash=10_000, database=database)
    broker.submit_order(
        "XYZ",
        "BUY",
        10,
        100,
        "seed",
        approved=True,
        stop_loss=90,
        profit_target=130,
    )

    summary = run_paper_session(
        settings=_watchlist(),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": _bars(109, -1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
    )

    assert broker.positions()["XYZ"]["quantity"] == 10
    assert any("Daily loss" in str(item.get("error", "")) for item in summary["results"])
    assert any("halted" in message for message in sent)
    database.close()


def test_dotenv_loads_missing_keys_without_overriding(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join([
            "# comment",
            "ATLAS_INSTRUMENTS=QQQ",
            "export ATLAS_LOOP_INTERVAL_SECONDS=120",
            "ATLAS_PAPER_APPROVED=true",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("ATLAS_INSTRUMENTS", "SPY")
    monkeypatch.delenv("ATLAS_LOOP_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("ATLAS_PAPER_APPROVED", raising=False)

    loaded = load_dotenv(env_file)

    assert "ATLAS_INSTRUMENTS" not in loaded
    assert os.environ["ATLAS_INSTRUMENTS"] == "SPY"
    assert os.environ["ATLAS_LOOP_INTERVAL_SECONDS"] == "120"
    assert os.environ["ATLAS_PAPER_APPROVED"] == "true"
