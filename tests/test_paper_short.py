from datetime import datetime, timezone

import pytest

from atlas.broker import PaperBroker
from atlas.dashboard import PaperDashboard
from atlas.database import AtlasDatabase
from atlas.execution import execute_paper_signal
from atlas.news import NewsEvent
from atlas.pipeline import run_paper_pipeline
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


def _now():
    return datetime.now(timezone.utc).isoformat()


def _short_signal(**overrides):
    signal = {
        "instrument": "XYZ",
        "direction": "SELL",
        "entry_price": 100.0,
        "stop_loss": 110.0,
        "profit_target": 80.0,
        "market_timestamp": _now(),
    }
    signal.update(overrides)
    return signal


def _cover_signal(price=90.0):
    return {
        "instrument": "XYZ",
        "direction": "BUY",
        "entry_price": price,
        "stop_loss": price - 1,
        "profit_target": price + 1,
        "market_timestamp": _now(),
    }


def test_flat_sell_opens_a_paper_short_inside_the_risk_cap():
    broker = PaperBroker(initial_cash=10_000)
    order = execute_paper_signal(_short_signal(), broker, "open-short", approved=True)

    assert order["effect"] == "OPEN_SHORT"
    assert order["side"] == "SELL"
    assert order["quantity"] == 10
    position = broker.positions()["XYZ"]
    assert position["side"] == "SHORT"
    assert position["quantity"] == 10
    assert position["stop_loss"] == 110
    assert position["profit_target"] == 80
    assert broker.account_info()["cash"] == 11_000
    assert broker.account_info()["equity"] == 10_000
    assert broker.account_info()["trading_mode"] == "PAPER"


def test_buy_covers_a_short_and_does_not_open_a_long():
    broker = PaperBroker(initial_cash=10_000)
    execute_paper_signal(_short_signal(), broker, "open-short", approved=True)
    broker.update_market_price("XYZ", 90)

    cover = execute_paper_signal(_cover_signal(90), broker, "cover-short", approved=True)

    assert cover["effect"] == "CLOSE_SHORT"
    assert cover["side"] == "BUY"
    assert cover["quantity"] == 10
    assert cover["realized_pnl"] == pytest.approx(100)
    assert broker.positions() == {}
    assert broker.account_info()["realized_pnl"] == pytest.approx(100)


def test_larger_buy_does_not_flip_a_short_into_a_long():
    broker = PaperBroker(initial_cash=10_000)
    execute_paper_signal(_short_signal(), broker, "open-short", approved=True)

    with pytest.raises(ValueError, match="does not open a long"):
        execute_paper_signal(
            _cover_signal(100),
            broker,
            "too-big-cover",
            approved=True,
            quantity=11,
        )

    assert broker.positions()["XYZ"]["side"] == "SHORT"
    assert broker.positions()["XYZ"]["quantity"] == 10


def test_sell_closes_a_long_and_does_not_flip_it_into_a_short():
    broker = PaperBroker(initial_cash=10_000)
    broker.submit_order(
        "XYZ", "BUY", 10, 100, "seed-long", approved=True, stop_loss=90, profit_target=120
    )

    closed = execute_paper_signal(_short_signal(), broker, "close-long", approved=True)

    assert closed["effect"] == "CLOSE_LONG"
    assert closed["quantity"] == 10
    assert broker.positions() == {}


def test_sell_does_not_add_to_an_open_short():
    broker = PaperBroker(initial_cash=10_000)
    execute_paper_signal(_short_signal(), broker, "open-short", approved=True)

    with pytest.raises(ValueError, match="does not add"):
        execute_paper_signal(_short_signal(), broker, "add-short", approved=True)

    assert broker.positions()["XYZ"]["quantity"] == 10


def test_short_is_blocked_when_exposure_is_already_at_the_cap():
    broker = PaperBroker(initial_cash=10_000)
    broker.submit_order(
        "AAA", "BUY", 10, 100, "full-book", approved=True, stop_loss=90, profit_target=120
    )

    with pytest.raises(PermissionError, match="no shares"):
        execute_paper_signal(
            _short_signal(instrument="XYZ"),
            broker,
            "blocked-short",
            approved=True,
        )

    assert "XYZ" not in broker.positions()
    assert broker.positions()["AAA"]["side"] == "LONG"


