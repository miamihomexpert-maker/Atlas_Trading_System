"""Close still-open paper positions at the regular-session close.

This is the one paper close that does not wait for a dashboard confirm.
It sells a long and buys to cover a short. It does not open a trade.
The fill price is the last bar from that session. A missing price leaves
the position open and is recorded. Live trading stays off.
"""

from datetime import datetime, timedelta, timezone
import os
from math import isfinite

from atlas.config import LIVE_TRADING
from atlas.database import AtlasDatabase
from atlas.logger import get_logger
from atlas.market_data import freshest_observation
from atlas.market_hours import as_new_york, regular_session_status
from atlas.broker import PaperBroker
from config.settings import load_settings


logger = get_logger()

EOD_FLATTEN_REASON = (
    "Closed at the regular-session close because the position was still open."
)
EOD_NO_PRICE_REASON = (
    "No session price was available, so the position was left open."
)


def flatten_open_paper_positions(
    settings=None,
    broker=None,
    database=None,
    histories_by_symbol=None,
    now=None,
    timeframes=None,
):
    """Flatten every open paper position at or after 16:00 America/New_York.

    A call before the close does nothing. A symbol with no session price is
    left open. Calling this again does not fill a position that is already
    flat, including one the user closed themselves.
    """
    if LIVE_TRADING:
        raise PermissionError("Live trading is disabled.")
    settings = settings or load_settings()
    if settings.live_trading_enabled:
        raise PermissionError("Live trading is disabled.")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone.")
    _is_open, reason = regular_session_status(current)
    report = {
        "live_trading": False,
        "trading_mode": "PAPER",
        "flattened": [],
        "unfilled": [],
        "skipped": None,
    }
    if reason != "after_close":
        report["skipped"] = reason
        return report

    created_database = database is None
    try:
        if database is None:
            database = AtlasDatabase(
                os.getenv("ATLAS_DATABASE_PATH", "database/atlas.sqlite3")
            )
        if broker is None:
            broker = PaperBroker(
                initial_cash=settings.paper_initial_cash,
                database=database,
            )
        if broker.account_info().get("trading_mode") != "PAPER":
            raise PermissionError("Live trading is disabled.")
        session_day = as_new_york(current).date().isoformat()
        stamp = current.isoformat()
        for symbol in sorted(broker.positions()):
            _flatten_one(
                broker,
                database,
                symbol,
                current,
                session_day,
                stamp,
                histories_by_symbol,
                timeframes,
                report,
            )
    finally:
        if created_database and database is not None:
            database.close()
    logger.info(
        "End-of-day flatten closed %s; unfilled %s; live trading disabled",
        len(report["flattened"]),
        len(report["unfilled"]),
    )
    return report


def session_price(histories, now):
    """Return (timestamp, price) for the last bar of this regular session.

    The price is the bar close from ``freshest_observation``. Nothing is
    filled in when that bar is missing, from another date, or still in the
    future relative to ``now``.
    """
    if not histories:
        return None
    try:
        observed = freshest_observation(histories, observed_at=now)
    except Exception as error:
        logger.info("Session price unavailable: %s", error)
        return None
    if not observed:
        return None
    price = observed.get("price")
    as_of = observed.get("as_of")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    if not isfinite(price) or price <= 0 or as_of is None:
        return None
    if as_of.tzinfo is None:
        return None
    eastern_now = as_new_york(now)
    eastern_as_of = as_of.astimezone(eastern_now.tzinfo)
    if eastern_as_of.date() != eastern_now.date():
        return None
    session_open = eastern_now.replace(hour=9, minute=30, second=0, microsecond=0)
    if eastern_as_of < session_open:
        return None
    if as_of > now.astimezone(timezone.utc) + timedelta(seconds=60):
        return None
    return as_of.isoformat(), float(price)


def _flatten_one(
    broker,
    database,
    symbol,
    now,
    session_day,
    stamp,
    histories_by_symbol,
    timeframes,
    report,
):
    position = broker.positions().get(symbol)
    if not position:
        return
    histories = _histories_for(symbol, histories_by_symbol, timeframes)
    quote = session_price(histories, now)
    if quote is None:
        _audit(
            database,
            "EOD_FLATTEN_UNFILLED",
            symbol,
            {
                "reason": EOD_NO_PRICE_REASON,
                "session_date": session_day,
                "timestamp": stamp,
                "scan_decision_unchanged": True,
            },
        )
        report["unfilled"].append({
            "symbol": symbol,
            "reason": EOD_NO_PRICE_REASON,
        })
        logger.info("%s end-of-day flatten left open: no session price", symbol)
        return
    market_timestamp, price = quote
    try:
        order = broker.close_open_position(
            symbol,
            price,
            f"eod-flatten-{session_day}-{symbol}",
            EOD_FLATTEN_REASON,
            approved=True,
            timestamp=stamp,
        )
    except PermissionError as error:
        if "Live trading" in str(error):
            raise
        _audit(
            database,
            "EOD_FLATTEN_UNFILLED",
            symbol,
            {
                "reason": str(error),
                "session_date": session_day,
                "timestamp": stamp,
                "scan_decision_unchanged": True,
            },
        )
        report["unfilled"].append({"symbol": symbol, "reason": str(error)})
        return
    except Exception as error:
        _audit(
            database,
            "EOD_FLATTEN_UNFILLED",
            symbol,
            {
                "reason": str(error),
                "session_date": session_day,
                "timestamp": stamp,
                "scan_decision_unchanged": True,
            },
        )
        report["unfilled"].append({"symbol": symbol, "reason": str(error)})
        logger.info("%s end-of-day flatten failed: %s", symbol, error)
        return
    if symbol in broker.positions():
        note = "The end-of-day close was already recorded and the position is still open."
        _audit(
            database,
            "EOD_FLATTEN_UNFILLED",
            symbol,
            {
                "reason": note,
                "session_date": session_day,
                "timestamp": stamp,
                "order_id": order.get("order_id"),
                "scan_decision_unchanged": True,
            },
        )
        report["unfilled"].append({"symbol": symbol, "reason": note})
        return
    _audit(
        database,
        "EOD_FLATTEN",
        symbol,
        {
            "reason": EOD_FLATTEN_REASON,
            "session_date": session_day,
            "timestamp": stamp,
            "order_id": order["order_id"],
            "side": order["side"],
            "effect": order.get("effect"),
            "quantity": order["quantity"],
            "price": order["price"],
            "market_timestamp": market_timestamp,
            "scan_decision_unchanged": True,
        },
    )
    _clear_exit_alerts(database, symbol)
    report["flattened"].append(order)
    logger.info(
        "%s end-of-day flatten %s %s @ %s",
        symbol,
        order.get("effect"),
        order.get("side"),
        order.get("price"),
    )


def _histories_for(symbol, histories_by_symbol, timeframes):
    if histories_by_symbol is not None:
        normalized = str(symbol).strip().upper()
        return histories_by_symbol.get(symbol) or histories_by_symbol.get(normalized)
    from atlas.session import fetch_session_histories, resolve_timeframes

    try:
        return fetch_session_histories(symbol, resolve_timeframes(timeframes))
    except Exception as error:
        logger.info("%s history skipped at the close: %s", symbol, error)
        return None


def _audit(database, action, symbol, detail):
    if database is None or not hasattr(database, "record_audit_event"):
        return
    database.record_audit_event(action, symbol, detail)


def _clear_exit_alerts(database, symbol):
    if database is None or not hasattr(database, "supersede_exit_alerts"):
        return
    for reason in ("stop", "target", "opposing"):
        database.supersede_exit_alerts(symbol, reason)
