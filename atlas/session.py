from datetime import datetime, timezone
import os
import time
import uuid

from atlas.broker import PaperBroker
from atlas.config import LIVE_TRADING
from atlas.database import AtlasDatabase
from atlas.execution import execute_paper_signal
from atlas.logger import get_logger
from atlas.market_hours import regular_session_status
from atlas.pipeline import _remote_approval_required, run_paper_pipeline
from config.settings import load_settings


logger = get_logger()

_TIMEFRAME_PERIODS = {
    "5m": "5d",
    "15m": "5d",
    "30m": "1mo",
    "1h": "1mo",
    "1d": "6mo",
    "1w": "2y",
    "1wk": "2y",
}


def hard_stop_enabled(explicit=None):
    """Auto-sell only when price trades through the stored stop and this is on."""
    if explicit is not None:
        return bool(explicit)
    return _env_flag("ATLAS_HARD_STOP", False)


def _env_flag(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def resolve_timeframes(timeframes=None):
    if timeframes is not None:
        parsed = tuple(
            str(item).strip()
            for item in timeframes
            if str(item).strip()
        )
    else:
        parsed = tuple(
            item.strip()
            for item in os.getenv("ATLAS_TIMEFRAMES", "1h,1d").split(",")
            if item.strip()
        )
    if not parsed:
        raise ValueError("At least one timeframe is required.")
    return parsed


def fetch_session_histories(symbol, timeframes):
    """Fetch each timeframe with a period Yahoo Finance accepts for that interval.

    Returns only intervals that came back with bars. Callers decide what to do
    when the mapping is empty. This does not invent prices.
    """
    from atlas.market_data import get_stock_history

    histories = {}
    for timeframe in timeframes:
        interval = "1wk" if timeframe == "1w" else timeframe
        period = _TIMEFRAME_PERIODS.get(timeframe, "1mo")
        try:
            history = get_stock_history(symbol, period=period, interval=interval)
        except Exception as error:
            logger.info("%s %s history skipped: %s", symbol, timeframe, error)
            continue
        if history is not None and not history.empty:
            histories[timeframe] = history
    return histories


def load_news_events(path=None):
    """Load the JSON news override. An unset path returns an empty list."""
    from atlas.news_feed import load_news_file

    if path is None:
        path = os.getenv("ATLAS_NEWS_FILE", "").strip()
    if not path:
        return []
    return load_news_file(path)


def run_paper_session(
    settings=None,
    broker=None,
    database=None,
    histories_by_symbol=None,
    news_events=None,
    approved=None,
    require_remote_approval=None,
    timeframes=None,
    minimum_agreement=None,
    manual_exits=False,
    hard_stop=None,
    company_loader=None,
):
    """Run one paper pass over the configured watchlist.

    Orders use ``run_paper_pipeline``, so risk limits, approval, and the paper
    broker stay on the fill path. Live trading is refused.
    """
    if LIVE_TRADING:
        raise PermissionError("Live trading is disabled.")
    settings = settings or load_settings()
    if settings.live_trading_enabled:
        raise PermissionError("Live trading is disabled.")

    timeframes = resolve_timeframes(timeframes)
    if minimum_agreement is None:
        minimum_agreement = 2 if len(timeframes) >= 2 else 1
    if approved is None:
        approved = _env_flag("ATLAS_PAPER_APPROVED", False)
    fetch_news = news_events is None
    if fetch_news:
        news_events = None
        news_report = None
    else:
        news_report = {"status": "OVERRIDE", "error": None, "source": "caller"}

    created_database = database is None
    results = []
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
        results.extend(
            _collect_cycle_results(
                settings,
                broker,
                database,
                timeframes,
                minimum_agreement,
                histories_by_symbol,
                news_events,
                news_report,
                approved,
                require_remote_approval,
                manual_exits=manual_exits,
                hard_stop=hard_stop_enabled(hard_stop),
                company_loader=company_loader,
                fetch_news=fetch_news,
            )
        )
        _notify_cycle(results)
    finally:
        if created_database and database is not None:
            database.close()

    summary = {
        "mode": "paper",
        "trading_mode": "PAPER",
        "live_trading": False,
        "instruments": list(settings.instruments),
        "timeframes": list(timeframes),
        "approved": bool(approved),
        "results": results,
        "fill_count": sum(
            1
            for item in results
            if item.get("order") and item["order"].get("status") == "FILLED"
        ),
    }
    logger.info(
        "Paper session finished; fills=%s; live trading disabled",
        summary["fill_count"],
    )
    return summary


def _run_symbol(
    symbol,
    broker,
    database,
    timeframes,
    minimum_agreement,
    histories_by_symbol,
    news_events,
    news_report,
    approved,
    require_remote_approval,
    max_market_data_age_seconds,
    manual_exits=False,
    hard_stop=False,
    execute=None,
):
    try:
        if histories_by_symbol is not None:
            histories = histories_by_symbol.get(symbol) or histories_by_symbol.get(
                str(symbol).strip().upper()
            )
        else:
            histories = fetch_session_histories(symbol, timeframes)
        if not histories:
            logger.info("%s has no market bars; no order submitted", symbol)
            return {
                "symbol": str(symbol).strip().upper(),
                "status": "NO_DATA",
                "order": None,
                "execution": "NOT_SUBMITTED",
            }
        result = run_paper_pipeline(
            symbol,
            histories,
            broker,
            f"paper-{symbol}-{uuid.uuid4().hex[:12]}",
            approved=approved,
            minimum_agreement=minimum_agreement,
            max_data_age_seconds=max_market_data_age_seconds,
            require_remote_approval=require_remote_approval,
            database=database,
            news_events=news_events,
            news_report=news_report,
            execute=not manual_exits if execute is None else execute,
        )
        if (
            manual_exits
            and result.get("decision") == "SELL"
            and str(symbol).strip().upper() in broker.positions()
        ):
            quote = _latest_quote(histories)
            price = quote[1] if quote else (result.get("recommendation") or {}).get("entry_price")
            position = broker.positions().get(str(symbol).strip().upper(), {})
            _stage_exit_alert(
                database,
                symbol,
                "opposing",
                price,
                position.get("stop_loss"),
                position.get("profit_target"),
                (result.get("recommendation") or {}).get("explanation")
                or result.get("analysis", {}).get("reason")
                or "Technical decision is SELL.",
            )
            result["exit_reason"] = "opposing"
            result["execution"] = "RECOMMENDED"
            result["order"] = None
    except PermissionError as error:
        if "Live trading" in str(error):
            raise
        logger.info("%s not submitted: %s", symbol, error)
        return {
            "symbol": str(symbol).strip().upper(),
            "order": None,
            "execution": "NOT_SUBMITTED",
            "error": str(error),
        }
    except Exception as error:
        logger.info("%s paper pass failed: %s", symbol, error)
        return {
            "symbol": str(symbol).strip().upper(),
            "status": "ERROR",
            "order": None,
            "execution": "NOT_SUBMITTED",
            "error": str(error),
        }

    logger.info(
        "%s technical=%s decision=%s news=%s model=%s execution=%s",
        result["symbol"],
        result.get("technical_signal"),
        result.get("decision"),
        (result.get("news") or {}).get("status"),
        (result.get("adaptive") or {}).get("status"),
        result.get("execution"),
    )
    return result


def resolve_loop_interval(interval_seconds=None):
    if interval_seconds is None:
        interval_seconds = int(os.getenv("ATLAS_LOOP_INTERVAL_SECONDS", "300"))
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        raise ValueError("ATLAS_LOOP_INTERVAL_SECONDS must be a positive integer.")
    if interval_seconds <= 0:
        raise ValueError("ATLAS_LOOP_INTERVAL_SECONDS must be a positive integer.")
    return interval_seconds


def run_paper_loop(
    interval_seconds=None,
    max_cycles=None,
    now=None,
    sleep=None,
    **session_kwargs
):
    """Repeat a paper session on US regular-session hours.

    Outside 09:30-16:00 America/New_York on weekdays the loop sleeps and does
    not fetch or submit. KeyboardInterrupt stops the loop. Live trading is refused.
    ``sleep`` and ``now`` are injection points for tests.
    """
    if LIVE_TRADING:
        raise PermissionError("Live trading is disabled.")
    interval_seconds = resolve_loop_interval(interval_seconds)
    if max_cycles is not None and (
        isinstance(max_cycles, bool)
        or not isinstance(max_cycles, int)
        or max_cycles <= 0
    ):
        raise ValueError("max_cycles must be a positive integer when provided.")
    session_kwargs.setdefault("manual_exits", True)
    session_kwargs.setdefault("hard_stop", hard_stop_enabled())
    clock = now or (lambda: datetime.now(timezone.utc))
    pause = sleep or time.sleep
    cycles = []
    stopped = None
    try:
        while True:
            current = clock()
            is_open, reason = regular_session_status(current)
            if not is_open:
                logger.info(
                    "US regular session is %s; sleeping %s seconds",
                    reason,
                    interval_seconds,
                )
                cycles.append({"market": reason, "ran": False})
            else:
                summary = run_paper_session(**session_kwargs)
                cycles.append({
                    "market": reason,
                    "ran": True,
                    "fill_count": summary["fill_count"],
                    "summary": summary,
                })
            if max_cycles is not None and len(cycles) >= max_cycles:
                break
            pause(interval_seconds)
    except KeyboardInterrupt:
        logger.info("Paper loop stopped")
        stopped = "interrupt"
    return {
        "mode": "paper-loop",
        "trading_mode": "PAPER",
        "live_trading": False,
        "interval_seconds": interval_seconds,
        "cycles": cycles,
        "stopped": stopped,
    }


def run_book_status(database=None, now=None):
    """Print the paper book from the ledger. Does not submit orders."""
    if LIVE_TRADING:
        raise PermissionError("Live trading is disabled.")
    settings = load_settings()
    if settings.live_trading_enabled:
        raise PermissionError("Live trading is disabled.")
    created_database = database is None
    path = os.getenv("ATLAS_DATABASE_PATH", "database/atlas.sqlite3")
    if database is None and path != ":memory:" and not os.path.exists(path):
        text = "No paper ledger yet. Live trading is disabled."
        print(text)
        return {
            "mode": "status-book",
            "live_trading": False,
            "ledger": False,
            "text": text,
        }
    if database is None:
        database = AtlasDatabase(path)
    try:
        broker = PaperBroker(
            initial_cash=settings.paper_initial_cash,
            database=database,
        )
        report = _book_report(broker, database, now=now)
    finally:
        if created_database:
            database.close()
    print(report["text"])
    logger.info("Paper book status printed; live trading disabled")
    return report


def _book_report(broker, database, now=None):
    account = broker.account_info()
    daily = broker.daily_pnl(now)
    lines = [
        "ATLAS paper book",
        "trading_mode: PAPER",
        "live_trading: false",
        f"cash: {account['cash']:.2f}",
        f"equity: {account['equity']:.2f}",
        f"daily_pnl: {daily:.2f}",
        "positions:",
    ]
    positions = broker.positions()
    if not positions:
        lines.append("  (none)")
    for symbol, position in sorted(positions.items()):
        stop = position.get("stop_loss")
        target = position.get("profit_target")
        lines.append(
            "  {symbol} qty={qty} avg={avg:.2f} last={last:.2f} "
            "stop={stop} target={target}".format(
                symbol=symbol,
                qty=position["quantity"],
                avg=position["average_price"],
                last=position["last_price"],
                stop=_price_text(stop),
                target=_price_text(target),
            )
        )
    lines.append("recent fills:")
    fills = database.list_paper_orders()[-10:]
    if not fills:
        lines.append("  (none)")
    for fill in fills:
        lines.append(
            f"  {fill['timestamp']} {fill['symbol']} {fill['side']} "
            f"{fill['quantity']} @ {float(fill['price']):.2f}"
        )
    text = "\n".join(lines)
    return {
        "mode": "status-book",
        "live_trading": False,
        "ledger": True,
        "trading_mode": "PAPER",
        "cash": account["cash"],
        "equity": account["equity"],
        "daily_pnl": daily,
        "positions": positions,
        "recent_fills": fills,
        "text": text,
    }


def _price_text(value):
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    return "n/a"


def _collect_cycle_results(
    settings,
    broker,
    database,
    timeframes,
    minimum_agreement,
    histories_by_symbol,
    news_events,
    news_report,
    approved,
    require_remote_approval,
    manual_exits=False,
    hard_stop=False,
    company_loader=None,
    fetch_news=False,
):
    from atlas.ranking import card_from_pipeline, rank_scans
    from atlas.universe import next_batch, scan_batch_size

    universe = list(settings.instruments)
    batch_size = scan_batch_size()
    large_universe = histories_by_symbol is None and len(universe) > batch_size
    if large_universe:
        start = int(database.get_metadata("scan_batch_index") or 0)
        batch, next_index = next_batch(universe, start, batch_size)
        database.set_metadata("scan_batch_index", str(next_index))
        database.set_metadata(
            "scan_batch_at",
            datetime.now(timezone.utc).isoformat(),
        )
    else:
        batch = universe
    symbols = list(batch)
    for symbol in broker.positions():
        if symbol not in symbols:
            symbols.append(symbol)
    if fetch_news:
        from atlas.news_feed import resolve_news

        news_events, news_report = resolve_news(symbols)
    from atlas.baseline import auto_train_enabled, ensure_baseline_model

    if auto_train_enabled():
        train_symbols = symbols[:1] if large_universe else symbols
        ensure_baseline_model(database, train_symbols)
    results = []
    for symbol in symbols:
        try:
            histories = _histories_for(symbol, timeframes, histories_by_symbol)
        except Exception as error:
            logger.info("%s history skipped: %s", symbol, error)
            results.append({
                "symbol": str(symbol).strip().upper(),
                "status": "NO_DATA",
                "order": None,
                "execution": "NOT_SUBMITTED",
                "error": str(error),
            })
            continue
        try:
            exit_result = _protective_exit(
                symbol,
                histories,
                broker,
                approved,
                require_remote_approval,
                settings.max_market_data_age_seconds,
                database,
                manual_exits=manual_exits,
                hard_stop=hard_stop,
            )
            if exit_result is not None:
                results.append(exit_result)
                if exit_result.get("execution") == "FILLED":
                    continue
            if symbol not in settings.instruments:
                continue
            if not histories:
                logger.info("%s has no market bars; no order submitted", symbol)
                empty = {
                    "symbol": str(symbol).strip().upper(),
                    "status": "NO_DATA",
                    "order": None,
                    "execution": "NOT_SUBMITTED",
                }
                _remember_scan(database, broker, empty)
                results.append(empty)
                continue
            result = _run_symbol(
                symbol,
                broker,
                database,
                timeframes,
                minimum_agreement,
                {str(symbol).strip().upper(): histories},
                news_events,
                news_report,
                approved,
                require_remote_approval,
                settings.max_market_data_age_seconds,
                manual_exits=manual_exits,
                hard_stop=hard_stop,
                execute=False if large_universe else None,
            )
            _remember_scan(database, broker, result)
            results.append(result)
        except Exception as error:
            logger.info("%s paper pass skipped: %s", symbol, error)
            results.append({
                "symbol": str(symbol).strip().upper(),
                "status": "NO_DATA",
                "order": None,
                "execution": "NOT_SUBMITTED",
                "error": str(error),
            })
    if large_universe and not manual_exits and approved:
        stored = database.list_symbol_scans()
        held = set(broker.positions())
        for card in rank_scans(stored)[:3]:
            if not card.get("buyable") or card["symbol"] in held or card["symbol"] not in batch:
                continue
            filled = _run_symbol(
                card["symbol"],
                broker,
                database,
                timeframes,
                minimum_agreement,
                None,
                news_events,
                news_report,
                approved,
                require_remote_approval,
                settings.max_market_data_age_seconds,
                manual_exits=False,
                hard_stop=hard_stop,
                execute=True,
            )
            results.append(filled)
    _remember_coverage(database, universe)
    _attach_company_facts(results, batch, company_loader)
    return results


def _remember_scan(database, broker, result):
    from atlas.ranking import card_from_pipeline

    if not (result.get("analysis") or result.get("decision") or result.get("status") == "NO_DATA"):
        return
    account = broker.account_info()
    card = card_from_pipeline(result, account["equity"], broker.current_exposure())
    if result.get("status") == "NO_DATA" and not result.get("decision"):
        card["decision"] = "NO_DATA"
        card["reason"] = card["reason"] or "No market bars returned."
        card["buyable"] = False
    database.upsert_symbol_scan(card["symbol"], card)


def _remember_coverage(database, universe):
    import json

    stored = {card["symbol"] for card in database.list_symbol_scans()}
    universe_symbols = {str(symbol).strip().upper() for symbol in universe}
    database.set_metadata(
        "scan_coverage",
        json.dumps({
            "universe_size": len(universe_symbols),
            "scanned_count": len(stored & universe_symbols),
            "source": "config/sp500.txt",
        }),
    )


def _attach_company_facts(results, instruments, company_loader):
    """Attach a display-only profile. It is not an input to the decision."""
    from atlas.company import profile_for_symbol, resolve_company_loader

    loader = resolve_company_loader(company_loader)
    configured = {str(symbol).strip().upper() for symbol in instruments}
    cache = {}
    for result in results:
        symbol = str(result.get("symbol") or "").strip().upper()
        if symbol not in configured:
            continue
        if symbol not in cache:
            cache[symbol] = profile_for_symbol(symbol, loader)
            logger.info(
                "%s company=%s source=%s",
                symbol,
                cache[symbol].get("status"),
                cache[symbol].get("source"),
            )
        result["company"] = cache[symbol]


def _histories_for(symbol, timeframes, histories_by_symbol):
    normalized = str(symbol).strip().upper()
    if histories_by_symbol is not None:
        return histories_by_symbol.get(symbol) or histories_by_symbol.get(normalized)
    return fetch_session_histories(normalized, timeframes)


def _protective_exit(
    symbol,
    histories,
    broker,
    approved,
    require_remote_approval,
    max_market_data_age_seconds,
    database,
    manual_exits=False,
    hard_stop=False,
):
    normalized = str(symbol).strip().upper()
    position = broker.positions().get(normalized)
    if not position:
        return None
    has_plan = "stop_loss" in position and "profit_target" in position
    quote = _latest_quote(histories)
    if quote is None:
        if not has_plan:
            return None
        return {
            "symbol": normalized,
            "order": None,
            "execution": "NOT_SUBMITTED",
            "exit_reason": "no_price",
        }
    market_timestamp, price = quote
    if _quote_is_stale(market_timestamp, max_market_data_age_seconds):
        if not has_plan:
            return None
        return {
            "symbol": normalized,
            "order": None,
            "execution": "NOT_SUBMITTED",
            "exit_reason": "stale_price",
        }
    broker.update_market_price(normalized, price)
    if not has_plan:
        return None
    if price <= position["stop_loss"]:
        reason = "stop"
    elif price >= position["profit_target"]:
        reason = "target"
    else:
        if manual_exits:
            _clear_exit_alert(database, normalized, "stop")
            _clear_exit_alert(database, normalized, "target")
        return None
    if manual_exits and not (reason == "stop" and hard_stop):
        alert_id = _stage_exit_alert(
            database,
            normalized,
            reason,
            price,
            position.get("stop_loss"),
            position.get("profit_target"),
            (
                f"Latest close {price:.4f} is at or through the stored {reason} "
                f"({position.get('stop_loss') if reason == 'stop' else position.get('profit_target')})."
            ),
        )
        return {
            "symbol": normalized,
            "decision": "SELL",
            "order": None,
            "execution": "RECOMMENDED",
            "exit_reason": reason,
            "alert_id": alert_id,
        }
    signal = _exit_signal(normalized, price, market_timestamp)
    try:
        if _remote_approval_required(require_remote_approval):
            raise PermissionError("Remote approval token is required.")
        order = execute_paper_signal(
            signal,
            broker,
            f"exit-{normalized}-{reason}-{uuid.uuid4().hex[:12]}",
            approved=approved,
            max_data_age_seconds=max_market_data_age_seconds,
            approval_database=database,
        )
    except PermissionError as error:
        if "Live trading" in str(error):
            raise
        logger.info("%s %s exit not submitted: %s", normalized, reason, error)
        return {
            "symbol": normalized,
            "order": None,
            "execution": "NOT_SUBMITTED",
            "exit_reason": reason,
            "error": str(error),
        }
    logger.info("%s protective %s exit %s", normalized, reason, order["status"])
    if reason == "stop":
        _clear_exit_alert(database, normalized, "stop")
        if manual_exits and hard_stop and database is not None:
            database.record_audit_event(
                "HARD_STOP_EXECUTED",
                normalized,
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "price": price,
                    "stop_loss": position.get("stop_loss"),
                    "order_id": order["order_id"],
                },
            )
    return {
        "symbol": normalized,
        "decision": "SELL",
        "order": order,
        "execution": order["status"],
        "exit_reason": reason,
    }


