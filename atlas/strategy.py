import pandas as pd
def get_strategy_status():
    return "STRATEGY ENGINE ONLINE"


def _safe_float(value):
    """Convert a numeric value to a normal Python float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_market(history, technical=None):
    """
    Evaluate market history and return a structured strategy result.

    This stage analyzes:
    - trend
    - momentum
    - volume
    - volatility
    - breakout behavior
    - mean reversion
    - VWAP

    It does not place trades.
    """

    if history is None or history.empty:
        return {
            "status": "NO_DATA",
            "signal": "WAIT",
            "confidence": None,
            "reason": "No market data available.",
        }

    required_columns = {"Open", "High", "Low", "Close", "Volume"}
    missing_columns = required_columns - set(history.columns)

    if missing_columns:
        return {
            "status": "INVALID_DATA",
            "signal": "WAIT",
            "reason": f"Missing columns: {sorted(missing_columns)}",
        }

    if len(history) < 20:
        return {
            "status": "INSUFFICIENT_DATA",
            "signal": "WAIT",
            "reason": "At least 20 bars are required for analysis.",
        }

    data = history.copy()

    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]

    numeric_data = data[list(required_columns)].apply(
        pd.to_numeric,
        errors="coerce",
    )
    has_non_finite = numeric_data.isin(
        [float("inf"), float("-inf")]
    ).any().any()
    if numeric_data.isna().any().any() or has_non_finite:
        return {
            "status": "INVALID_DATA",
            "signal": "WAIT",
            "confidence": None,
            "reason": "OHLCV data contains non-numeric or non-finite values.",
        }

    data[list(required_columns)] = numeric_data
    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]
    if (
        (close <= 0).any()
        or (volume < 0).any()
        or (high < low).any()
        or (high < data["Open"]).any()
        or (high < close).any()
        or (low > data["Open"]).any()
        or (low > close).any()
    ):
        return {
            "status": "INVALID_DATA",
            "signal": "WAIT",
            "confidence": None,
            "reason": "OHLCV values violate price or volume constraints.",
        }

    technical = {} if technical is None else dict(technical)
    technical.setdefault("sma", close.rolling(20).mean())
    technical.setdefault("ema", close.ewm(span=20, adjust=False).mean())
    delta = close.diff()
    average_gain = delta.clip(lower=0).rolling(14).mean()
    average_loss = -delta.clip(upper=0).rolling(14).mean()
    relative_strength = average_gain / average_loss
    technical.setdefault("rsi", 100 - (100 / (1 + relative_strength)))
    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(
        span=26,
        adjust=False,
    ).mean()
    technical.setdefault(
        "macd",
        (
            macd_line,
            macd_line.ewm(span=9, adjust=False).mean(),
            macd_line - macd_line.ewm(span=9, adjust=False).mean(),
        ),
    )

    latest_price = _safe_float(close.iloc[-1])
    latest_volume = _safe_float(volume.iloc[-1])

    # ---------------------------------------------------------
    # TREND
    # ---------------------------------------------------------
    sma_5 = close.rolling(5).mean()
    sma_20 = close.rolling(20).mean()

    latest_sma_5 = _safe_float(sma_5.iloc[-1])
    latest_sma_20 = None

    if technical is not None and "sma" in technical:
        latest_sma_20 = _safe_float(technical["sma"].iloc[-1])

    if latest_sma_20 is None:
        latest_sma_20 = _safe_float(sma_20.iloc[-1])

    latest_ema_20 = None

    if technical is not None and "ema" in technical:
        latest_ema_20 = _safe_float(technical["ema"].iloc[-1])    

    if latest_sma_5 > latest_sma_20 and (
        latest_ema_20 is None or latest_price > latest_ema_20
    ):
        trend = "BULLISH"
    elif latest_sma_5 < latest_sma_20 and (
        latest_ema_20 is None or latest_price < latest_ema_20
    ):
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"


    # ---------------------------------------------------------
    # MOMENTUM
    # ---------------------------------------------------------
    previous_close = _safe_float(close.iloc[-6])

    if previous_close and previous_close != 0:
        momentum_pct = ((latest_price / previous_close) - 1) * 100
    else:
        momentum_pct = 0.0

    if momentum_pct > 1.0:
        momentum = "STRONG_UP"
    elif momentum_pct > 0:
        momentum = "UP"
    elif momentum_pct < -1.0:
        momentum = "STRONG_DOWN"
    elif momentum_pct < 0:
        momentum = "DOWN"
    else:
        momentum = "FLAT"

    # ---------------------------------------------------------
    # VOLUME
    # ---------------------------------------------------------
    avg_volume_20 = _safe_float(volume.rolling(20).mean().iloc[-1])

    if avg_volume_20 and avg_volume_20 > 0:
        volume_ratio = latest_volume / avg_volume_20
    else:
        volume_ratio = 0.0

    if volume_ratio >= 1.5:
        volume_state = "HIGH"
    elif volume_ratio >= 0.75:
        volume_state = "NORMAL"
    else:
        volume_state = "LOW"

    # ---------------------------------------------------------
    # VOLATILITY
    # ---------------------------------------------------------
    returns = close.pct_change()
    volatility_value = _safe_float(returns.tail(20).std())

    if volatility_value is None:
        volatility_value = 0.0

    if volatility_value >= 0.03:
        volatility = "HIGH"
    elif volatility_value >= 0.015:
        volatility = "MODERATE"
    else:
        volatility = "LOW"

    # ---------------------------------------------------------
    # BREAKOUT
    # ---------------------------------------------------------
    previous_20_high = _safe_float(high.iloc[:-1].tail(20).max())
    previous_20_low = _safe_float(low.iloc[:-1].tail(20).min())

    if latest_price > previous_20_high:
        breakout = "UPSIDE_BREAKOUT"
    elif latest_price < previous_20_low:
        breakout = "DOWNSIDE_BREAKOUT"
    else:
        breakout = "NONE"

    # ---------------------------------------------------------
    # MEAN REVERSION
    # ---------------------------------------------------------
    mean_20 = latest_sma_20
    std_20 = _safe_float(close.rolling(20).std().iloc[-1])

    if std_20 and std_20 > 0:
        z_score = (latest_price - mean_20) / std_20
    else:
        z_score = 0.0

    if z_score >= 2.0:
        mean_reversion = "OVEREXTENDED_HIGH"
    elif z_score <= -2.0:
        mean_reversion = "OVEREXTENDED_LOW"
    else:
        mean_reversion = "NORMAL"

    typical_price = (high + low + close) / 3
    session_dates = (
        data.index.date
        if isinstance(data.index, pd.DatetimeIndex)
        else [0] * len(data)
    )
    cumulative_volume = volume.groupby(session_dates).cumsum()
    cumulative_price_volume = (
        (typical_price * volume)
        .groupby(session_dates)
        .cumsum()
    )
    vwap_series = (
        cumulative_price_volume /
        cumulative_volume.replace(0, float("nan"))
    )
    latest_vwap = _safe_float(vwap_series.iloc[-1])

    if latest_vwap is None:
        latest_vwap = latest_price

    if latest_price > latest_vwap:
        vwap_state = "ABOVE_VWAP"
    elif latest_price < latest_vwap:
        vwap_state = "BELOW_VWAP"
    else:
        vwap_state = "AT_VWAP"
    macd_state = "NEUTRAL"
    latest_macd = None
    latest_macd_signal = None

    if technical is not None and "macd" in technical:
        macd_series, macd_signal_series, macd_histogram = technical["macd"]

        latest_macd = _safe_float(macd_series.iloc[-1])
        latest_macd_signal = _safe_float(macd_signal_series.iloc[-1])

    if latest_macd is not None and latest_macd_signal is not None:
        if latest_macd > latest_macd_signal:
            macd_state = "BULLISH"
        elif latest_macd < latest_macd_signal:
            macd_state = "BEARISH"
    rsi_state = "NEUTRAL"
    latest_rsi = None

    if technical is not None and "rsi" in technical:
        rsi_series = technical["rsi"]
        latest_rsi = _safe_float(rsi_series.iloc[-1])

        if latest_rsi is not None:
            if latest_rsi < 30:
                rsi_state = "BULLISH"
            elif latest_rsi > 70:
                rsi_state = "BEARISH"



        # ---------------------------------------------------------
        # FIRST-STAGE SIGNAL SCORING
        # ---------------------------------------------------------
        bullish_score = 0
        bearish_score = 0

        if trend == "BULLISH":
            bullish_score += 1
        elif trend == "BEARISH":
            bearish_score += 1

        if momentum in ("UP", "STRONG_UP"):
            bullish_score += 1
        elif momentum in ("DOWN", "STRONG_DOWN"):
            bearish_score += 1
        if rsi_state == "BULLISH":
            bullish_score += 1
        elif rsi_state == "BEARISH":
            bearish_score += 1
        if macd_state == "BULLISH":
            bullish_score += 1
        elif macd_state == "BEARISH":
            bearish_score += 1

        if vwap_state == "ABOVE_VWAP":
            bullish_score += 1
        elif vwap_state == "BELOW_VWAP":
            bearish_score += 1

        if breakout == "UPSIDE_BREAKOUT":
            bullish_score += 2
        elif breakout == "DOWNSIDE_BREAKOUT":
            bearish_score += 2
        if volume_state == "HIGH":
            if breakout == "UPSIDE_BREAKOUT":
                bullish_score += 1
            elif breakout == "DOWNSIDE_BREAKOUT":
                bearish_score += 1
        if breakout == "NONE":
            if mean_reversion == "OVEREXTENDED_LOW":
                bullish_score += 1
            elif mean_reversion == "OVEREXTENDED_HIGH":
                bearish_score += 1

        # Require agreement before producing a directional signal.
        required_score = 5 if volume_state == "LOW" else 4

        if bullish_score >= required_score and bullish_score > bearish_score:
            signal = "BUY"
        elif bearish_score >= required_score and bearish_score > bullish_score:
            signal = "SELL"
        else:
            signal = "WAIT"

        confidence = max(bullish_score, bearish_score) / 9
        confidence = min(confidence, 1.0)
        if volatility == "HIGH":
            confidence *= 0.8
        elif volatility == "MODERATE":
            confidence *= 0.9

        return {
            "status": "READY",
            "signal": signal,
            "price": latest_price,
            "volume": latest_volume,
            "market_timestamp": (
                data.index[-1].isoformat()
                if isinstance(data.index[-1], pd.Timestamp)
                else None
            ),
            "observations": {
                "trend": trend,
                "momentum": momentum,
                "momentum_pct": round(momentum_pct, 4),
                "volume": volume_state,
                "volume_ratio": round(volume_ratio, 4),
                "volatility": volatility,
                "volatility_value": round(volatility_value, 6),
                "breakout": breakout,
                "mean_reversion": mean_reversion,
                "z_score": round(z_score, 4),
                "vwap": vwap_state,
                "vwap_price": round(latest_vwap, 4),
                "rsi": rsi_state,
                "macd": macd_state,
                "bullish_score": bullish_score,
                "bearish_score": bearish_score,
                "news_events": "NOT_CONNECTED",
            },
            "confidence": round(confidence, 2),
            "reason": "Market analysis completed.",
        }