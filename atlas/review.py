"""After-the-fact review of paper fills and stored scan decisions.

Numbers come from the ledger and from bars that occur after the fill or the
scan. A missing later bar stays missing. A stored decision is not rewritten.
"""

from datetime import datetime, timezone
import json
from math import isfinite


_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "1d", "1w")
_DECISIONS = {"BUY", "SELL", "WAIT"}


def build_review(orders, observations, bars_by_symbol):
    """Review stored fills and scan decisions against later closes.

    ``bars_by_symbol`` maps a symbol to ``{timeframe: dataframe or list}``.
    A list item is ``{"time": iso, "close": number}``.
    """
    fills = [_review_fill(order, orders, bars_by_symbol) for order in orders or []]
    decisions = [
        _review_decision(observation, bars_by_symbol)
        for observation in observations or []
        if str(observation.get("decision") or "").upper() in _DECISIONS
    ]
    counted = [row for row in fills if row["counts"]]
    if not fills:
        note = "No paper fills to review yet."
        board = None
    elif not counted:
        note = "Not enough later bars yet."
        board = None
    else:
        note = "Counted from ledger sells and from buys that have a later bar."
        board = _scoreboard(counted)
    return {
        "fills": fills,
        "decisions": decisions,
        "scoreboard": board,
        "scoreboard_note": note,
        "model": _review_models(decisions),
    }


def latest_bar(bars):
    chosen = None
    for bar in bars or []:
        when = _parse_time(bar.get("time"))
        if when is None:
            continue
        if chosen is None or when > chosen[0]:
            chosen = (when, bar)
    return chosen[1] if chosen else None


def choose_bars(histories):
    """Prefer the shortest timeframe that actually returned closes."""
    if not isinstance(histories, dict):
        return None, []
    names = [name for name in _TIMEFRAMES if name in histories]
    names.extend(name for name in histories if name not in names)
    for name in names:
        bars = _closes(histories.get(name))
        if bars:
            return name, bars
    return None, []


def _review_fill(order, orders, bars_by_symbol):
    symbol = str(order.get("symbol") or "").strip().upper()
    side = str(order.get("side") or "").strip().upper()
    price = _number(order.get("price"))
    quantity = order.get("quantity")
    filled_at = order.get("timestamp")
    timeframe, bars = choose_bars((bars_by_symbol or {}).get(symbol))
    later = _first_after(bars, filled_at)
    later_sell = _has_later_sell(orders, order)
    row = {
        "order_id": order.get("order_id"),
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "filled_at": filled_at,
        "timeframe": timeframe,
        "next_bar_time": later["time"] if later else None,
        "next_close": later["close"] if later else None,
        "result": None,
        "result_kind": None,
        "counts": False,
        "note": "Not enough later bars yet.",
    }
    effect = str(order.get("effect") or "").upper()
    row["effect"] = effect or None
    if effect == "OPEN_SHORT":
        if later is None or price is None or not isinstance(quantity, int) or isinstance(quantity, bool):
            row["note"] = "This opened a paper short. Not enough later bars yet."
            return row
        row["result"] = (price - later["close"]) * quantity
        row["result_kind"] = "next_bar"
        change = _return(price, later["close"])
        row["next_return"] = None if change is None else -change
        row["counts"] = True
        row["note"] = "This opened a paper short. Marked to the next bar. This is not a closed-trade profit."
        return row
    if side == "SELL" or effect == "CLOSE_SHORT":
        realized = _number(order.get("realized_pnl"))
        row["result"] = realized
        row["result_kind"] = "realized"
        row["counts"] = realized is not None
        row["note"] = (
            "Realized P&L from covering this paper short."
            if effect == "CLOSE_SHORT"
            else "Realized P&L from the ledger for this sell."
        )
        if later is None:
            row["note"] += " No bar after this fill is available yet."
        return row
    if side != "BUY" or price is None or not isinstance(quantity, int) or isinstance(quantity, bool):
        row["note"] = "This fill does not have a price and quantity that can be reviewed."
        return row
    if later_sell:
        row["note"] = (
            "This buy was later sold. The realized result is on that sell."
        )
        if later is not None:
            row["next_return"] = _return(price, later["close"])
        return row
    if later is None:
        return row
    row["result"] = (later["close"] - price) * quantity
    row["result_kind"] = "next_bar"
    row["next_return"] = _return(price, later["close"])
    row["counts"] = True
    row["note"] = "Marked to the next bar after the buy. This is not a closed-trade profit."
    return row


