import threading

import pandas as pd

from atlas import scanner


def test_watchlist_scanning_returns_recommendations_without_execution(monkeypatch):
    history = pd.DataFrame(
        {
            "Open": range(100, 140),
            "High": range(100, 140),
            "Low": range(100, 140),
            "Close": range(100, 140),
            "Volume": [1_000_000] * 40,
        }
    )
    monkeypatch.setattr(
        scanner,
        "get_multi_timeframe_history",
        lambda symbol, timeframes, period: {
            "5m": history,
            "1h": history,
        },
    )

    result = scanner.scan_watchlist(["xyz"], timeframes=("5m", "1h"))

    assert result[0]["symbol"] == "XYZ"
    assert result[0]["analysis"]["signal"] == "BUY"
    assert result[0]["recommendation"]["instrument"] == "XYZ"
    assert result[0]["execution"] == "NOT_SUBMITTED"


def test_scheduled_scanner_is_interruptible_and_callbacks_receive_results(monkeypatch):
    calls = []
    stop = threading.Event()
    monkeypatch.setattr(
        scanner,
        "scan_watchlist",
        lambda symbols, timeframes: [{"symbol": "XYZ", "status": "READY"}],
    )

    def receive(results):
        calls.extend(results)
        stop.set()

    cycles = scanner.run_scheduled_scanner(
        ["XYZ"],
        stop,
        on_results=receive,
    )

    assert cycles == 1
    assert calls == [{"symbol": "XYZ", "status": "READY"}]
