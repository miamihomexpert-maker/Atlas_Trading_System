import pandas as pd
import yfinance as yf


OHLCV_COLUMNS = ("Open", "High", "Low", "Close", "Volume")
TIMEFRAME_RULES = {
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "1d": "1D",
    "1w": "1W",
}


def normalize_ohlcv(history):
    """Return sorted, de-duplicated OHLCV data or raise on invalid bars."""
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
    if data.isna().any().any() or data.isin(
        [float("inf"), float("-inf")]
    ).any().any():
        raise ValueError("Market history contains non-numeric or non-finite OHLCV.")
    if (
        (data["Close"] <= 0).any()
        or (data["Volume"] < 0).any()
        or (data["High"] < data["Low"]).any()
        or (data["High"] < data["Open"]).any()
        or (data["High"] < data["Close"]).any()
        or (data["Low"] > data["Open"]).any()
        or (data["Low"] > data["Close"]).any()
    ):
        raise ValueError("Market history contains invalid OHLCV bar values.")

    data = data.sort_index()
    return data.loc[~data.index.duplicated(keep="last")]


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


def _fetch_history(symbol, period, interval):
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise ValueError("Instrument symbol is required.")
    history = yf.Ticker(normalized_symbol).history(period=period, interval=interval)
    if history.empty:
        return None
    return normalize_ohlcv(history)


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
