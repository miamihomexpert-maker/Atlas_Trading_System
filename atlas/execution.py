from datetime import datetime, timezone
from math import isfinite

from atlas.risk import (
    RiskLimits,
    calculate_position_size,
    validate_daily_loss,
)


def execute_paper_signal(
    signal,
    broker,
    client_order_id,
    approved=False,
    limits=None,
    daily_pnl=0.0,
    quantity=None,
    max_data_age_seconds=900,
    approval_token=None,
    approval_chat_id=None,
    approval_database=None,
):
    """Risk-check and submit an explicitly approved paper order.

    Daily loss comes from ``broker.daily_pnl()`` (broker state and, when
    attached, the database). ``daily_pnl`` is accepted for compatibility and
    is not used, so a caller cannot pass 0 and skip the halt.
    """
    del daily_pnl
    limits = limits or RiskLimits()
    remote_approval = approval_token is not None
    if not approved and not remote_approval:
        raise PermissionError("Signal requires explicit human approval.")
    if remote_approval and (
        approval_database is None
        or not str(approval_chat_id or "").strip()
    ):
        raise ValueError("Remote approval requires its database and authorized chat ID.")
    if not isinstance(signal, dict):
        raise ValueError("Signal must be a mapping.")

    symbol = str(signal.get("instrument", "")).strip().upper()
    side = str(signal.get("direction", "")).strip().upper()
    entry = signal.get("entry_price")
    stop = signal.get("stop_loss")
    target = signal.get("profit_target")
    if not symbol or side not in {"BUY", "SELL"}:
        raise ValueError("Signal must include an instrument and BUY/SELL direction.")
    market_timestamp = signal.get("market_timestamp")
    if not isinstance(market_timestamp, str) or not market_timestamp.strip():
        raise PermissionError("Signal has no source-market timestamp.")
    try:
        source_time = datetime.fromisoformat(
            market_timestamp.strip().replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ValueError("Signal has an invalid source-market timestamp.") from error
    if source_time.tzinfo is None:
        source_time = source_time.replace(tzinfo=timezone.utc)
    if (
        not isfinite(max_data_age_seconds)
        or max_data_age_seconds <= 0
    ):
        raise ValueError("Maximum market-data age must be a positive finite number.")
    age_seconds = (
        datetime.now(timezone.utc) - source_time.astimezone(timezone.utc)
    ).total_seconds()
    if age_seconds < -60 or age_seconds > max_data_age_seconds:
        raise PermissionError("Signal rejected because source market data is stale.")
    if (
        isinstance(entry, bool)
        or isinstance(stop, bool)
        or isinstance(target, bool)
        or not isinstance(entry, (int, float))
        or not isinstance(stop, (int, float))
        or not isinstance(target, (int, float))
        or not isfinite(entry)
        or not isfinite(stop)
        or not isfinite(target)
        or entry <= 0
        or stop <= 0
        or target <= 0
    ):
        raise ValueError("Signal entry, stop, or target prices are invalid.")
    if side == "BUY" and not stop < entry < target:
        raise ValueError("BUY signals require stop < entry < target.")
    if side == "SELL" and not target < entry < stop:
        raise ValueError("SELL signals require target < entry < stop.")

    account = broker.account_info()
    equity = account["equity"]
    if not validate_daily_loss(equity, broker.daily_pnl(), limits):
        raise PermissionError("Daily loss limit reached; trading is halted.")

    positions = broker.positions()
    current_exposure = sum(
        position["quantity"] * position["last_price"]
        for position in positions.values()
    )
    risk_quantity = calculate_position_size(
        equity,
        entry,
        stop,
        limits,
        current_exposure=current_exposure if side == "BUY" else 0.0,
    )
    if quantity is None:
        quantity = risk_quantity
        if side == "SELL":
            quantity = min(
                quantity,
                positions.get(symbol, {}).get("quantity", 0),
            )
    elif (
        isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or quantity <= 0
        or quantity > risk_quantity
    ):
        raise ValueError("Requested quantity exceeds the configured risk limit.")
    if quantity <= 0:
        raise PermissionError("Risk limits or available position permit no shares.")

    if remote_approval:
        claim = approval_database.claim_trade_approval(
            approval_token,
            approval_chat_id,
            client_order_id,
            signal,
        )
        if claim["status"] != "EXECUTING":
            raise PermissionError(
                f"Remote approval cannot authorize this order: {claim['status']}."
            )

    order = broker.submit_order(
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=float(entry),
        client_order_id=client_order_id,
        approved=True,
        stop_loss=float(stop),
        profit_target=float(target) if side == "BUY" else None,
        limits=limits,
    )
    if remote_approval:
        approval_database.complete_trade_approval(
            approval_token,
            client_order_id,
        )
    return order


def get_execution_status():
    return "PAPER EXECUTION READY; HUMAN APPROVAL REQUIRED; LIVE TRADING DISABLED"
