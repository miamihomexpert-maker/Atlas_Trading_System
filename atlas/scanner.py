from atlas.market_data import get_intraday_history, get_multi_timeframe_history
from atlas.pipeline import run_multi_timeframe_pipeline
from atlas.signals import signal_from_analysis


def get_scanner_status():
    return "MARKET SCANNER AVAILABLE; AUTOMATED SCHEDULING NOT CONFIGURED"


def scan_stock(symbol):
    history = get_intraday_history(symbol)

    if history is None or len(history) < 2:
        return {
            "symbol": symbol,
            "status": "INSUFFICIENT_DATA"
        }

    previous_close = float(history["Close"].iloc[-2])
    latest_close = float(history["Close"].iloc[-1])
    latest_volume = int(history["Volume"].iloc[-1])

    if previous_close <= 0:
        return {
            "symbol": symbol,
            "status": "INVALID_DATA"
        }

    change_percent = (
        (latest_close - previous_close) / previous_close
    ) * 100

    return {
        "symbol": symbol,
        "status": "READY",
        "price": round(latest_close, 2),
        "change_percent": round(change_percent, 3),
        "volume": latest_volume,
        "timestamp": str(history.index[-1]),
    }


def scan_market(symbols):
    results = []

    for symbol in symbols:
        try:
            results.append(scan_stock(symbol))
        except Exception as error:
            results.append({
                "symbol": symbol,
                "status": "ERROR",
                "error": str(error)
            })

    return results


def scan_timeframes(symbol, histories, minimum_agreement=2):
    """Analyze injected/fetched interval histories and create a proposal if aligned."""
    analysis = run_multi_timeframe_pipeline(
        histories,
        minimum_agreement=minimum_agreement,
    )
    signal = None
    if analysis["signal"] in {"BUY", "SELL"}:
        aligned = [
            result["strategy"]
            for result in analysis["timeframes"].values()
            if result["strategy"].get("signal") == analysis["signal"]
        ]
        if aligned:
            representative = dict(aligned[0])
            representative["signal"] = analysis["signal"]
            signal = signal_from_analysis(symbol, representative)
    return {
        "symbol": str(symbol).strip().upper(),
        "analysis": analysis,
        "recommendation": signal,
        "execution": "NOT_SUBMITTED",
    }


def scan_watchlist(symbols, timeframes=("5m", "15m", "1h", "1d"), period="1mo"):
    """Fetch and analyze each instrument without submitting orders."""
    results = []
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        try:
            histories = get_multi_timeframe_history(
                normalized,
                timeframes=timeframes,
                period=period,
            )
            if not histories:
                results.append(
                    {
                        "symbol": normalized,
                        "status": "NO_DATA",
                        "recommendation": None,
                    }
                )
                continue
            result = scan_timeframes(normalized, histories)
            result["status"] = result["analysis"]["status"]
            results.append(result)
        except Exception as error:
            results.append(
                {
                    "symbol": normalized,
                    "status": "ERROR",
                    "error": str(error),
                    "recommendation": None,
                }
            )
    return results


def run_scheduled_scanner(
    symbols,
    stop_event,
    interval_seconds=300,
    timeframes=("5m", "15m", "1h", "1d"),
    on_results=None,
    max_cycles=None,
):
    """Run explicit, interruptible scan cycles; this is never started implicitly."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive.")
    if max_cycles is not None and (
        isinstance(max_cycles, bool)
        or not isinstance(max_cycles, int)
        or max_cycles <= 0
    ):
        raise ValueError("max_cycles must be a positive integer when provided.")

    cycles = 0
    while not stop_event.is_set():
        results = scan_watchlist(symbols, timeframes=timeframes)
        if on_results is not None:
            on_results(results)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        if stop_event.wait(interval_seconds):
            break
    return cycles
