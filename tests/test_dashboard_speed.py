"""Stored dashboard reads stay fast when Yahoo is slow or rate limited."""

import json
import threading
import time

from atlas.dashboard import PaperDashboard, _state_payload
from atlas.database import AtlasDatabase
from atlas.market_data import (
    clear_feed_pause,
    feed_is_paused,
    get_stock_history,
    note_feed_failure,
    pause_note,
)
from atlas.news_feed import fetch_headlines
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


def _settings(*symbols):
    return Settings(
        instruments=symbols,
        paper_initial_cash=10_000,
        live_trading_enabled=False,
    )


def _stored_card():
    return {
        "symbol": "XYZ",
        "decision": "WAIT",
        "reason": "Stored from the last scan.",
        "price": 101.0,
        "entry": None,
        "stop": None,
        "target": None,
        "quantity": None,
        "confidence": None,
        "reasons": ["Stored from the last scan."],
        "buyable": False,
        "shortable": False,
        "agreement": None,
        "scanned_at": "2026-11-10T15:00:00+00:00",
        "timeframes": {"1h": {"signal": "WAIT", "trend": "BEARISH"}},
        "news": {
            "status": "FETCHED",
            "headlines": ["Stored headline"],
            "items": [{"title": "Stored headline"}],
            "source": "stored",
        },
        "model": {
            "status": "CANDIDATE_NOT_PROMOTED",
            "reason": None,
            "version": None,
            "probability_up": None,
            "signal": "WAIT",
        },
        "company": {"symbol": "XYZ", "status": "NOT_LOADED"},
        "behavior": {"status": "NOT_LOADED", "insiders": [], "holders": []},
    }


def _live_service(tmp_path):
    database = AtlasDatabase(tmp_path / "speed.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=_settings("XYZ"),
        database=database,
        histories_by_symbol=None,
        news_events=[],
        hard_stop=False,
    )
    database.upsert_symbol_scan("XYZ", _stored_card())
    return service, database


def test_state_payload_returns_stored_cards_without_yahoo(tmp_path, monkeypatch):
    service, database = _live_service(tmp_path)
    calls = []

    def fetch(symbol, timeframes):
        calls.append((symbol, tuple(timeframes)))
        time.sleep(2)
        return {"1h": recent_history(100, 1)}

    def news(symbols):
        calls.append(("news", tuple(symbols)))
        time.sleep(2)
        return [], {"status": "FETCHED", "error": None, "source": "test", "event_count": 0}

    def holders(symbol):
        calls.append(("holders", symbol))
        time.sleep(2)
        return {"status": "FETCHED", "insiders": [], "holders": []}

    monkeypatch.setattr("atlas.dashboard.fetch_session_histories", fetch)
    monkeypatch.setattr("atlas.dashboard.resolve_news", news)
    monkeypatch.setattr(service, "behavior_loader", holders)
    monkeypatch.setattr(service, "company_loader", holders)

    started = time.perf_counter()
    payload = _state_payload(service, force_scan=False)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert calls == []
    assert payload["portfolio"]["mode"] == "PAPER"
    assert payload["portfolio"]["cash"] == 10_000
    assert payload["clock"]["timezone"] == "America/New_York"
    assert payload["review"]["fills"] == []
    card = payload["scans"][0]
    assert card["symbol"] == "XYZ"
    assert card["decision"] == "WAIT"
    assert card["reason"] == "Stored from the last scan."
    assert card["news"]["headlines"] == ["Stored headline"]
    assert payload["scan_pending"] is False
    database.close()


def test_refresh_now_does_not_wait_and_a_paused_pass_keeps_cards(tmp_path, monkeypatch):
    clear_feed_pause()
    service, database = _live_service(tmp_path)
    calls = []
    gate = threading.Event()

    def fetch(symbol, timeframes):
        calls.append(symbol)
        gate.wait(3)
        return {"1h": recent_history(100, 1), "1d": recent_history(100, 1)}

    monkeypatch.setattr("atlas.dashboard.fetch_session_histories", fetch)
    try:
        started = time.perf_counter()
        payload = _state_payload(service, force_scan=True)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5
        assert payload["scans"][0]["reason"] == "Stored from the last scan."
        assert payload["portfolio"]["mode"] == "PAPER"
        assert payload["clock"]["timezone"] == "America/New_York"
        assert payload["scan_pending"] is True
        deadline = time.perf_counter() + 1
        while not calls and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert calls == ["XYZ"]
        gate.set()
        assert service.wait_for_scan(5)
    finally:
        gate.set()
        service.wait_for_scan(5)
        clear_feed_pause()
        database.close()