def _latest_quote(histories):
    """Return (as-of timestamp, price) for the newest bar across timeframes.

    The timestamp is when that bar was current, not Yahoo's open label.
    """
    from atlas.market_data import freshest_observation

    if not histories:
        return None
    observed = freshest_observation(histories)
    if observed is None:
        return None
    return observed["as_of"].isoformat(), observed["price"]


def _quote_is_stale(market_timestamp, max_age_seconds):
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or max_age_seconds <= 0
    ):
        return True
    source = datetime.fromisoformat(str(market_timestamp).replace("Z", "+00:00"))
    if source.tzinfo is None:
        source = source.replace(tzinfo=timezone.utc)
    age = (
        datetime.now(timezone.utc) - source.astimezone(timezone.utc)
    ).total_seconds()
    return age < -60 or age > max_age_seconds


def _exit_signal(symbol, price, market_timestamp):
    return {
        "instrument": symbol,
        "direction": "SELL",
        "entry_price": float(price),
        "stop_loss": float(price) * 1.01,
        "profit_target": float(price) * 0.99,
        "market_timestamp": market_timestamp,
        "explanation": "Protective paper exit at the latest observed price.",
    }


def _notify_cycle(results):
    halt_noted = False
    for item in results:
        order = item.get("order")
        if order and order.get("status") == "FILLED":
            verb = "opened" if order.get("side") == "BUY" else "closed"
            _notify(
                f"Atlas paper {verb} {order.get('symbol')} {order.get('side')} "
                f"{order.get('quantity')} @ {order.get('price')}"
            )
        if not halt_noted and "Daily loss" in str(item.get("error", "")):
            halt_noted = True
            _notify("Atlas paper trading halted: daily loss limit reached.")


def _stage_exit_alert(database, symbol, reason, price, stop_loss, profit_target, message):
    if database is None or not hasattr(database, "upsert_exit_alert"):
        return None
    return database.upsert_exit_alert(
        symbol,
        reason,
        None if price is None else float(price),
        None if stop_loss is None else float(stop_loss),
        None if profit_target is None else float(profit_target),
        {
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def _clear_exit_alert(database, symbol, reason):
    if database is None or not hasattr(database, "supersede_exit_alerts"):
        return
    database.supersede_exit_alerts(symbol, reason)


def _notify(text):
    token = os.getenv("ATLAS_TELEGRAM_BOT_TOKEN")
    chat = os.getenv("ATLAS_TELEGRAM_CHAT_ID")
    if not token or not chat:
        return {"status": "SKIPPED"}
    try:
        from atlas.telegram import send_telegram_message

        send_telegram_message(text)
        return {"status": "SENT"}
    except Exception as error:
        logger.info("Telegram notification failed: %s", error)
        return {"status": "FAILED"}
