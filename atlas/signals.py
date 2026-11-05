from datetime import datetime, timezone
from math import isfinite


def create_trade_signal(
    symbol,
    direction,
    entry_price,
    stop_loss,
    profit_target,
    explanation,
    timestamp=None,
    market_timestamp=None,
):
    """Build a validated, recommendation-only trade signal."""
    symbol = str(symbol).strip().upper()
    direction = str(direction).strip().upper()
    if not symbol:
        raise ValueError("Instrument symbol is required.")
    if direction not in {"BUY", "SELL"}:
        raise ValueError("Direction must be BUY or SELL.")
    if not all(
        isfinite(value) and value > 0
        for value in (entry_price, stop_loss, profit_target)
    ):
        raise ValueError("Entry, stop-loss, and profit-target prices must be positive.")
    if direction == "BUY" and not stop_loss < entry_price < profit_target:
        raise ValueError("BUY signals require stop < entry < target.")
    if direction == "SELL" and not profit_target < entry_price < stop_loss:
        raise ValueError("SELL signals require target < entry < stop.")

    risk = abs(entry_price - stop_loss)
    reward = abs(profit_target - entry_price)
    return {
        "instrument": symbol,
        "direction": direction,
        "entry_price": float(entry_price),
        "stop_loss": float(stop_loss),
        "profit_target": float(profit_target),
        "risk_reward_ratio": round(reward / risk, 4),
        "explanation": str(explanation).strip(),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "market_timestamp": market_timestamp,
    }


def signal_from_analysis(symbol, strategy_result, stop_distance=None):
    """Translate a directional analysis into a proposed, never-executed signal."""
    if strategy_result.get("status") != "READY":
        return None
    direction = strategy_result.get("signal")
    if direction not in {"BUY", "SELL"}:
        return None

    entry = strategy_result.get("price")
    if not isinstance(entry, (int, float)) or not isfinite(entry) or entry <= 0:
        raise ValueError("Directional analysis has no valid entry price.")
    if stop_distance is None:
        observations = strategy_result.get("observations", {})
        volatility = observations.get("volatility_value", 0.01)
        if not isinstance(volatility, (int, float)) or not isfinite(volatility):
            volatility = 0.01
        stop_distance = max(entry * 0.01, entry * volatility * 2)
    if not isfinite(stop_distance) or stop_distance <= 0:
        raise ValueError("Stop distance must be a positive finite number.")

    if direction == "BUY":
        stop_loss = entry - stop_distance
        profit_target = entry + 2 * stop_distance
    else:
        stop_loss = entry + stop_distance
        profit_target = entry - 2 * stop_distance
    if stop_loss <= 0 or profit_target <= 0:
        return None

    return create_trade_signal(
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        stop_loss=stop_loss,
        profit_target=profit_target,
        explanation=strategy_result.get("reason", "Technical analysis signal."),
        market_timestamp=strategy_result.get("market_timestamp"),
    )


def get_signals_status():
    return "SIGNALS MODULE ONLINE"