def test_rate_limited_batch_scores_zero_and_skips_later_fetches(tmp_path, monkeypatch):
    clear_feed_pause()
    service, database = _live_service(tmp_path)
    calls = []

    def fetch(symbol, timeframes):
        calls.append(symbol)
        raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")

    monkeypatch.setattr("atlas.dashboard.fetch_session_histories", fetch)
    try:
        started = time.perf_counter()
        service._scan_one_batch()
        assert time.perf_counter() - started < 0.5
        assert calls == ["XYZ"]
        assert feed_is_paused()
        stored = database.list_symbol_scans()[0]
        assert stored["decision"] == "WAIT"
        assert stored["news"]["headlines"] == ["Stored headline"]
        coverage = json.loads(database.get_metadata("scan_coverage"))
        assert coverage["batch_count"] == 0
        payload = _state_payload(service, force_scan=False)
        assert "This pass scored 0 names." in payload["candidate_explanation"]
        assert payload["scans"][0]["symbol"] == "XYZ"
        assert calls == ["XYZ"]
    finally:
        clear_feed_pause()
        database.close()


def test_paused_candle_request_returns_without_a_download(tmp_path, monkeypatch):
    clear_feed_pause()
    database = AtlasDatabase(tmp_path / "candles.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=_settings("XYZ"),
        database=database,
        histories_by_symbol=None,
        news_events=[],
        hard_stop=False,
    )
    calls = []
    monkeypatch.setattr(
        "atlas.dashboard.fetch_session_histories",
        lambda symbol, timeframes: calls.append(symbol),
    )
    note_feed_failure(RuntimeError("Too Many Requests. Rate limited. Try after a while."))
    try:
        started = time.perf_counter()
        payload = service.candles("XYZ", "1m")
        assert time.perf_counter() - started < 0.5
        assert calls == []
        assert payload["candles"] == []
        assert "no bars were returned" in payload["error"].lower()
        assert "rate limited" in payload["error"].lower()
        assert payload["feed_paused"] is True
        assert payload["retry_after_seconds"] > 0
    finally:
        clear_feed_pause()
        database.close()


def test_rate_limit_backs_off_and_a_normal_skip_does_not(monkeypatch):
    clear_feed_pause()
    calls = {"n": 0}

    class RateTicker:
        def __init__(self, symbol):
            calls["n"] += 1

        def history(self, period, interval):
            raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")

    monkeypatch.setattr("atlas.market_data.yf.Ticker", RateTicker)
    try:
        assert get_stock_history("XYZ", period="5d", interval="1h") is None
        assert feed_is_paused()
        assert "rate limited" in (pause_note() or "").lower()
        assert get_stock_history("ABC", period="5d", interval="1h") is None
        assert calls["n"] == 1
    finally:
        clear_feed_pause()

    calls["n"] = 0

    class BrokenTicker:
        def __init__(self, symbol):
            calls["n"] += 1

        def history(self, period, interval):
            raise ValueError("Market history contains invalid OHLCV bar values.")

    monkeypatch.setattr("atlas.market_data.yf.Ticker", BrokenTicker)
    try:
        assert get_stock_history("XYZ", period="5d", interval="1h") is None
        assert feed_is_paused() is False
        assert get_stock_history("ABC", period="5d", interval="1h") is None
        assert calls["n"] == 2
    finally:
        clear_feed_pause()


def test_history_timeout_fails_fast_and_pauses(monkeypatch):
    clear_feed_pause()
    monkeypatch.setattr("atlas.market_data.HISTORY_TIMEOUT_SECONDS", 0.2)

    class SlowTicker:
        def __init__(self, symbol):
            pass

        def history(self, period, interval):
            time.sleep(5)
            return None

    monkeypatch.setattr("atlas.market_data.yf.Ticker", SlowTicker)
    try:
        started = time.perf_counter()
        assert get_stock_history("XYZ", period="1d", interval="1m") is None
        assert time.perf_counter() - started < 1.5
        assert feed_is_paused()
        assert "timed out" in (pause_note() or "").lower()
        started = time.perf_counter()
        assert get_stock_history("XYZ", period="1d", interval="1m") is None
        assert time.perf_counter() - started < 0.5
    finally:
        clear_feed_pause()


def test_paused_news_fetch_does_not_call_yahoo():
    clear_feed_pause()
    note_feed_failure(RuntimeError("Too Many Requests. Rate limited. Try after a while."))
    try:
        events, report = fetch_headlines(
            ["XYZ"],
            ticker_factory=lambda _symbol: (_ for _ in ()).throw(AssertionError("news fetched")),
        )
        assert events == []
        assert report["status"] == "FETCH_FAILED"
        assert "rate limited" in (report["error"] or "").lower()
    finally:
        clear_feed_pause()
