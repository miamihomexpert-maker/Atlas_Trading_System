from datetime import datetime
import json
import os
from pathlib import Path
import uuid

from atlas.broker import PaperBroker
from atlas.config import LIVE_TRADING
from atlas.database import AtlasDatabase
from atlas.logger import get_logger
from atlas.news import NewsEvent
from atlas.pipeline import run_paper_pipeline
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
        history = get_stock_history(symbol, period=period, interval=interval)
        if history is not None and not history.empty:
            histories[timeframe] = history
    return histories


def load_news_events(path=None):
    """Load caller-supplied news. No file means there is nothing to assess."""
    if path is None:
        path = os.getenv("ATLAS_NEWS_FILE")
    if not path:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("ATLAS_NEWS_FILE must contain a JSON list of events.")
    events = []
    for item in payload:
        published_at = datetime.fromisoformat(
            str(item["published_at"]).replace("Z", "+00:00")
        )
        events.append(
            NewsEvent(
                event_id=item["event_id"],
                title=item["title"],
                category=item["category"],
                published_at=published_at,
                sentiment=float(item["sentiment"]),
                impact=float(item["impact"]),
                instruments=tuple(item.get("instruments") or ()),
            )
        )
    return events


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
    if news_events is None:
        news_events = load_news_events()

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

        for symbol in settings.instruments:
            results.append(
                _run_symbol(
                    symbol,
                    broker,
                    database,
                    timeframes,
                    minimum_agreement,
                    histories_by_symbol,
                    news_events,
                    approved,
                    require_remote_approval,
                    settings.max_market_data_age_seconds,
                )
            )
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
    approved,
    require_remote_approval,
    max_market_data_age_seconds,
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
        )
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
