import numpy as np
import pandas as pd

from atlas.adaptive import latest_model_probability, train_candidate
from atlas.database import AtlasDatabase


def synthetic_history(rows=240):
    rng = np.random.default_rng(42)
    returns = rng.normal(0.0005, 0.01, rows)
    close = 100 * np.cumprod(1 + returns)
    return pd.DataFrame(
        {
            "Open": close * (1 - 0.001),
            "High": close * 1.005,
            "Low": close * 0.995,
            "Close": close,
            "Volume": rng.integers(100_000, 500_000, rows),
        },
        index=pd.date_range("2024-01-01", periods=rows, freq="D"),
    )


def test_candidate_trains_and_reports_sequential_out_of_sample_metrics(tmp_path):
    database = AtlasDatabase(tmp_path / "models.sqlite3")
    candidate = train_candidate(
        synthetic_history(),
        database=database,
        folds=3,
        minimum_train_samples=40,
    )
    saved = database.get_model_version(candidate["version"])
    prediction = latest_model_probability(
        synthetic_history(),
        candidate["model"],
    )

    assert candidate["status"] == "candidate"
    assert len(candidate["walk_forward"]) == 3
    assert candidate["metrics"]["sample_count"] > 0
    assert 0 <= candidate["metrics"]["accuracy"] <= 1
    assert 0 <= candidate["metrics"]["maximum_drawdown"] <= 1
    assert saved["status"] == "candidate"
    assert 0 <= prediction["probability_up"] <= 1
    assert prediction["signal"] in {"BUY", "WAIT"}
    database.close()


def test_model_training_rejects_insufficient_history():
    history = synthetic_history(rows=45)

    try:
        train_candidate(history, minimum_train_samples=40)
    except ValueError as error:
        assert "walk-forward" in str(error)
    else:
        raise AssertionError("Training must reject a dataset without valid test folds.")


def test_model_update_is_not_promoted_without_new_incumbent_holdout(tmp_path):
    history = synthetic_history()
    database = AtlasDatabase(tmp_path / "promotion.sqlite3")
    trained_through = str(history.index[-1])
    model = {
        "coefficients": [0] * 8,
        "mean": [0] * 7,
        "scale": [1] * 7,
        "threshold": 0.55,
        "trained_through": trained_through,
    }
    initial_metrics = {
        "sample_count": 100,
        "accuracy": 0.6,
        "net_return_pct": 5,
        "maximum_drawdown": 0.1,
    }
    database.save_model_candidate("incumbent", initial_metrics, model)
    assert database.promote_model("incumbent")["promoted"]

    candidate = train_candidate(
        history,
        database=database,
        folds=3,
        minimum_train_samples=40,
    )

    assert candidate["metrics"]["candidate_comparison"]["sample_count"] == 0
    result = database.promote_model(candidate["version"])
    assert not result["promoted"]
    assert database.active_model()["version"] == "incumbent"
    database.close()
