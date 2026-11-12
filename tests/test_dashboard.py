from pathlib import Path
import json
import threading
from http.client import HTTPConnection
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from atlas.broker import PaperBroker
from atlas.dashboard import PaperDashboard, _state_payload, describe_scan, make_server
from atlas.database import AtlasDatabase
from atlas.risk import calculate_position_size
from atlas.session import run_paper_session
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


def _bars(start, step):
    frame = recent_history(start, step)
    return {"1h": frame, "1d": frame.copy()}


def _settings(*symbols, cash=10_000.0):
    return Settings(instruments=symbols, paper_initial_cash=cash, live_trading_enabled=False)


def _service(tmp_path, histories, symbols=("XYZ",), cash=10_000.0):
    database = AtlasDatabase(tmp_path / "dash.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=_settings(*symbols, cash=cash),
        database=database,
        histories_by_symbol=histories,
        news_events=[],
        hard_stop=False,
        candle_loader=lambda symbol, timeframe: histories[symbol][timeframe],
    )
    return service, database


def test_top_buy_list_does_not_pad_when_fewer_than_three_qualify(tmp_path):
    histories = {
        "XYZ": _bars(100, 1),
        "ABC": _bars(140, -1),
    }
    service, database = _service(tmp_path, histories, symbols=("XYZ", "ABC", "DEF"))

    ranked = service.candidates()

    assert ranked["qualified_count"] == 1
    assert [item["symbol"] for item in ranked["candidates"]] == ["XYZ"]
    candidate = ranked["candidates"][0]
    assert candidate["entry"] > candidate["stop"]
    assert candidate["target"] > candidate["entry"]
    assert candidate["quantity"] > 0
    assert candidate["reasons"]
    assert "DEF" in ranked["explanation"]
    assert "ABC" in ranked["explanation"]
    assert len(ranked["candidates"]) < 3
    database.close()


def test_dashboard_buy_requires_approval_token_and_obeys_risk(tmp_path):
    histories = {"XYZ": _bars(100, 1)}
    service, database = _service(tmp_path, histories)
    with pytest.raises(PermissionError, match="token"):
        service.buy("wrong-token", "XYZ")

    result = service.buy(service.token, "XYZ")
    broker = service._broker()
    order = result["order"]
    assert order["status"] == "FILLED"
    assert order["side"] == "BUY"
    assert order["quantity"] == calculate_position_size(
        10_000,
        order["price"],
        broker.positions()["XYZ"]["stop_loss"],
        current_exposure=0,
    )
    assert order["quantity"] * order["price"] <= 10_000 * 0.10 + 1e-6
    events = database.list_audit_events()
    assert any(event["action"] == "DASHBOARD_BUY_CLICK" and event["timestamp"] for event in events)
    database.close()


def test_dashboard_respects_daily_loss_halt_and_live_refusal(monkeypatch, tmp_path):
    histories = {"XYZ": _bars(100, 1)}
    service, database = _service(tmp_path, histories)
    broker = service._broker()
    broker.submit_order(
        "XYZ", "BUY", 10, 100, "seed", approved=True, stop_loss=90, profit_target=130,
    )
    broker.update_market_price("XYZ", 70)

    with pytest.raises(PermissionError, match="Daily loss"):
        service.buy(service.token, "XYZ")
    assert service._broker().positions()["XYZ"]["quantity"] == 10

    monkeypatch.setattr("atlas.dashboard.LIVE_TRADING", True)
    with pytest.raises(PermissionError, match="Live trading"):
        service.buy(service.token, "XYZ")
    assert service.portfolio()["mode"] == "PAPER"
    database.close()


