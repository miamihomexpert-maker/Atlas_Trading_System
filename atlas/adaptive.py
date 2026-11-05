from datetime import datetime, timezone
import uuid

import numpy as np

from atlas.market_data import normalize_ohlcv


_FEATURE_COUNT = 7
_MIN_HISTORY = 22


def _is_after_timestamp(candidate, cutoff):
    if candidate == cutoff:
        return False
    try:
        candidate_time = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        cutoff_time = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if candidate_time.tzinfo is None:
            candidate_time = candidate_time.replace(tzinfo=timezone.utc)
        if cutoff_time.tzinfo is None:
            cutoff_time = cutoff_time.replace(tzinfo=timezone.utc)
        return candidate_time > cutoff_time
    except ValueError:
        try:
            return float(candidate) > float(cutoff)
        except ValueError:
            return False


def _feature_dataset(history):
    data = normalize_ohlcv(history)
    if len(data) < _MIN_HISTORY:
        raise ValueError(f"At least {_MIN_HISTORY} bars are required for model training.")

    close = data["Close"].to_numpy(dtype=float)
    volume = data["Volume"].to_numpy(dtype=float)
    high = data["High"].to_numpy(dtype=float)
    low = data["Low"].to_numpy(dtype=float)
    features = []
    labels = []
    forward_returns = []
    timestamps = []
    for index in range(20, len(data) - 1):
        prior_returns = close[1:index + 1] / close[:index] - 1
        trailing_close = close[index - 19:index + 1]
        trailing_volume = volume[index - 19:index + 1]
        average_volume = float(trailing_volume.mean())
        features.append(
            [
                close[index] / close[index - 1] - 1,
                close[index] / close[index - 5] - 1,
                close[index] / close[index - 20] - 1,
                float(np.std(prior_returns[-20:])),
                close[index] / float(trailing_close.mean()) - 1,
                (
                    volume[index] / average_volume - 1
                    if average_volume > 0
                    else 0.0
                ),
                (high[index] - low[index]) / close[index],
            ]
        )
        forward_return = close[index + 1] / close[index] - 1
        labels.append(1.0 if forward_return > 0 else 0.0)
        forward_returns.append(forward_return)
        timestamps.append(str(data.index[index + 1]))

    return (
        np.asarray(features, dtype=float),
        np.asarray(labels, dtype=float),
        np.asarray(forward_returns, dtype=float),
        timestamps,
    )


def _fit_logistic(features, labels, epochs=600, learning_rate=0.1, l2=0.01):
    mean = features.mean(axis=0)
    scale = features.std(axis=0)
    scale[scale < 1e-8] = 1.0
    normalized = (features - mean) / scale
    design = np.column_stack((np.ones(len(normalized)), normalized))
    coefficients = np.zeros(design.shape[1], dtype=float)
    for _ in range(epochs):
        logits = np.clip(design @ coefficients, -30, 30)
        probabilities = 1 / (1 + np.exp(-logits))
        gradient = design.T @ (probabilities - labels) / len(labels)
        gradient[1:] += l2 * coefficients[1:]
        coefficients -= learning_rate * gradient
    return {
        "coefficients": coefficients.tolist(),
        "mean": mean.tolist(),
        "scale": scale.tolist(),
        "feature_count": _FEATURE_COUNT,
        "threshold": 0.55,
    }


def predict_probability(model, features):
    coefficients = np.asarray(model["coefficients"], dtype=float)
    mean = np.asarray(model["mean"], dtype=float)
    scale = np.asarray(model["scale"], dtype=float)
    row = np.asarray(features, dtype=float)
    if row.shape != (_FEATURE_COUNT,) or coefficients.shape != (_FEATURE_COUNT + 1,):
        raise ValueError("Model or feature vector has an incompatible shape.")
    if not np.isfinite(row).all() or not np.isfinite(coefficients).all():
        raise ValueError("Model and features must contain finite values.")
    normalized = (row - mean) / scale
    logit = float(np.clip(coefficients[0] + normalized @ coefficients[1:], -30, 30))
    return 1 / (1 + np.exp(-logit))


