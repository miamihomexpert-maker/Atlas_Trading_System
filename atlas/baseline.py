"""Fit the existing logistic model on daily bars and promote it only if the
walk-forward gates already in ``AtlasDatabase.promote_model`` pass.

A failed gate leaves the candidate stored and unscored for trading. The
dashboard can show the computed probability, and the decision stays on the
technical scan.
"""

from datetime import datetime, timezone
import json
import os

from atlas.adaptive import latest_model_probability, train_candidate


def auto_train_enabled():
    return os.getenv("ATLAS_AUTO_TRAIN", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _default_history(symbol):
    from atlas.market_data import get_stock_history

    return get_stock_history(symbol, period="2y", interval="1d")


def ensure_baseline_model(database, symbols, history_loader=None):
    """Train one daily model per symbol until one passes promotion, or record why not."""
    if database is None:
        return {"status": "NO_DATABASE", "promoted": False, "reason": "No database."}
    active = database.active_model()
    if active is not None:
        return {
            "status": "ACTIVE",
            "promoted": True,
            "version": active["version"],
            "reason": "An active model is already stored.",
        }
    loader = history_loader or _default_history
    reports = []
    promoted_version = None
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        if not normalized:
            continue
        history = loader(normalized)
        if history is None or getattr(history, "empty", True) or len(history) < 22:
            reports.append({
                "symbol": normalized,
                "status": "INSUFFICIENT_HISTORY",
                "reason": "Fewer than 22 daily bars were returned.",
            })
            continue
        try:
            candidate = train_candidate(history, database=database)
        except ValueError as error:
            reports.append({
                "symbol": normalized,
                "status": "INSUFFICIENT_HISTORY",
                "reason": str(error),
            })
            continue
        promotion = database.promote_model(candidate["version"])
        try:
            prediction = latest_model_probability(history, candidate["model"])
        except ValueError as error:
            prediction = None
            prediction_error = str(error)
        else:
            prediction_error = None
        reports.append({
            "symbol": normalized,
            "status": "ACTIVE" if promotion.get("promoted") else "CANDIDATE_NOT_PROMOTED",
            "version": candidate["version"],
            "promoted": bool(promotion.get("promoted")),
            "reason": promotion.get("reason"),
            "probability_up": None if prediction is None else prediction.get("probability_up"),
            "signal": None if prediction is None else prediction.get("signal"),
            "prediction_error": prediction_error,
        })
        database.set_metadata("baseline_candidate", candidate["version"])
        database.set_metadata("baseline_reason", promotion.get("reason") or "")
        if promotion.get("promoted"):
            promoted_version = candidate["version"]
            break
    if promoted_version:
        summary = {
            "status": "ACTIVE",
            "promoted": True,
            "version": promoted_version,
            "reason": "Candidate passed out-of-sample performance and risk gates.",
            "reports": reports,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        }
    elif any(item["status"] == "CANDIDATE_NOT_PROMOTED" for item in reports):
        summary = {
            "status": "CANDIDATE_NOT_PROMOTED",
            "promoted": False,
            "version": reports[-1].get("version"),
            "reason": reports[-1].get("reason"),
            "reports": reports,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        summary = {
            "status": "INSUFFICIENT_HISTORY",
            "promoted": False,
            "version": None,
            "reason": "No symbol had enough daily history to train.",
            "reports": reports,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        }
    database.set_metadata("baseline_report", json.dumps(summary, sort_keys=True))
    return summary
