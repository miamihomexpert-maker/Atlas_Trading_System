"""Rows for the paper dashboard Today's trades tab.

Every price comes from the ledger or from an open position the ledger
rebuilt. A missing figure stays missing. This does not submit an order.
"""

from datetime import datetime, timezone

from atlas.market_hours import as_new_york


EMPTY_DAY_NOTE = "No paper positions or same-day trades."
DAY_TRADE_NOTE = (
    "Each symbol is listed once. Sell and cover still ask you to confirm. "
    "A closed row has no sell button."
)


def build_day_trades(positions, orders, now=None):
    """One row per open position, plus today's closes that are no longer open."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone.")
    today = as_new_york(current).date()
    positions = positions or {}
    orders = list(orders or [])
    rows = []
    open_symbols = set()
    for symbol in sorted(positions):
        position = positions[symbol] or {}
        quantity = position.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            continue
        side = "SHORT" if position.get("side") == "SHORT" else "LONG"
        open_symbols.add(symbol)
        rows.append({
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": _entry_price(orders, symbol, position),
            "close_price": None,
            "status": "open",
            "closed": False,
            "action": "cover" if side == "SHORT" else "sell",
            "reason": None,
        })
    closed_symbols = []
    for order in orders:
        symbol = str(order.get("symbol") or "").strip().upper()
        if not symbol or symbol in open_symbols or symbol in closed_symbols:
            continue
        if _on_date(order.get("timestamp"), today) and _is_close(order):
            closed_symbols.append(symbol)
    for symbol in closed_symbols:
        symbol_orders = [
            order for order in orders
            if str(order.get("symbol") or "").strip().upper() == symbol
        ]
        closing = [order for order in symbol_orders if _is_close(order) and _on_date(order.get("timestamp"), today)]
        if not closing:
            continue
        last_close = closing[-1]
        side = _closed_side(last_close)
        rows.append({
            "symbol": symbol,
            "side": side,
            "quantity": last_close.get("quantity"),
            "price": _entry_price(symbol_orders, symbol, None),
            "close_price": _number(last_close.get("price")),
            "status": "closed",
            "closed": True,
            "action": None,
            "reason": _text(last_close.get("reason")),
        })
    if not rows:
        note = EMPTY_DAY_NOTE
    else:
        note = DAY_TRADE_NOTE
    return {"rows": rows, "note": note}


def _entry_price(orders, symbol, position):
    opening = [
        order for order in orders
        if str(order.get("symbol") or "").strip().upper() == symbol and not _is_close(order)
    ]
    if opening:
        price = _number(opening[-1].get("price"))
        if price is not None:
            return price
    if position is None:
        closing = [
            order for order in orders
            if str(order.get("symbol") or "").strip().upper() == symbol and _is_close(order)
        ]
        if closing:
            return _number(closing[-1].get("price"))
        return None
    return _number(position.get("average_price"))


def _closed_side(order):
    effect = str(order.get("effect") or "").upper()
    if effect == "CLOSE_SHORT":
        return "SHORT"
    if effect == "CLOSE_LONG":
        return "LONG"
    if str(order.get("side") or "").upper() == "BUY":
        return "SHORT"
    return "LONG"


def _is_close(order):
    effect = str(order.get("effect") or "").upper()
    if effect in {"CLOSE_LONG", "CLOSE_SHORT"}:
        return True
    if effect in {"OPEN_LONG", "OPEN_SHORT"}:
        return False
    return str(order.get("side") or "").upper() == "SELL"


def _on_date(stamp, day):
    parsed = _parse_time(stamp)
    if parsed is None:
        return False
    return as_new_york(parsed).date() == day


def _parse_time(stamp):
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return float(value)


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None