def _evaluate_predictions(labels, forward_returns, probabilities, commission_bps):
    if not len(labels):
        raise ValueError("Out-of-sample evaluation has no observations.")
    accuracy = float(np.mean((probabilities >= 0.5) == labels))
    brier_score = float(np.mean((probabilities - labels) ** 2))
    threshold = 0.55
    active = probabilities >= threshold
    net_returns = []
    previous_active = False
    commission = commission_bps / 10_000
    for is_active, forward_return in zip(active, forward_returns):
        turnover = int(is_active != previous_active)
        net_returns.append(
            (float(forward_return) if is_active else 0.0)
            - turnover * commission
        )
        previous_active = bool(is_active)
    if previous_active:
        net_returns[-1] -= commission

    equity = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    for daily_return in net_returns:
        equity *= max(0.0, 1 + daily_return)
        peak = max(peak, equity)
        if peak > 0:
            maximum_drawdown = max(
                maximum_drawdown,
                (peak - equity) / peak,
            )
    return {
        "sample_count": len(labels),
        "accuracy": accuracy,
        "brier_score": brier_score,
        "net_return_pct": (equity - 1) * 100,
        "maximum_drawdown": maximum_drawdown,
        "threshold": threshold,
    }


def train_candidate(
    history,
    database=None,
    folds=3,
    minimum_train_samples=40,
    commission_bps=5.0,
):
    """Train a logistic classifier and evaluate it only on later observations.

    Feature scaling is fit independently from each fold's training period.
    The returned full-history fit remains a candidate; promotion is a separate,
    risk-gated operation on ``AtlasDatabase``.
    """
    if isinstance(folds, bool) or not isinstance(folds, int) or folds < 2:
        raise ValueError("At least two walk-forward folds are required.")
    if (
        isinstance(minimum_train_samples, bool)
        or not isinstance(minimum_train_samples, int)
        or minimum_train_samples < 10
    ):
        raise ValueError("minimum_train_samples must be at least 10.")
    if not np.isfinite(commission_bps) or commission_bps < 0:
        raise ValueError("commission_bps must be finite and non-negative.")

    features, labels, returns, timestamps = _feature_dataset(history)
    active = database.active_model() if database is not None else None
    active_model = active["model"] if active is not None else None
    active_trained_through = (
        active_model.get("trained_through")
        if active_model is not None
        else None
    )
    first_test = max(minimum_train_samples, len(features) // 2)
    remaining = len(features) - first_test
    if remaining < folds:
        raise ValueError("Not enough historical observations for walk-forward testing.")
    fold_size = remaining // folds
    all_labels = []
    all_returns = []
    all_probabilities = []
    fold_reports = []
    comparison_labels = []
    comparison_returns = []
    candidate_comparison_probabilities = []
    incumbent_comparison_probabilities = []
    for fold in range(folds):
        test_start = first_test + fold * fold_size
        test_end = len(features) if fold == folds - 1 else test_start + fold_size
        if test_end <= test_start:
            continue
        fold_model = _fit_logistic(features[:test_start], labels[:test_start])
        fold_probabilities = np.asarray(
            [
                predict_probability(fold_model, row)
                for row in features[test_start:test_end]
            ],
            dtype=float,
        )
        fold_labels = labels[test_start:test_end]
        fold_returns = returns[test_start:test_end]
        if active_model is not None and active_trained_through is not None:
            eligible = [
                offset
                for offset, timestamp in enumerate(
                    timestamps[test_start:test_end]
                )
                if _is_after_timestamp(timestamp, active_trained_through)
            ]
            for offset in eligible:
                index = test_start + offset
                comparison_labels.append(float(labels[index]))
                comparison_returns.append(float(returns[index]))
                candidate_comparison_probabilities.append(
                    float(fold_probabilities[offset])
                )
                incumbent_comparison_probabilities.append(
                    float(predict_probability(active_model, features[index]))
                )
        report = _evaluate_predictions(
            fold_labels,
            fold_returns,
            fold_probabilities,
            commission_bps,
        )
        fold_reports.append(
            {
                "fold": fold + 1,
                "training_samples": test_start,
                "out_of_sample_start": timestamps[test_start],
                "out_of_sample_end": timestamps[test_end - 1],
                **report,
            }
        )
        all_labels.extend(fold_labels.tolist())
        all_returns.extend(fold_returns.tolist())
        all_probabilities.extend(fold_probabilities.tolist())

    metrics = _evaluate_predictions(
        np.asarray(all_labels, dtype=float),
        np.asarray(all_returns, dtype=float),
        np.asarray(all_probabilities, dtype=float),
        commission_bps,
    )
    model = _fit_logistic(features, labels)
    model["trained_through"] = timestamps[-1]
    if active is not None:
        if comparison_labels:
            candidate_comparison = _evaluate_predictions(
                np.asarray(comparison_labels, dtype=float),
                np.asarray(comparison_returns, dtype=float),
                np.asarray(candidate_comparison_probabilities, dtype=float),
                commission_bps,
            )
            incumbent_comparison = _evaluate_predictions(
                np.asarray(comparison_labels, dtype=float),
                np.asarray(comparison_returns, dtype=float),
                np.asarray(incumbent_comparison_probabilities, dtype=float),
                commission_bps,
            )
        else:
            candidate_comparison = {
                "sample_count": 0,
                "accuracy": 0.0,
                "brier_score": 1.0,
                "net_return_pct": 0.0,
                "maximum_drawdown": 1.0,
            }
            incumbent_comparison = dict(candidate_comparison)
        metrics["candidate_comparison"] = candidate_comparison
        metrics["incumbent_comparison"] = {
            "version": active["version"],
            **incumbent_comparison,
        }
    version = f"atlas-logistic-{uuid.uuid4().hex[:12]}"
    candidate = {
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "candidate",
        "model_type": "logistic_regression",
        "metrics": metrics,
        "walk_forward": fold_reports,
        "model": model,
    }
    if database is not None:
        database.save_model_candidate(version, metrics, model)
    return candidate


def latest_model_probability(history, model):
    data = normalize_ohlcv(history)
    if len(data) < _MIN_HISTORY:
        raise ValueError(f"At least {_MIN_HISTORY} bars are required for prediction.")
    close = data["Close"].to_numpy(dtype=float)
    volume = data["Volume"].to_numpy(dtype=float)
    high = data["High"].to_numpy(dtype=float)
    low = data["Low"].to_numpy(dtype=float)
    index = len(data) - 1
    prior_returns = close[1:index + 1] / close[:index] - 1
    trailing_close = close[index - 19:index + 1]
    trailing_volume = volume[index - 19:index + 1]
    average_volume = float(trailing_volume.mean())
    features = np.asarray(
        [
            close[index] / close[index - 1] - 1,
            close[index] / close[index - 5] - 1,
            close[index] / close[index - 20] - 1,
            float(np.std(prior_returns[-20:])),
            close[index] / float(trailing_close.mean()) - 1,
            (
                volume[index] / average_volume - 1
                if average_volume > 0
                else 0.0
            ),
            (high[index] - low[index]) / close[index],
        ],
        dtype=float,
    )
    probability = float(predict_probability(model, features))
    threshold = float(model.get("threshold", 0.55))
    if not np.isfinite(threshold):
        raise ValueError("Model threshold must be finite.")
    sell_below = 1 - threshold
    if threshold > 0.5 and sell_below < threshold:
        if probability >= threshold:
            signal = "BUY"
        elif probability <= sell_below:
            signal = "SELL"
        else:
            signal = "WAIT"
    else:
        signal = "BUY" if probability >= threshold else "WAIT"
    return {
        "probability_up": probability,
        "signal": signal,
        "timestamp": str(data.index[-1]),
    }


def get_adaptive_status():
    return (
        "LOCAL LOGISTIC MODEL AVAILABLE; WALK-FORWARD CANDIDATES REQUIRE "
        "OUT-OF-SAMPLE PROMOTION GATES"
    )
