from datetime import datetime, timezone
import os

from atlas.adaptive import latest_model_probability
from atlas.config import LIVE_TRADING
from atlas.execution import execute_paper_signal
from atlas.news import apply_news_context, assess_news_impact
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


def _remote_approval_required(explicit):
    if explicit is not None:
        return bool(explicit)
    setting = os.getenv("ATLAS_REQUIRE_REMOTE_APPROVAL", "false").strip().lower()
    if setting in {"1", "true", "yes"}:
        return True
    if setting in {"0", "false", "no", ""}:
        return False
    raise ValueError("ATLAS_REQUIRE_REMOTE_APPROVAL must be a boolean value.")


def _decision_history(histories):
    if "1d" in histories:
        return histories["1d"]
    return max(histories.values(), key=len)


def _apply_model_and_news(scanned, histories, symbol, database, news_events):
    """Confirm a technical decision with the active model and news, if present.

    A missing model does not block the decision. An active model can confirm
    BUY or SELL and otherwise withholds the recommendation. News can veto.
    Neither source invents a direction the technical scan did not already take.
    """
    analysis = scanned["analysis"]
    decision = {
        "status": analysis.get("status"),
        "signal": analysis.get("signal"),
        "price": analysis.get("price"),
        "reason": analysis.get("reason", ""),
        "confidence": None,
    }
    recommendation = scanned["recommendation"]
    if recommendation is not None:
        decision["market_timestamp"] = recommendation.get("market_timestamp")

    adaptive = {"status": "NO_ACTIVE_MODEL", "prediction": None}
    active = database.active_model() if database is not None else None
    if active is not None:
        try:
            prediction = latest_model_probability(
                _decision_history(histories),
                active["model"],
            )
        except ValueError as error:
            adaptive = {
                "status": "DATA_UNAVAILABLE",
                "version": active["version"],
                "prediction": None,
                "reason": str(error),
            }
            decision["signal"] = "WAIT"
            decision["reason"] = (
                f"{decision['reason']} Active model could not score this history."
            ).strip()
            recommendation = None
        else:
            adaptive = {
                "status": "ACTIVE",
                "version": active["version"],
                "prediction": prediction,
            }
            if decision["signal"] in {"BUY", "SELL"} and prediction["signal"] != decision["signal"]:
                decision["signal"] = "WAIT"
                decision["reason"] = (
                    f"{decision['reason']} Model and strategy did not agree; "
                    "no directional recommendation."
                ).strip()
                recommendation = None
            elif decision["signal"] in {"BUY", "SELL"}:
                decision["reason"] = (
                    f"{decision['reason']} Confirmed by active model {active['version']}."
                ).strip()

    impact = assess_news_impact(list(news_events or []), symbol)
    decision = apply_news_context(decision, impact)
    if decision.get("signal") not in {"BUY", "SELL"}:
        recommendation = None
    elif recommendation is not None:
        recommendation = dict(recommendation)
        recommendation["explanation"] = decision.get("reason") or recommendation.get("explanation")
    return decision, recommendation, adaptive, impact


def run_paper_pipeline(
    symbol,
    histories,
    broker,
    client_order_id,
    approved=False,
    minimum_agreement=2,
    limits=None,
    max_data_age_seconds=900,
    approval_token=None,
    approval_chat_id=None,
    approval_database=None,
    require_remote_approval=None,
    database=None,
    news_events=None,
):
    """Scan, then send one recommendation through risk, approval, and the paper broker.

    Technical timeframe agreement is confirmed by an active model when one
    exists, and by caller-supplied news. Telegram approval stays optional
    unless ``require_remote_approval`` is set or ``ATLAS_REQUIRE_REMOTE_APPROVAL``
    is true. Live trading is refused.
    """
    if LIVE_TRADING:
        raise PermissionError("Live trading is disabled.")
    if broker.account_info().get("trading_mode") != "PAPER":
        raise PermissionError("Live trading is disabled.")

    from atlas.scanner import scan_timeframes

    scanned = scan_timeframes(
        symbol,
        histories,
        minimum_agreement=minimum_agreement,
    )
    decision, recommendation, adaptive, news_impact = _apply_model_and_news(
        scanned,
        histories,
        symbol,
        database,
        news_events,
    )
    result = {
        "symbol": scanned["symbol"],
        "analysis": scanned["analysis"],
        "technical_signal": scanned["analysis"].get("signal"),
        "decision": decision.get("signal"),
        "recommendation": recommendation,
        "adaptive": adaptive,
        "news": news_impact,
        "order": None,
        "execution": "NOT_SUBMITTED",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    if recommendation is None:
        return result
    if _remote_approval_required(require_remote_approval) and approval_token is None:
        raise PermissionError("Remote approval token is required.")

    result["order"] = execute_paper_signal(
        recommendation,
        broker,
        client_order_id,
        approved=approved,
        limits=limits,
        max_data_age_seconds=max_data_age_seconds,
        approval_token=approval_token,
        approval_chat_id=approval_chat_id,
        approval_database=approval_database or database,
    )
    result["execution"] = result["order"]["status"]
    return result
