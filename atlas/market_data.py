from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from atlas.logger import get_logger


logger = get_logger()


OHLCV_COLUMNS = ("Open", "High", "Low", "Close", "Volume")
TIMEFRAME_RULES = {
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "1d": "1D",
    "1w": "1W",
}
# Yahoo Finance indexes bars by their open. These durations are the bar's coverage.
_BAR_DURATION = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "60m": timedelta(hours=1),
    "1h": timedelta(hours=1),
    "90m": timedelta(minutes=90),
}
_SESSION_CLOSE = (16, 0)


def normalize_ohlcv(history):
    """Return sorted, de-duplicated OHLCV data.

    Invalid bars are dropped. Valid bars are kept. If every bar is invalid,
    this raises and does not invent a replacement price.
    """
    if history is None:
        raise ValueError("Market data provider returned no history.")
    if not isinstance(history, pd.DataFrame):
        raise TypeError("Market history must be a pandas DataFrame.")
    if history.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS, index=history.index)

    data = history.copy()
    data.columns = [str(column).title() for column in data.columns]
    missing = set(OHLCV_COLUMNS) - set(data.columns)
    if missing:
        raise ValueError(f"Market history is missing columns: {sorted(missing)}")
    data = data.loc[:, list(OHLCV_COLUMNS)]
    data = data.apply(pd.to_numeric, errors="coerce")
    finite = data.notna().all(axis=1) & ~data.isin(
        [float("inf"), float("-inf")]
    ).any(axis=1)
    ordered = (
        data["Close"].gt(0)
        & data["Volume"].ge(0)
        & data["High"].ge(data["Low"])
        & data["High"].ge(data["Open"])
        & data["High"].ge(data["Close"])
        & data["Low"].le(data["Open"])
        & data["Low"].le(data["Close"])
    )
    kept = data.loc[finite & ordered]
    if kept.empty:
        raise ValueError("Market history contains invalid OHLCV bar values.")
    kept = kept.sort_index()
    return kept.loc[~kept.index.duplicated(keep="last")]


def resample_ohlcv(history, timeframe):
    """Aggregate finer OHLCV bars into a larger supported interval."""
    if timeframe not in TIMEFRAME_RULES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    data = normalize_ohlcv(history)
    if data.empty:
        return data
    result = data.resample(
        TIMEFRAME_RULES[timeframe],
        label="left",
        closed="left",
    ).agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    result = result.dropna(subset=["Open", "High", "Low", "Close"])
    return normalize_ohlcv(result)


def get_market_status():
    return "MARKET DATA MODULE ONLINE"


def _as_utc(stamp):
    if hasattr(stamp, "to_pydatetime"):
        stamp = stamp.to_pydatetime()
    if not isinstance(stamp, datetime):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _session_close(bar_open):
    """Regular-session close on the bar's New York calendar date."""
    eastern = bar_open.astimezone(ZoneInfo("America/New_York"))
    hour, minute = _SESSION_CLOSE
    return eastern.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(
        timezone.utc
    )


def bar_coverage_end(bar_open, timeframe):
    """When the bar stops describing the market.

    Intraday Yahoo bars are labeled at the open, so a 13:30 1h bar covers
    13:30–14:30. Daily and weekly bars are labeled at midnight and cover
    through that session's 16:00 America/New_York close.
    """
    opened = _as_utc(bar_open)
    if opened is None:
        raise ValueError("Bar timestamp must be a datetime.")
    name = str(timeframe).strip().lower()
    if name in {"1d", "1day", "d"}:
        return _session_close(opened)
    if name in {"1w", "1wk", "1week", "w"}:
        eastern = opened.astimezone(ZoneInfo("America/New_York"))
        days_to_friday = (4 - eastern.weekday()) % 7
        friday = eastern + timedelta(days=days_to_friday)
        return _session_close(friday)
    duration = _BAR_DURATION.get(name)
    if duration is None:
        return opened
    return opened + duration


def bar_as_of(bar_open, timeframe, observed_at):
    """Return when this fetched bar was last current.

    A bar that still covers ``observed_at`` is current as of the fetch.
    A bar that already ended is current only through its end. A bar that
    opens in the future is left in the future so callers can reject it.
    """
    opened = _as_utc(bar_open)
    observed = _as_utc(observed_at)
    if opened is None or observed is None:
        raise ValueError("Bar and observation times must be datetimes.")
    if opened > observed + timedelta(seconds=60):
        return opened
    ended = bar_coverage_end(opened, timeframe)
    if observed < ended:
        return observed
    return ended


def freshest_observation(histories, observed_at=None):
    """Pick the newest bar across timeframes by when its data was current.

    Price comes from that bar. Ties prefer the shorter timeframe.
    """
    if not histories:
        return None
    observed = _as_utc(observed_at or datetime.now(timezone.utc))
    if observed is None:
        raise ValueError("observed_at must be a datetime.")
    best = None
    for timeframe, frame in histories.items():
        if frame is None or getattr(frame, "empty", True):
            continue
        opened = _as_utc(frame.index[-1])
        try:
            price = float(frame["Close"].iloc[-1])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if opened is None or price <= 0:
            continue
        as_of = bar_as_of(opened, timeframe, observed)
        duration = _BAR_DURATION.get(str(timeframe).strip().lower(), timedelta(days=365))
        candidate = (as_of, -duration, price, str(timeframe), opened)
        if best is None or (candidate[0], candidate[1]) > (best[0], best[1]):
            best = candidate
    if best is None:
        return None
    as_of, _neg_duration, price, timeframe, opened = best
    return {
        "as_of": as_of,
        "price": price,
        "timeframe": timeframe,
        "bar_open": opened,
    }


def _fetch_history(symbol, period, interval):
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise ValueError("Instrument symbol is required.")
    try:
        history = yf.Ticker(normalized_symbol).history(period=period, interval=interval)
    except Exception as error:
        logger.info("%s history fetch skipped: %s", normalized_symbol, error)
        return None
    if history is None or getattr(history, "empty", True):
        return None
    try:
        return normalize_ohlcv(history)
    except (TypeError, ValueError) as error:
        logger.info("%s history dropped: %s", normalized_symbol, error)
        return None


def get_stock_price(symbol):
    history = _fetch_history(symbol, period="1d", interval="1m")
    if history is None:
        return None
    return float(history["Close"].iloc[-1])


def get_stock_history(symbol, period="1mo", interval="1d"):
    return _fetch_history(symbol, period=period, interval=interval)


def get_intraday_history(symbol, period="1d", interval="1m"):
    return _fetch_history(symbol, period=period, interval=interval)


def get_multi_timeframe_history(
    symbol,
    timeframes=("5m", "15m", "1h", "1d"),
    period="1mo",
):
    """Fetch supported intervals separately; callers choose suitable periods."""
    requested = tuple(dict.fromkeys(timeframes))
    unsupported = set(requested) - set(TIMEFRAME_RULES)
    if unsupported:
        raise ValueError(f"Unsupported timeframes: {sorted(unsupported)}")
    histories = {}
    for timeframe in requested:
        history = _fetch_history(symbol, period=period, interval=timeframe)
        if history is not None:
            histories[timeframe] = history
    return histories
