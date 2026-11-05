from math import isfinite

import pandas as pd

from atlas.strategy import evaluate_market


_REQUIRED_COLUMNS = {"Open", "High", "Low", "Close", "Volume"}


def _validate_history(history):
    if history is None or history.empty:
        raise ValueError("Historical OHLCV data is required.")
    missing = _REQUIRED_COLUMNS - set(history.columns)
    if missing:
        raise ValueError(f"Historical data is missing columns: {sorted(missing)}")
    values = history[list(_REQUIRED_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    if values.isna().any().any() or values.isin(
        [float("inf"), float("-inf")]
    ).any().any():
        raise ValueError("Historical OHLCV data must contain finite numeric values.")
    if (
        (values["Close"] <= 0).any()
        or (values["Volume"] < 0).any()
        or (values["High"] < values["Low"]).any()
        or (values["High"] < values["Open"]).any()
        or (values["High"] < values["Close"]).any()
        or (values["Low"] > values["Open"]).any()
        or (values["Low"] > values["Close"]).any()
    ):
        raise ValueError("Historical OHLCV data violates price or volume constraints.")
    return history.copy()


def _validate_parameters(initial_cash, position_fraction, commission_bps, slippage_bps):
    parameters = (initial_cash, position_fraction, commission_bps, slippage_bps)
    if not all(isfinite(value) for value in parameters):
        raise ValueError("Backtest parameters must be finite numbers.")
    if initial_cash <= 0:
        raise ValueError("Initial cash must be positive.")
    if not 0 < position_fraction <= 1:
        raise ValueError("Position fraction must be greater than 0 and at most 1.")
    if commission_bps < 0 or slippage_bps < 0:
        raise ValueError("Trading costs cannot be negative.")


def run_backtest(
    history,
    initial_cash=100_000.0,
    position_fraction=0.10,
    commission_bps=1.0,
    slippage_bps=5.0,
    start_index=20,
):
    """Backtest close-derived signals at the following bar's open.

    The strategy only receives data through the signal bar. Orders are filled
    on the next bar, with adverse slippage and commission applied on both sides.
    """
    data = _validate_history(history)
    _validate_parameters(
        initial_cash,
        position_fraction,
        commission_bps,
        slippage_bps,
    )
    if isinstance(start_index, bool) or not isinstance(start_index, int) or start_index < 0:
        raise ValueError("start_index must be a non-negative integer.")

    cash = float(initial_cash)
    position = None
    completed_trades = []
    equity_curve = []
    total_costs = 0.0
    commission_rate = commission_bps / 10_000
    slippage_rate = slippage_bps / 10_000
    first_signal_index = max(start_index, 20)

    def close_position(raw_price, timestamp):
        nonlocal cash, position, total_costs
        fill_price = raw_price * (1 - slippage_rate)
        exit_fee = position["quantity"] * fill_price * commission_rate
        proceeds = position["quantity"] * fill_price - exit_fee
        cash += proceeds
        trade_costs = position["entry_fee"] + exit_fee
        total_costs += exit_fee
        completed_trades.append(
            {
                "entry_timestamp": position["entry_timestamp"],
                "exit_timestamp": str(timestamp),
                "entry_price": position["entry_price"],
                "exit_price": fill_price,
                "quantity": position["quantity"],
                "net_pnl": proceeds - position["entry_cost"],
                "costs": trade_costs,
            }
        )
        position = None

    for signal_index in range(first_signal_index, len(data) - 1):
        signal_result = evaluate_market(data.iloc[: signal_index + 1])
        next_bar = data.iloc[signal_index + 1]
        fill_time = data.index[signal_index + 1]
        raw_open = float(next_bar["Open"])

        if position is None and signal_result.get("signal") == "BUY":
            fill_price = raw_open * (1 + slippage_rate)
            quantity = int(cash * position_fraction / fill_price)
            if quantity > 0:
                notional = quantity * fill_price
                entry_fee = notional * commission_rate
                if notional + entry_fee <= cash:
                    cash -= notional + entry_fee
                    total_costs += entry_fee
                    position = {
                        "quantity": quantity,
                        "entry_price": fill_price,
                        "entry_cost": notional + entry_fee,
                        "entry_fee": entry_fee,
                        "entry_timestamp": str(fill_time),
                    }
        elif position is not None and signal_result.get("signal") == "SELL":
            close_position(raw_open, fill_time)

        marked_equity = cash
        if position is not None:
            marked_equity += position["quantity"] * float(next_bar["Close"])
        equity_curve.append(
            {
                "timestamp": str(fill_time),
                "equity": round(marked_equity, 2),
            }
        )

    if position is not None:
        last_bar = data.iloc[-1]
        close_position(float(last_bar["Close"]), data.index[-1])
        if equity_curve:
            equity_curve[-1]["equity"] = round(cash, 2)

    equity_values = [float(point["equity"]) for point in equity_curve]
    if not equity_values:
        equity_values = [float(initial_cash)]
        equity_curve = [
            {"timestamp": str(data.index[-1]), "equity": round(initial_cash, 2)}
        ]
    peak = float(initial_cash)
    maximum_drawdown = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak > 0:
            maximum_drawdown = max(maximum_drawdown, (peak - value) / peak)

    profits = [trade["net_pnl"] for trade in completed_trades if trade["net_pnl"] > 0]
    losses = [trade["net_pnl"] for trade in completed_trades if trade["net_pnl"] < 0]
    gross_profit = sum(profits)
    gross_loss = abs(sum(losses))
    final_equity = cash
    return {
        "status": "COMPLETED",
        "trades": completed_trades,
        "trade_count": len(completed_trades),
        "win_rate": (
            len(profits) / len(completed_trades)
            if completed_trades
            else 0.0
        ),
        "average_profit": sum(profits) / len(profits) if profits else 0.0,
        "average_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else None
        ),
        "maximum_drawdown": maximum_drawdown,
        "estimated_costs": total_costs,
        "net_return_pct": (final_equity / initial_cash - 1) * 100,
        "initial_equity": float(initial_cash),
        "final_equity": final_equity,
        "equity_curve": equity_curve,
        "execution_assumption": "next-bar open; adverse slippage and commission",
    }


def walk_forward_backtest(history, folds=3, **backtest_options):
    """Evaluate sequential out-of-sample windows with prior bars as context."""
    data = _validate_history(history)
    if isinstance(folds, bool) or not isinstance(folds, int) or folds <= 0:
        raise ValueError("folds must be a positive integer.")
    available_bars = len(data) - 20
    if available_bars < folds:
        raise ValueError("Not enough bars for the requested walk-forward folds.")

    test_window = available_bars // folds
    results = []
    for fold_number in range(folds):
        start = 20 + fold_number * test_window
        end = (
            len(data)
            if fold_number == folds - 1
            else min(len(data), start + test_window)
        )
        if start >= end:
            continue
        fold_history = data.iloc[:end]
        result = run_backtest(
            fold_history,
            start_index=start,
            **backtest_options,
        )
        results.append(
            {
                "fold": fold_number + 1,
                "out_of_sample_start": str(data.index[start]),
                "out_of_sample_end": str(data.index[end - 1]),
                "performance": result,
            }
        )
    return {
        "status": "COMPLETED",
        "fold_count": len(results),
        "folds": results,
        "validation": "sequential out-of-sample; no model training or deployment",
    }