def test_http_order_route_rejects_missing_token_and_ignores_client_size(tmp_path):
    histories = {"XYZ": _bars(100, 1)}
    service, database = _service(tmp_path, histories)
    server = make_server(service, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        page = urlopen(base + "/").read().decode("utf-8")
        assert "PAPER" in page
        assert "RAIENS" not in page
        assert service.token in page
        state = json.loads(urlopen(base + "/api/state").read().decode("utf-8"))
        assert state["portfolio"]["mode"] == "PAPER"
        assert state["portfolio"]["live_trading"] is False
        assert "token" not in state["portfolio"]
        blob = json.dumps(state)
        assert "TELEGRAM" not in blob
        assert service.token not in blob

        denied = Request(
            base + "/api/buy",
            data=json.dumps({"symbol": "XYZ", "quantity": 999999}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as denied_error:
            urlopen(denied)
        assert denied_error.value.code == 403

        remote = HTTPConnection(host, port, timeout=5)
        remote.request(
            "POST",
            "/api/buy",
            body=json.dumps({"symbol": "XYZ"}),
            headers={
                "Content-Type": "application/json",
                "Host": "evil.example",
                "X-Atlas-Token": service.token,
            },
        )
        remote_response = remote.getresponse()
        assert remote_response.status == 403
        remote_response.read()
        remote.close()

        accepted = Request(
            base + "/api/buy",
            data=json.dumps({"symbol": "XYZ", "quantity": 999999}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Atlas-Token": service.token,
            },
            method="POST",
        )
        body = json.loads(urlopen(accepted).read().decode("utf-8"))
        assert body["order"]["quantity"] < 999999
        assert body["order"]["quantity"] * body["order"]["price"] <= 10_000 * 0.10 + 1e-6
    finally:
        server.shutdown()
        server.server_close()
        database.close()


def test_manual_loop_recommends_exits_unless_hard_stop_is_on(tmp_path):
    database = AtlasDatabase(tmp_path / "manual.sqlite3")
    broker = PaperBroker(initial_cash=10_000, database=database)
    broker.submit_order(
        "XYZ", "BUY", 1, 100, "seed", approved=True, stop_loss=90, profit_target=120,
    )
    recommended = run_paper_session(
        settings=_settings("XYZ"),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": _bars(120, -1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
        manual_exits=True,
        hard_stop=False,
    )
    assert recommended["fill_count"] == 0
    assert recommended["results"][0]["execution"] == "RECOMMENDED"
    assert recommended["results"][0]["exit_reason"] == "stop"
    assert broker.positions()["XYZ"]["quantity"] == 1
    assert database.list_exit_alerts()[0]["reason"] == "stop"

    forced = run_paper_session(
        settings=_settings("XYZ"),
        broker=broker,
        database=database,
        histories_by_symbol={"XYZ": _bars(120, -1)},
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
        manual_exits=True,
        hard_stop=True,
    )
    assert forced["results"][0]["exit_reason"] == "stop"
    assert forced["results"][0]["order"]["side"] == "SELL"
    assert broker.positions() == {}
    assert any(event["action"] == "HARD_STOP_EXECUTED" for event in database.list_audit_events())
    database.close()


def test_wait_card_explains_why_buy_stays_disabled(tmp_path):
    histories = {
        "XYZ": {"1h": recent_history(100, 1), "1d": recent_history(140, -1)},
        "ABC": _bars(140, -1),
    }
    service, database = _service(tmp_path, histories, symbols=("XYZ", "ABC"))
    service.name_loader = lambda symbol: "Example Fund" if symbol == "XYZ" else symbol
    ranked = service.candidates()
    cards = {card["symbol"]: card for card in ranked["scans"]}

    assert list(cards) == ["XYZ", "ABC"]
    assert len(ranked["candidates"]) <= 2
    waiting = cards["XYZ"]
    assert waiting["buyable"] is False
    assert waiting["copy"]["buy_enabled"] is False
    assert "wait" in waiting["copy"]["decision"].lower() or "not a buy" in waiting["copy"]["buy_note"].lower()
    assert waiting["copy"]["why"]
    assert waiting["copy"]["timeframes"]
    assert waiting["name"] == "Example Fund"
    assert cards["ABC"]["name"] is None
    assert "FETCH_FAILED" not in waiting["copy"]["news"]

    with pytest.raises(ValueError, match="not a current BUY"):
        service.buy(service.token, "XYZ")
    assert service._broker().positions() == {}
    database.close()


def test_plain_language_does_not_invent_a_probability_or_headlines():
    copy = describe_scan({
        "decision": "WAIT",
        "reason": "Insufficient timeframe agreement.",
        "buyable": False,
        "timeframes": {
            "1h": {"signal": "WAIT", "trend": "BEARISH", "bullish_score": 2, "bearish_score": 3},
            "1d": {"signal": "SELL", "trend": "BEARISH", "bullish_score": 0, "bearish_score": 4},
        },
        "news": {"status": "FETCH_FAILED", "headlines": []},
        "model": {
            "status": "CANDIDATE_NOT_PROMOTED",
            "probability_up": 0.5442,
            "signal": "WAIT",
        },
    })
    assert "timeframes do not agree" in copy["why"]
    assert "The news feed did not return headlines." == copy["news"]
    assert "did not pass the checks" in copy["model"]
    assert "54%" in copy["model"]
    assert copy["buy_enabled"] is False

    quiet = describe_scan({
        "decision": "WAIT",
        "reason": "Insufficient timeframe agreement.",
        "buyable": False,
        "timeframes": {},
        "news": {"status": "FETCH_FAILED", "headlines": []},
        "model": {"status": "CANDIDATE_NOT_PROMOTED", "probability_up": None, "signal": "WAIT"},
    })
    assert "chance" not in quiet["model"]


def test_trade_buttons_are_always_in_the_page_template():
    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert 'id="action-buy"' in page
    assert 'id="action-sell"' in page
    assert 'id="trade-actions"' in page
    assert "data-pick=" in page
    assert "No open paper position." in page
    assert "${scan.buyable ? `<button" not in page


def test_one_bad_symbol_does_not_blank_dashboard_state(tmp_path, monkeypatch):
    database = AtlasDatabase(tmp_path / "bad-bar.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=_settings("AAA", "BBB"),
        database=database,
        histories_by_symbol=None,
        news_events=[],
        hard_stop=False,
    )

    def fetch(symbol, timeframes):
        if symbol == "AAA":
            raise ValueError("Market history contains invalid OHLCV bar values.")
        return _bars(100, 1)

    monkeypatch.setattr("atlas.dashboard.fetch_session_histories", fetch)
    payload = _state_payload(service, force_scan=True)

    assert payload["clock"]["timezone"] == "America/New_York"
    assert payload["portfolio"]["mode"] == "PAPER"
    assert payload["portfolio"]["cash"] == 10_000
    assert service.wait_for_scan(5)
    payload = _state_payload(service)
    stored = {card["symbol"]: card for card in database.list_symbol_scans()}
    assert stored["AAA"]["decision"] == "NO_DATA"
    assert "invalid OHLCV" in stored["AAA"]["reason"]
    assert stored["BBB"]["symbol"] == "BBB"
    assert payload["scans"]
    database.close()


def test_paper_loop_continues_after_one_bad_history(tmp_path, monkeypatch):
    database = AtlasDatabase(tmp_path / "loop-bad.sqlite3")
    broker = PaperBroker(initial_cash=10_000, database=database)

    def fetch(symbol, timeframes):
        if symbol == "AAA":
            raise ValueError("Market history contains invalid OHLCV bar values.")
        return _bars(100, 1)

    monkeypatch.setattr("atlas.session.fetch_session_histories", fetch)
    summary = run_paper_session(
        settings=_settings("AAA", "BBB"),
        broker=broker,
        database=database,
        news_events=[],
        approved=False,
        timeframes=("1h", "1d"),
    )

    symbols = [item["symbol"] for item in summary["results"]]
    assert "AAA" in symbols
    assert "BBB" in symbols
    assert summary["live_trading"] is False
    database.close()


def test_minute_chart_is_display_only(tmp_path, monkeypatch):
    monkeypatch.delenv("ATLAS_TIMEFRAMES", raising=False)
    frame = recent_history(100, 1)
    periods = {}

    def history(symbol, period, interval):
        periods[interval] = period
        return frame

    monkeypatch.setattr("atlas.market_data.get_stock_history", history)
    from atlas.session import fetch_session_histories

    fetched = fetch_session_histories("XYZ", ("1m", "1h"))
    assert set(fetched) == {"1m", "1h"}
    assert periods["1m"] == "5d"
    assert periods["1h"] == "1mo"
    service, database = _service(tmp_path, {"XYZ": {"1h": frame, "1d": frame.copy()}})
    calls = []

    def fetch(symbol, timeframes):
        calls.append((symbol, tuple(timeframes)))
        return {"1m": frame}

    monkeypatch.setattr("atlas.dashboard.fetch_session_histories", fetch)
    service.candle_loader = None
    payload = service.candles("XYZ", "1m")

    assert service.timeframes == ("1h", "1d")
    assert "1m" not in service.portfolio()["timeframes"]
    assert service.chart_timeframes() == ["1m", "1h", "1d"]
    assert calls == [("XYZ", ("1m",))]
    assert payload["chart_only"] is True
    assert payload["refresh_seconds"] == 15
    assert payload["error"] is None
    assert payload["candles"][-1]["close"] == frame["Close"].iloc[-1]
    assert "not a tick stream" in payload["feed_note"]
    with pytest.raises(ValueError, match="not available"):
        service.candles("XYZ", "5m")

    def fail(symbol, timeframes):
        raise ValueError("Market history contains invalid OHLCV bar values.")

    monkeypatch.setattr("atlas.dashboard.fetch_session_histories", fail)
    failed = service.candles("XYZ", "1m")
    assert failed["candles"] == []
    assert "invalid OHLCV" in failed["error"]
    assert service.timeframes == ("1h", "1d")
    state = _state_payload(service)
    assert state["chart_timeframes"] == ["1m", "1h", "1d"]
    assert state["chart_refresh_seconds"] == 15
    assert state["portfolio"]["timeframes"] == ["1h", "1d"]
    assert state["clock"]["timezone"] == "America/New_York"
    database.close()


def test_chart_poll_does_not_scan_and_news_text_is_readable():
    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert 'name="atlas-dashboard-version" content="26"' in page
    assert "const CHART_REFRESH_MS = 15000;" in page
    start = page.index("if (!sessionStillOpen()) return;")
    block = page[start:page.index("CHART_REFRESH_MS);", start)]
    assert "loadCandles({ live: true })" in block
    assert "feedPausedUntil" in block
    assert "scan=1" not in block
    assert "/api/state" not in block
    assert "#news a { color: #ffd56a; text-decoration: underline; }" in page
    assert "#news h3, #news p, #news .clip { color: #f7fbff; }" in page
    assert "#news .muted { color: #d7e4ef; }" in page
    assert "TOP 3 SCAN CANDIDATES" in page
