import pandas as pd

from atlas.pipeline import run_adaptive_pipeline, run_multi_timeframe_pipeline


def history(start=100, step=1, rows=40):
    close = [start + index * step for index in range(rows)]
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Volume": [1_000_000] * rows,
        }
    )


def test_multi_timeframe_pipeline_requires_independent_agreement():
    histories = {
        "5m": history(),
        "1h": history(),
        "1d": history(start=140, step=-1),
    }

    result = run_multi_timeframe_pipeline(histories, minimum_agreement=2)

    assert result["ready_timeframes"] == 3
    assert result["agreement"] == {"BUY": 2, "SELL": 1}
    assert result["signal"] == "BUY"


def test_adaptive_pipeline_requires_active_model_and_strategy_agreement():
    model = {
        "coefficients": [5, 0, 0, 0, 0, 0, 0, 0],
        "mean": [0] * 7,
        "scale": [1] * 7,
        "threshold": 0.55,
    }

    class ActiveModelDatabase:
        def active_model(self):
            return {"version": "approved-v1", "model": model}

    result = run_adaptive_pipeline(
        history(),
        ActiveModelDatabase(),
        "XYZ",
    )

    assert result["adaptive"]["status"] == "ACTIVE"
    assert result["strategy"]["signal"] == "BUY"
    assert result["recommendation"]["instrument"] == "XYZ"