def test_daily_loss_halt_blocks_a_new_short():
    broker = PaperBroker(initial_cash=10_000)
    broker.submit_order(
        "AAA", "BUY", 10, 100, "seed-long", approved=True, stop_loss=90, profit_target=120
    )
    broker.update_market_price("AAA", 70)

    with pytest.raises(PermissionError, match="Daily loss"):
        execute_paper_signal(_short_signal(), broker, "halted-short", approved=True)

    assert "XYZ" not in broker.positions()
    assert broker.positions()["AAA"]["quantity"] == 10


def test_short_reopens_from_the_ledger_as_a_short(tmp_path):
    database = AtlasDatabase(tmp_path / "short.sqlite3")
    broker = PaperBroker(initial_cash=10_000, database=database)
    execute_paper_signal(_short_signal(), broker, "open-short", approved=True)
    assert database.list_paper_orders()[-1]["effect"] == "OPEN_SHORT"
    database.close()

    restored_db = AtlasDatabase(tmp_path / "short.sqlite3")
    restored = PaperBroker(initial_cash=10_000, database=restored_db)

    assert restored.positions()["XYZ"]["side"] == "SHORT"
    assert restored.positions()["XYZ"]["quantity"] == 10
    assert restored.positions()["XYZ"]["stop_loss"] == 110
    restored_db.close()


def test_dashboard_sell_opens_a_short_only_for_a_real_sell(tmp_path, monkeypatch):
    frame = recent_history(100, 0)
    database = AtlasDatabase(tmp_path / "dash-short.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(instruments=("XYZ",), paper_initial_cash=10_000),
        database=database,
        histories_by_symbol={"XYZ": {"1h": frame, "1d": frame.copy()}},
        news_events=[],
        hard_stop=False,
    )

    def preview(symbol, broker, histories):
        return {
            "decision": "WAIT",
            "recommendation": None,
        }

    monkeypatch.setattr(service, "_preview", preview)
    with pytest.raises(ValueError, match="not a short"):
        service.sell(service.token, "XYZ")
    assert service._broker().positions() == {}

    monkeypatch.setattr(
        service,
        "_preview",
        lambda symbol, broker, histories: {
            "decision": "SELL",
            "recommendation": _short_signal(),
        },
    )
    opened = service.sell(service.token, "XYZ")
    assert opened["order"]["effect"] == "OPEN_SHORT"
    assert service._broker().positions()["XYZ"]["side"] == "SHORT"

    covered = service.buy(service.token, "XYZ")
    assert covered["order"]["effect"] == "CLOSE_SHORT"
    assert service._broker().positions() == {}
    database.close()


def test_dashboard_sell_closes_a_long_without_opening_a_short(tmp_path):
    frame = recent_history(100, 1)
    database = AtlasDatabase(tmp_path / "dash-long.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(instruments=("XYZ",), paper_initial_cash=10_000),
        database=database,
        histories_by_symbol={"XYZ": {"1h": frame, "1d": frame.copy()}},
        news_events=[],
        hard_stop=False,
    )
    service._broker().submit_order(
        "XYZ", "BUY", 4, 100, "seed-long", approved=True, stop_loss=90, profit_target=120
    )

    closed = service.sell(service.token, "XYZ")

    assert closed["order"]["effect"] == "CLOSE_LONG"
    assert service._broker().positions() == {}
    database.close()


def test_opposing_news_cannot_turn_a_wait_into_a_short():
    falling = {"1h": recent_history(140, -1), "1d": recent_history(140, -1)}
    broker = PaperBroker(initial_cash=10_000)
    news = [
        NewsEvent(
            event_id="bullish-1",
            title="Supplied opposing item",
            category="EARNINGS",
            published_at=datetime.now(timezone.utc),
            sentiment=1.0,
            impact=1.0,
            instruments=("XYZ",),
        )
    ]

    withheld = run_paper_pipeline(
        "XYZ",
        falling,
        broker,
        "news-blocks-short",
        approved=True,
        news_events=news,
    )

    assert withheld["decision"] == "WAIT"
    assert withheld["order"] is None
    assert broker.positions() == {}


def test_page_copy_names_a_short_and_a_cover():
    page = open("atlas/dashboard_page.html", encoding="utf-8").read()
    assert 'content="29"' in page
    assert "Buy covers the open paper short" in page
    assert "Sell opens a paper short" in page
    assert "does not open a short" in page
    assert "Buy to cover" in page
    assert "No open paper position." in page
    assert "A wait is not a short" in page
