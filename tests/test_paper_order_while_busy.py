"""A confirmed paper order still fills when a scan or the loop holds the book.

The scan keeps the shared market-data lock. The loop process keeps the SQLite
file. Neither one may swallow the order. A real risk rejection stays visible.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from atlas.broker import PaperBroker
from atlas.dashboard import PaperDashboard, _shares_blocked_reason, make_server
from atlas.database import AtlasDatabase
from atlas.market_data import call_yahoo, clear_feed_pause
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


def _settings(*symbols, cash=100_000.0):
    return Settings(
        instruments=symbols,
        paper_initial_cash=cash,
        live_trading_enabled=False,
    )


def _stamp(hours_ago=0):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return when.isoformat()


def _card(symbol, decision, entry, stop, target, stamp, price=None):
    return {
        "symbol": symbol,
        "decision": decision,
        "reason": "Stored from the scan the user confirmed.",
        "price": entry if price is None else price,
        "entry": entry,
        "stop": stop,
        "target": target,
        "quantity": 1,
        "confidence": 0.5,
        "reasons": ["Stored from the scan the user confirmed."],
        "buyable": decision == "BUY",
        "shortable": decision == "SELL",
        "agreement": {"1h": decision},
        "scanned_at": stamp,
        "market_timestamp": stamp,
        "timeframes": {"1h": {"signal": decision}},
        "news": {"status": None, "headlines": [], "items": [], "source": None},
        "model": {
            "status": None,
            "reason": None,
            "version": None,
            "probability_up": None,
            "signal": None,
        },
    }


def _live(tmp_path, symbols, cards, cash=100_000.0):
    database = AtlasDatabase(tmp_path / "orders.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=_settings(*symbols, cash=cash),
        database=database,
        histories_by_symbol=None,
        news_events=[],
        hard_stop=False,
    )
    for card in cards:
        database.upsert_symbol_scan(card["symbol"], card)
    return service, database


def _forbid_fetch(*_args, **_kwargs):
    raise AssertionError("the order waited on the scan's market-data fetch")


def _hold_scan_lock():
    held = threading.Event()
    release = threading.Event()

    def scan_action():
        held.set()
        release.wait(30)
        return "scan"

    thread = threading.Thread(target=lambda: call_yahoo(scan_action, timeout=30))
    thread.start()
    assert held.wait(2)
    return thread, release


def test_order_fills_while_a_scan_holds_the_market_data_lock(tmp_path, monkeypatch):
    clear_feed_pause()
    stamp = _stamp()
    service, database = _live(
        tmp_path,
        ("CF", "CCL", "IP"),
        [
            _card("CF", "BUY", 125.37, 123.60, 128.90, stamp),
            _card("CCL", "SELL", 20.0, 21.0, 18.0, stamp),
            _card("IP", "BUY", 125.37, 123.60, 128.90, stamp),
        ],
    )
    monkeypatch.setattr("atlas.dashboard.fetch_session_histories", _forbid_fetch)
    service._scan_active = True
    thread, release = _hold_scan_lock()
    try:
        blocked, error = call_yahoo(lambda: "shared")
        assert blocked is None
        assert "already running" in str(error).lower()

        started = time.perf_counter()
        bought = service.buy(service.token, "CF")
        assert time.perf_counter() - started < 1
        order = bought["order"]
        assert order["status"] == "FILLED"
        assert order["side"] == "BUY"
        assert order["price"] == 125.37
        assert order["quantity"] == 79

        book = service.portfolio()
        assert book["cash"] == pytest.approx(100_000 - 79 * 125.37)
        assert book["cash"] < 100_000
        position = book["positions"][0]
        assert position["symbol"] == "CF"
        assert position["side"] == "LONG"
        assert position["quantity"] == 79
        assert book["recent_fills"][-1]["symbol"] == "CF"
        assert book["recent_fills"][-1]["side"] == "BUY"
        trades = service.day_trades()["rows"]
        assert trades[0]["symbol"] == "CF"
        assert trades[0]["closed"] is False
        assert trades[0]["action"] == "sell"

        with pytest.raises(PermissionError, match="10%"):
            service.buy(service.token, "IP")
        assert [row["symbol"] for row in service.portfolio()["positions"]] == ["CF"]

        closed = service.sell(service.token, "CF")
        assert closed["order"]["status"] == "FILLED"
        assert closed["order"]["side"] == "SELL"
        assert closed["order"]["effect"] == "CLOSE_LONG"
        assert service.portfolio()["positions"] == []
        closed_row = service.day_trades()["rows"][0]
        assert closed_row["symbol"] == "CF"
        assert closed_row["closed"] is True

        shorted = service.sell(service.token, "CCL")
        assert shorted["order"]["status"] == "FILLED"
        assert shorted["order"]["effect"] == "OPEN_SHORT"
        assert shorted["order"]["quantity"] > 0
        short_book = service.portfolio()
        assert short_book["positions"][0]["side"] == "SHORT"
        assert short_book["positions"][0]["symbol"] == "CCL"
        assert short_book["cash"] > 100_000
        short_row = service.day_trades()["rows"]
        assert any(row["symbol"] == "CCL" and row["action"] == "cover" for row in short_row)

        covered = service.buy(service.token, "CCL")
        assert covered["order"]["effect"] == "CLOSE_SHORT"
        assert service.portfolio()["positions"] == []

        still_blocked, still_error = call_yahoo(lambda: "shared")
        assert still_blocked is None
        assert "already running" in str(still_error).lower()
    finally:
        release.set()
        thread.join(3)
        clear_feed_pause()
        database.close()


def test_risk_rejections_name_the_limit_while_a_scan_is_running(tmp_path, monkeypatch):
    clear_feed_pause()
    stamp = _stamp()
    stale = _stamp(hours_ago=2)
    service, database = _live(
        tmp_path,
        ("WAIT", "GAP", "OLD", "TINY", "FRESH"),
        [
            _card("WAIT", "WAIT", None, None, None, stamp),
            {
                **_card("GAP", "BUY", 125.37, 123.60, 128.90, stamp),
                "market_timestamp": None,
            },
            _card("OLD", "BUY", 125.37, 123.60, 128.90, stale),
            _card("TINY", "BUY", 500.0, 1.0, 600.0, stamp),
            _card("FRESH", "BUY", 50.0, 49.0, 55.0, stamp),
        ],
        cash=10_000.0,
    )
    monkeypatch.setattr("atlas.dashboard.fetch_session_histories", _forbid_fetch)
    service._scan_active = True
    try:
        with pytest.raises(ValueError, match="not a current BUY") as wait_error:
            service.buy(service.token, "WAIT")
        assert "wait" in str(wait_error.value).lower()

        with pytest.raises(PermissionError, match="source-market timestamp"):
            service.buy(service.token, "GAP")

        with pytest.raises(PermissionError, match="stale"):
            service.buy(service.token, "OLD")

        with pytest.raises(PermissionError, match="1%"):
            service.buy(service.token, "TINY")

        assert service.portfolio()["cash"] == 10_000
        assert service.portfolio()["positions"] == []
        assert service.portfolio()["recent_fills"] == []
        assert service.day_trades()["rows"] == []
        assert "1%" in _shares_blocked_reason(10_000, 500, 1, 0)
        assert "10%" in _shares_blocked_reason(10_000, 100, 99, 1_000)

        broker = service._broker()
        broker.submit_order(
            "LOSS", "BUY", 1, 1000, "seed-loss",
            approved=True, stop_loss=900, profit_target=1100,
        )
        broker.update_market_price("LOSS", 100)
        with pytest.raises(PermissionError, match="Daily loss"):
            service.buy(service.token, "FRESH")
        assert service._broker().positions()["LOSS"]["quantity"] == 1

        server = make_server(service, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            request = Request(
                f"http://{host}:{port}/api/buy",
                data=b'{"symbol":"WAIT"}',
                headers={
                    "Content-Type": "application/json",
                    "X-Atlas-Token": service.token,
                },
                method="POST",
            )
            with pytest.raises(HTTPError) as http_error:
                urlopen(request)
            assert http_error.value.code == 400
            body = http_error.value.read().decode("utf-8")
            assert "not a current BUY" in body
            assert "wait" in body.lower()
        finally:
            server.shutdown()
            server.server_close()
    finally:
        clear_feed_pause()
        database.close()


def test_order_fills_while_the_loop_process_holds_the_database(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTO_TRAIN", "false")
    clear_feed_pause()
    frame = recent_history(100, 1)
    database = AtlasDatabase(tmp_path / "shared.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=_settings("XYZ", cash=10_000),
        database=database,
        histories_by_symbol={"XYZ": {"1h": frame, "1d": frame.copy()}},
        news_events=[],
        hard_stop=False,
    )
    holder = tmp_path / "hold_ledger.py"
    holder.write_text(
        "import sqlite3, sys, time\n"
        "path, ready = sys.argv[1], sys.argv[2]\n"
        "conn = sqlite3.connect(path, timeout=30)\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "open(ready, 'w', encoding='utf-8').write('held')\n"
        "time.sleep(6)\n"
        "conn.commit()\n"
        "conn.close()\n",
        encoding="utf-8",
    )
    ready = tmp_path / "ready"
    process = subprocess.Popen(
        [sys.executable, str(holder), database.path, str(ready)],
    )
    try:
        deadline = time.time() + 5
        while not ready.exists() and time.time() < deadline and process.poll() is None:
            time.sleep(0.02)
        assert ready.exists()
        started = time.perf_counter()
        result = service.buy(service.token, "XYZ")
        elapsed = time.perf_counter() - started
        assert elapsed >= 1
        assert elapsed < 20
        assert result["order"]["status"] == "FILLED"
        assert result["order"]["side"] == "BUY"
        assert result["order"]["quantity"] > 0
        book = service.portfolio()
        assert book["positions"][0]["symbol"] == "XYZ"
        assert book["cash"] < 10_000
        assert book["recent_fills"]
        assert service.day_trades()["rows"][0]["symbol"] == "XYZ"
        assert service.day_trades()["rows"][0]["closed"] is False

        other = AtlasDatabase(database.path)
        try:
            replayed = PaperBroker(initial_cash=10_000, database=other)
            assert replayed.positions()["XYZ"]["quantity"] == result["order"]["quantity"]
            assert other.list_paper_orders()[-1]["symbol"] == "XYZ"
        finally:
            other.close()

        closed = service.sell(service.token, "XYZ")
        assert closed["order"]["effect"] == "CLOSE_LONG"
        assert service.portfolio()["positions"] == []
    finally:
        if process.poll() is None:
            process.wait(timeout=15)
        database.close()
        clear_feed_pause()


def test_page_keeps_a_rejection_beside_the_order_buttons():
    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert 'name="atlas-dashboard-version" content="29"' in page
    assert 'id="order-note"' in page
    ideas = page.split("function renderIdeas", 1)[1].split("function ", 1)[0]
    assert "candidate-note" in ideas
    assert "order-note" not in ideas
    ask_buy = page.split("async function askBuy", 1)[1].split("async function askSell", 1)[0]
    ask_sell = page.split("async function askSell", 1)[1].split("async function askAlert", 1)[0]
    for block in (ask_buy, ask_sell):
        assert "setOrderNote(error.message" in block
        assert "candidate-note" not in block
    refresh = page.split("async function refresh", 1)[1].split("function scheduleScanFollowUp", 1)[0]
    assert "setOrderNote(state.orderNote, state.orderTone)" in refresh