def _review_decision(observation, bars_by_symbol):
    symbol = str(observation.get("symbol") or "").strip().upper()
    decision = str(observation.get("decision") or "").strip().upper()
    price = _number(observation.get("price"))
    bar_time = observation.get("bar_time")
    timeframe, bars = choose_bars((bars_by_symbol or {}).get(symbol))
    if observation.get("timeframe"):
        named = _closes(((bars_by_symbol or {}).get(symbol) or {}).get(observation["timeframe"]))
        if named:
            timeframe = observation["timeframe"]
            bars = named
    later = _first_after(bars, bar_time)
    row = {
        "symbol": symbol,
        "decision": decision,
        "price": price,
        "bar_time": bar_time,
        "timeframe": timeframe,
        "next_bar_time": later["time"] if later else None,
        "next_close": later["close"] if later else None,
        "next_return": None,
        "model_status": observation.get("model_status"),
        "model_signal": observation.get("model_signal"),
        "model_version": observation.get("model_version"),
        "model_matched": None,
        "note": "Not enough later bars yet.",
    }
    if later is None or price is None or price <= 0:
        if decision == "WAIT":
            row["note"] = "This stayed a wait. Not enough later bars yet."
        return row
    row["next_return"] = _return(price, later["close"])
    if decision == "WAIT":
        row["note"] = "This stayed a wait. The next bar is what the market did after the scan."
    elif decision == "BUY":
        row["note"] = "The scan was a buy. The next bar is what the market did after the scan."
    elif decision == "SELL":
        row["note"] = "The scan was a sell. The next bar is what the market did after the scan."
    row["model_matched"] = _model_match(
        observation.get("model_status"),
        observation.get("model_signal"),
        price,
        later["close"],
    )
    return row


def _review_models(decisions):
    if not decisions:
        return {
            "note": "No stored scan decisions to review yet.",
            "graded": 0,
            "matched": 0,
        }
    graded = 0
    matched = 0
    saw_status = False
    for row in decisions:
        status = row.get("model_status")
        if status:
            saw_status = True
        if status != "ACTIVE":
            continue
        if row.get("model_matched") is None:
            continue
        graded += 1
        if row["model_matched"]:
            matched += 1
    if graded == 0 and saw_status:
        note = "A stored model was not used for the decision."
    elif graded == 0:
        note = "No directional model call has a later bar yet."
    else:
        note = "Active-model calls are graded only against a later close."
    return {"note": note, "graded": graded, "matched": matched}


def _model_match(status, signal, price, close):
    if status != "ACTIVE":
        return None
    if signal not in {"BUY", "SELL"}:
        return None
    if close == price:
        return False
    if signal == "BUY":
        return close > price
    return close < price


def _scoreboard(rows):
    results = [row["result"] for row in rows if _number(row.get("result")) is not None]
    gains = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    hits = len(gains)
    count = len(results)
    return {
        "reviewed_count": count,
        "hit_count": hits,
        "hit_rate": hits / count if count else None,
        "average_gain": sum(gains) / len(gains) if gains else None,
        "average_loss": sum(losses) / len(losses) if losses else None,
    }


def _has_later_sell(orders, buy):
    if str(buy.get("side") or "").upper() != "BUY":
        return False
    symbol = str(buy.get("symbol") or "").strip().upper()
    buy_time = _parse_time(buy.get("timestamp"))
    for order in orders or []:
        if str(order.get("symbol") or "").strip().upper() != symbol:
            continue
        if str(order.get("side") or "").upper() != "SELL":
            continue
        if order.get("order_id") == buy.get("order_id"):
            continue
        sell_time = _parse_time(order.get("timestamp"))
        if buy_time is None or sell_time is None or sell_time > buy_time:
            return True
    return False


def _closes(frame):
    if frame is None:
        return []
    if isinstance(frame, list):
        bars = []
        for item in frame:
            if not isinstance(item, dict):
                continue
            close = _number(item.get("close"))
            if close is None or not item.get("time"):
                continue
            bars.append({"time": str(item["time"]), "close": close})
        return bars
    if getattr(frame, "empty", True) or not hasattr(frame, "iterrows"):
        return []
    bars = []
    for stamp, row in frame.iterrows():
        close = _number(row["Close"]) if "Close" in getattr(row, "index", ()) else None
        if close is None:
            continue
        when = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)
        bars.append({"time": when, "close": close})
    return bars


def _first_after(bars, after):
    cutoff = _parse_time(after)
    if cutoff is None:
        return None
    chosen = None
    for bar in bars or []:
        when = _parse_time(bar["time"])
        if when is None or when <= cutoff:
            continue
        if chosen is None or when < _parse_time(chosen["time"]):
            chosen = bar
    return chosen


def _return(price, close):
    if price is None or price <= 0 or close is None:
        return None
    return (close / price) - 1


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return number


def _parse_time(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        current = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            current = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def review_payload(review):
    """JSON-ready copy. Datetimes are already strings."""
    return json.loads(json.dumps(review, sort_keys=True))
