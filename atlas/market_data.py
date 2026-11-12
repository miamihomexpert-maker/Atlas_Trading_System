from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import logging
import threading
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from atlas.logger import get_logger


logger = get_logger()

# One Yahoo call at a time, and a short wait. A hung download must not pin the page.
HISTORY_TIMEOUT_SECONDS = 4
_PAUSE_BASE_SECONDS = 60
_PAUSE_MAX_SECONDS = 300
_RATE_LIMIT_MARKERS = (
    "too many requests",
    "rate limited",
    "rate limit",
    "yfratelimiterror",
)
# Scan, news, and company reads share one Yahoo call. Chart bars do not.
_fetch_lock = threading.Lock()
_chart_locks_guard = threading.Lock()
_chart_locks = {}
_lane_local = threading.local()
_pause_lock = threading.Lock()
_skip_local = threading.local()
_paused_until = None
_pause_note = None
_pause_steps = 0


class _YahooRateLimitLog(logging.Handler):
    """yfinance often logs the rate limit and returns nothing instead of raising."""

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return
        if _is_rate_limit_text(message):
            _pause(message)


def _install_yahoo_rate_limit_log():
    yf_log = logging.getLogger("yfinance")
    if any(isinstance(handler, _YahooRateLimitLog) for handler in yf_log.handlers):
        return
    yf_log.addHandler(_YahooRateLimitLog(level=logging.INFO))


def _is_rate_limit_text(value):
    text = str(value or "").lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _mark_skip(reason):
    reasons = getattr(_skip_local, "reasons", None)
    if reasons is None:
        reasons = []
        _skip_local.reasons = reasons
    if reason:
        reasons.append(reason)


def take_skips():
    """Return and clear skip reasons recorded on this thread."""
    reasons = list(getattr(_skip_local, "reasons", None) or [])
    _skip_local.reasons = []
    return reasons


def _pause(reason, now=None):
    """Hold further Yahoo calls. The note is what the dashboard can show."""
    global _paused_until, _pause_note, _pause_steps
    now = now or datetime.now(timezone.utc)
    timed_out = "timed out" in str(reason or "").lower()
    with _pause_lock:
        if _paused_until is not None and now < _paused_until:
            return
        _pause_steps += 1
        seconds = min(
            _PAUSE_MAX_SECONDS,
            _PAUSE_BASE_SECONDS * (2 ** min(_pause_steps - 1, 4)),
        )
        _paused_until = now + timedelta(seconds=seconds)
        if timed_out:
            _pause_note = "Yahoo history timed out. Requests are paused."
        else:
            _pause_note = "Yahoo rate limited. History requests are paused."
        until = _paused_until
    logger.info(
        "Market-data feed paused for %ss until %s: %s",
        seconds,
        until.isoformat(),
        reason,
    )


def note_feed_failure(error):
    """Pause when the message is a Yahoo rate limit. Other errors stay local."""
    if not _is_rate_limit_text(f"{type(error).__name__} {error}"):
        return False
    _pause(error)
    return True


def note_feed_success():
    global _pause_steps
    if feed_is_paused():
        return
    with _pause_lock:
        _pause_steps = 0


def clear_feed_pause():
    """Drop the pause. Tests use this so one case does not silence the next."""
    global _paused_until, _pause_note, _pause_steps
    with _pause_lock:
        _paused_until = None
        _pause_note = None
        _pause_steps = 0
    take_skips()


def feed_status(now=None):
    """None when calls may proceed. Otherwise a short pause description."""
    now = now or datetime.now(timezone.utc)
    with _pause_lock:
        if _paused_until is None or now >= _paused_until:
            return None
        remaining = max(0, int((_paused_until - now).total_seconds()))
        return {
            "paused": True,
            "until": _paused_until.isoformat(),
            "seconds_remaining": remaining,
            "note": _pause_note,
        }


def feed_is_paused(now=None):
    return feed_status(now) is not None


def pause_note(now=None):
    status = feed_status(now)
    return None if status is None else status["note"]


@contextmanager
def chart_fetch_scope():
    """Fetch chart bars without taking the scan's market-data lock.

    The Yahoo pause still applies. One chart fetch per symbol and interval
    runs at a time; a second one is skipped so the caller can show cached bars.
    """
    previous = getattr(_lane_local, "name", None)
    _lane_local.name = "chart"
    try:
        yield
    finally:
        if previous is None:
            try:
                del _lane_local.name
            except AttributeError:
                pass
        else:
            _lane_local.name = previous


def _yahoo_lane():
    return getattr(_lane_local, "name", "shared")


def _chart_symbol_lock(symbol, interval):
    key = (str(symbol).strip().upper(), str(interval).strip())
    with _chart_locks_guard:
        lock = _chart_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _chart_locks[key] = lock
        return lock


def call_yahoo(action, timeout=None):
    """Run one Yahoo call off the request if it is already known to be blocked.

    Returns ``(value, error)``. A pause, a busy shared feed, a timeout, or a
    rate limit yields no value. The action's own data is returned unchanged.
    Chart-lane calls do not take the scan lock.
    """
    if feed_is_paused():
        _mark_skip("paused")
        return None, RuntimeError(pause_note() or "Yahoo requests are paused.")
    share_lock = _yahoo_lane() != "chart"
    acquired = False
    if share_lock:
        acquired = _fetch_lock.acquire(blocking=False)
        if not acquired:
            _mark_skip("busy")
            return None, RuntimeError("A market-data request is already running.")
    try:
        if feed_is_paused():
            _mark_skip("paused")
            return None, RuntimeError(pause_note() or "Yahoo requests are paused.")
        box = {}

        def run():
            try:
                box["value"] = action()
            except Exception as error:
                box["error"] = error

        worker = threading.Thread(target=run, name="atlas-yahoo", daemon=True)
        worker.start()
        worker.join(timeout if timeout is not None else HISTORY_TIMEOUT_SECONDS)
        if worker.is_alive():
            _pause("Yahoo history timed out.")
            _mark_skip("timeout")
            return None, TimeoutError("Yahoo history timed out.")
        if "error" in box:
            error = box["error"]
            if note_feed_failure(error):
                _mark_skip("rate_limit")
            else:
                _mark_skip("error")
            return None, error
        note_feed_success()
        return box.get("value"), None
    finally:
        if acquired:
            _fetch_lock.release()


_install_yahoo_rate_limit_log()


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
    chart_lock = None
    if _yahoo_lane() == "chart":
        chart_lock = _chart_symbol_lock(normalized_symbol, interval)
        if not chart_lock.acquire(blocking=False):
            _mark_skip("chart_busy")
            return None
    try:
        return _read_history(normalized_symbol, period, interval)
    finally:
        if chart_lock is not None:
            chart_lock.release()


def _read_history(symbol, period, interval):
    history, error = call_yahoo(
        lambda: yf.Ticker(symbol).history(period=period, interval=interval)
    )
    if error is not None:
        if not feed_is_paused():
            logger.info("%s history fetch skipped: %s", symbol, error)
        return None
    if history is None or getattr(history, "empty", True):
        if feed_is_paused():
            _mark_skip("rate_limit")
            logger.info(
                "%s history fetch skipped: %s",
                symbol,
                pause_note() or "Yahoo returned no bars while rate limited.",
            )
        else:
            _mark_skip("empty")
        return None
    try:
        normalized = normalize_ohlcv(history)
    except (TypeError, ValueError) as error:
        logger.info("%s history dropped: %s", symbol, error)
        _mark_skip("error")
        return None
    return normalized


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
