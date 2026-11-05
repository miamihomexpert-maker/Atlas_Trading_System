from atlas.strategy import evaluate_market

from atlas.technical import calculate_sma, calculate_ema, calculate_rsi, calculate_macd


def run_pipeline(history):
    strategy_result = evaluate_market(history)

    if strategy_result["status"] != "READY":
        return {
            "strategy": strategy_result,
            "technical": None,
        }

    sma = calculate_sma(history)
    ema = calculate_ema(history)
    rsi = calculate_rsi(history)
    macd = calculate_macd(history)

    technical = {
        "sma": sma,
        "ema": ema,
        "rsi": rsi,
        "macd": macd,
}

    strategy_result = evaluate_market(history, technical)

    return {
        "strategy": strategy_result,
        "technical": technical,
}


def run_multi_timeframe_pipeline(histories, minimum_agreement=2):
    """Analyze provided intervals independently and require directional agreement."""
    if not isinstance(histories, dict) or not histories:
        raise ValueError("Provide market history for at least one timeframe.")
    if (
        isinstance(minimum_agreement, bool)
        or not isinstance(minimum_agreement, int)
        or minimum_agreement < 1
    ):
        raise ValueError("minimum_agreement must be a positive integer.")

    analyses = {
        timeframe: run_pipeline(history)
        for timeframe, history in histories.items()
    }
    ready = {
        timeframe: result["strategy"]
        for timeframe, result in analyses.items()
        if result["strategy"]["status"] == "READY"
    }
    directional = {
        timeframe: result["signal"]
        for timeframe, result in ready.items()
        if result["signal"] in {"BUY", "SELL"}
    }
    counts = {
        direction: sum(value == direction for value in directional.values())
        for direction in ("BUY", "SELL")
    }
    decision = "WAIT"
    if len(ready) >= minimum_agreement:
        for direction in ("BUY", "SELL"):
            if counts[direction] >= minimum_agreement and counts[direction] > counts[
                "SELL" if direction == "BUY" else "BUY"
            ]:
                decision = direction
                break

    prices = [
        result.get("price")
        for result in ready.values()
        if isinstance(result.get("price"), (int, float))
    ]
    return {
        "status": "READY" if ready else "NO_DATA",
        "signal": decision,
        "timeframes": analyses,
        "agreement": counts,
        "ready_timeframes": len(ready),
        "price": prices[-1] if prices else None,
        "reason": (
            "Insufficient timeframe agreement."
            if decision == "WAIT"
            else f"{decision} confirmed by {counts[decision]} timeframes."
        ),
    }


def run_adaptive_pipeline(history, database, instrument, news_impact=None):
    """Require the active, validated model and heuristic strategy to agree."""
    from atlas.adaptive import latest_model_probability
    from atlas.news import apply_news_context
    from atlas.signals import signal_from_analysis

    result = run_pipeline(history)
    active = database.active_model() if database is not None else None
    if active is None or result["strategy"]["status"] != "READY":
        result["adaptive"] = {
            "status": "NO_ACTIVE_MODEL" if active is None else "DATA_UNAVAILABLE",
            "prediction": None,
        }
        result["recommendation"] = None
        return result

    prediction = latest_model_probability(history, active["model"])
    strategy_result = dict(result["strategy"])
    strategy_signal = strategy_result["signal"]
    if prediction["signal"] in {"BUY", "SELL"} and prediction["signal"] == strategy_signal:
        strategy_result["confidence"] = round(
            min(
                strategy_result.get("confidence", 0.0),
                prediction["probability_up"]
                if prediction["signal"] == "BUY"
                else 1 - prediction["probability_up"],
            ),
            4,
        )
        strategy_result["reason"] = (
            f"{strategy_result.get('reason', '')} "
            f"Confirmed by active model {active['version']}."
        ).strip()
    else:
        strategy_result["signal"] = "WAIT"
        strategy_result["reason"] = (
            f"{strategy_result.get('reason', '')} "
            "Model and strategy did not agree; no directional recommendation."
        ).strip()
    if news_impact is not None:
        strategy_result = apply_news_context(strategy_result, news_impact)

    result["strategy"] = strategy_result
    result["adaptive"] = {
        "status": "ACTIVE",
        "version": active["version"],
        "prediction": prediction,
    }
    result["recommendation"] = signal_from_analysis(instrument, strategy_result)
    return result
