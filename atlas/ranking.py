"""Order scan cards with the fields the pipeline already produced.

Risk-sizable BUY and SELL names come first, then other BUY names, then WAIT,
then other SELL names, then anything without a decision. Confidence is the
existing strategy confidence. Nothing here is an expected return.
"""


_TIER = {"BUY": 1, "WAIT": 2, "SELL": 3}


def rank_scans(cards):
    return sorted(cards, key=_rank_key)


def top_scans(cards, limit=3):
    ranked = rank_scans(cards)
    return ranked[:limit]


def _rank_key(card):
    decision = str(card.get("decision") or "").upper()
    if (card.get("buyable") and decision == "BUY") or (
        card.get("shortable") and decision == "SELL"
    ):
        tier = 0
    else:
        tier = _TIER.get(decision, 4)
    confidence = card.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence_rank = (1, 0.0)
    else:
        confidence_rank = (0, -float(confidence))
    decision_name = str(card.get("decision") or "").upper()
    bias = card.get("learning_bias")
    if (
        decision_name not in {"BUY", "SELL"}
        or isinstance(bias, bool)
        or not isinstance(bias, int)
    ):
        bias_rank = 0
    else:
        bias_rank = -bias
    return (tier, confidence_rank, bias_rank, str(card.get("symbol") or ""))


def card_from_pipeline(result, equity, exposure):
    """A display card from one pipeline result. Sizing uses the current account."""
    from atlas.risk import calculate_position_size

    analysis = result.get("analysis") or {}
    recommendation = result.get("recommendation") or {}
    decision = result.get("decision") or result.get("status") or "NO_DATA"
    entry = stop = target = quantity = None
    if decision in {"BUY", "SELL"} and recommendation:
        entry = recommendation.get("entry_price")
        stop = recommendation.get("stop_loss")
        target = recommendation.get("profit_target")
        if isinstance(entry, (int, float)) and isinstance(stop, (int, float)) and not isinstance(entry, bool):
            quantity = calculate_position_size(
                equity,
                entry,
                stop,
                current_exposure=exposure,
            )
    adaptive = result.get("adaptive") or {}
    prediction = adaptive.get("prediction") or {}
    news = result.get("news") or {}
    return {
        "symbol": result.get("symbol") or "",
        "decision": decision,
        "reason": analysis.get("reason") or result.get("error") or "",
        "price": analysis.get("price"),
        "entry": entry,
        "stop": stop,
        "target": target,
        "quantity": quantity,
        "confidence": _confidence(result),
        "reasons": [analysis.get("reason")] if analysis.get("reason") else [],
        "buyable": decision == "BUY" and isinstance(quantity, int) and quantity > 0,
        "shortable": decision == "SELL" and isinstance(quantity, int) and quantity > 0,
        "agreement": analysis.get("agreement"),
        "scanned_at": result.get("scanned_at"),
        "timeframes": {},
        "news": {
            "status": news.get("status"),
            "headlines": list(news.get("headlines") or []),
            "items": list(news.get("items") or []),
            "source": news.get("source"),
        },
        "model": {
            "status": adaptive.get("status"),
            "reason": adaptive.get("reason"),
            "version": adaptive.get("version"),
            "probability_up": prediction.get("probability_up"),
            "signal": prediction.get("signal"),
        },
    }


def coverage_note(universe_size, scanned_count, buy_count, batch_count):
    """Plain status. It does not promise profit."""
    not_yet = max(0, int(universe_size) - int(scanned_count))
    if not_yet:
        progress = (
            f"Scanned {scanned_count} of {universe_size} S&P 500 names. "
            f"{not_yet} have not been scanned yet. "
        )
    else:
        progress = (
            f"All {universe_size} S&P 500 names have been scanned at least once. "
        )
    progress += f"This pass scored {batch_count} names. "
    if buy_count >= 3:
        detail = (
            "The cards are the top risk-sizable buys from the stored scans. "
            "This is not a forecast of profit."
        )
    elif buy_count == 0:
        detail = (
            "None of the stored scans is a risk-sizable buy, so the cards are "
            "the next scan results. Buy stays off. This is not a forecast of profit."
        )
    else:
        detail = (
            f"{buy_count} stored scan is a risk-sizable buy. "
            "The other cards are the next scan results, not extra buys. "
            "This is not a forecast of profit."
        )
    return progress + detail


def _confidence(result):
    scores = []
    decision = result.get("decision")
    for frame in ((result.get("analysis") or {}).get("timeframes") or {}).values():
        strategy = (frame or {}).get("strategy") or {}
        value = strategy.get("confidence")
        if strategy.get("signal") != decision:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        scores.append(float(value))
    return max(scores) if scores else None
