import pandas as pd

from atlas import backtesting


def make_history(rows=23):
    opens = [100.0] * rows
    closes = [100.0] * rows
    highs = [100.0] * rows
    lows = [100.0] * rows
    opens[21] = 100.0
    closes[21] = 105.0
    highs[21] = 105.0
    opens[22] = 110.0
    closes[22] = 110.0
    highs[22] = 110.0
    return pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": [1_000] * rows,
        }
    )


def test_backtest_fills_next_bar_and_includes_trading_costs(monkeypatch):
    def fake_evaluate(history):
        return {
            "status": "READY",
            "signal": "BUY" if len(history) == 21 else "SELL",
        }

    monkeypatch.setattr(backtesting, "evaluate_market", fake_evaluate)
    result = backtesting.run_backtest(
        make_history(),
        initial_cash=10_000,
        position_fraction=0.5,
        commission_bps=10,
        slippage_bps=20,
    )

    assert result["trade_count"] == 1
    assert result["trades"][0]["entry_price"] == 100.2
    assert result["trades"][0]["exit_price"] == 109.78
    assert result["estimated_costs"] > 0
    assert result["trades"][0]["net_pnl"] > 0
    assert result["execution_assumption"].startswith("next-bar open")


def test_walk_forward_uses_multiple_out_of_sample_folds(monkeypatch):
    monkeypatch.setattr(
        backtesting,
        "evaluate_market",
        lambda history: {"status": "READY", "signal": "WAIT"},
    )
    result = backtesting.walk_forward_backtest(
        make_history(80),
        folds=3,
    )

    assert result["status"] == "COMPLETED"
    assert result["fold_count"] == 3
    assert all(
        fold["performance"]["trade_count"] == 0
        for fold in result["folds"]
    )
    assert "out-of-sample" in result["validation"]
