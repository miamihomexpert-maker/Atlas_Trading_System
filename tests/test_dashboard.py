import json
import threading
from http.client import HTTPConnection
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from atlas.broker import PaperBroker
from atlas.dashboard import PaperDashboard, make_server
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
