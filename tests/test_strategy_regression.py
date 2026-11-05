import pandas as pd

from atlas.strategy import evaluate_market
from atlas.pipeline import run_pipeline 
from atlas.pipeline import run_pipeline

def make_history(closes, volumes=None):
    if volumes is None:
        volumes = [1_000_000] * len(closes)

    return pd.DataFrame(
    {
        "Open": closes,
        "High": closes,
        "Low": closes,
        "Close": closes,
        "Volume": volumes,
    }
)


def test_evaluate_market_returns_expected_keys():
    history = make_history([100 + i for i in range(40)])

    result = evaluate_market(history)

    assert isinstance(result, dict)
    assert "status" in result
    assert "signal" in result
    assert "confidence" in result
def test_evaluate_market_bullish_history():
        history = make_history([100 + i for i in range(40)])

        result = evaluate_market(history)

        assert result["status"] == "READY"
        assert result["signal"] in {"BUY", "WAIT"}
        assert isinstance(result["confidence"], (int, float))


def test_evaluate_market_bearish_history():
    history = make_history([140 - i for i in range(40)])

    result = evaluate_market(history)

    
    
    assert result["status"] == "READY"
    assert result["signal"] in {"SELL", "WAIT"}
    assert isinstance(result["confidence"], (int, float))


def test_evaluate_market_missing_data():
    history = pd.DataFrame()

    result = evaluate_market(history)
   

    assert result["status"] == "NO_DATA"
    assert result["signal"] == "WAIT"
    assert result["confidence"] is None


def test_run_pipeline_missing_data():
    history = pd.DataFrame()

    result = run_pipeline(history)

    assert result["strategy"]["status"] == "NO_DATA"
    assert result["strategy"]["signal"] == "WAIT"
    assert result["strategy"]["confidence"] is None
    assert result["technical"] is None
def test_run_pipeline_with_market_data():
    history = make_history([100 + i for i in range(40)])

    result = run_pipeline(history)

    assert result["strategy"]["status"] == "READY"
    assert result["technical"] is not None
    assert "sma" in result["technical"]
    assert "ema" in result["technical"]
    assert "rsi" in result["technical"]
    assert "macd" in result["technical"]
