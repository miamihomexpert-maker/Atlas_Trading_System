from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from atlas.broker import PaperBroker
from atlas.execution import execute_paper_signal
from atlas.market_data import bar_as_of, freshest_observation
from atlas.scanner import scan_timeframes
from atlas.session import _latest_quote


NEW_YORK = ZoneInfo("America/New_York")


def _buy_signal(market_timestamp):
    return {
        "instrument": "XYZ",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "profit_target": 120.0,
        "market_timestamp": market_timestamp,
    }


def test_forming_hourly_bar_is_fresh_and_its_open_label_is_not():
    now = datetime.now(timezone.utc)
    opened = now - timedelta(minutes=42)
    as_of = bar_as_of(opened, "1h", now)

    assert abs((as_of - now).total_seconds()) < 1
    assert (now - opened).total_seconds() > 900

    broker = PaperBroker(initial_cash=10_000)
    with pytest.raises(PermissionError, match="stale"):
        execute_paper_signal(
            _buy_signal(opened.isoformat()),
            broker,
            "open-label",
            approved=True,
            max_data_age_seconds=900,
        )
    order = execute_paper_signal(
        _buy_signal(as_of.isoformat()),
        broker,
        "coverage-time",
        approved=True,
        max_data_age_seconds=900,
    )
    assert order["status"] == "FILLED"
    assert order["quantity"] <= 10


def test_closed_bar_and_yesterday_session_stay_stale():
    now = datetime(2026, 11, 5, 19, 12, tzinfo=timezone.utc)
    closed_open = now - timedelta(minutes=100)  # ended 40 minutes ago
    closed_as_of = bar_as_of(closed_open, "1h", now)
    assert (now - closed_as_of).total_seconds() == pytest.approx(40 * 60, abs=1)

    yesterday = datetime(2026, 11, 4, 0, 0, tzinfo=NEW_YORK)
    daily_as_of = bar_as_of(yesterday, "1d", now)
    assert daily_as_of.astimezone(NEW_YORK).hour == 16
    assert (now - daily_as_of).total_seconds() > 900

    just_closed = bar_as_of(now - timedelta(minutes=70), "1h", now)
    assert (now - just_closed).total_seconds() == pytest.approx(10 * 60, abs=1)


def _indexed(end, step):
    index = pd.DatetimeIndex([end - timedelta(hours=39 - i) for i in range(40)])
    close = [100 + i * step for i in range(40)]
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Volume": [1_000_000] * 40,
        },
        index=index,
    )


def test_entry_path_uses_the_newest_bar_across_timeframes_not_the_daily_open():
    now = datetime.now(timezone.utc)
    hourly_open = now - timedelta(minutes=40)
    hourly = _indexed(hourly_open, step=1)
    midnight = now.astimezone(NEW_YORK).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    daily = _indexed(midnight, step=1)
    # Daily is first on purpose. The old path copied that frame's midnight stamp.
    histories = {"1d": daily, "1h": hourly}
    scanned = scan_timeframes("XYZ", histories, minimum_agreement=2)
    recommendation = scanned["recommendation"]
    assert recommendation is not None
    stamped = datetime.fromisoformat(recommendation["market_timestamp"])
    age = (datetime.now(timezone.utc) - stamped.astimezone(timezone.utc)).total_seconds()
    assert age < 5
    assert abs((stamped.astimezone(timezone.utc) - midnight.astimezone(timezone.utc)).total_seconds()) > 3600

    quote_time, price = _latest_quote(histories)
    quote_at = datetime.fromisoformat(quote_time)
    assert abs((datetime.now(timezone.utc) - quote_at.astimezone(timezone.utc)).total_seconds()) < 5
    assert price == pytest.approx(float(hourly["Close"].iloc[-1]))

    dead_open = now - timedelta(hours=3)
    observed = freshest_observation({"1h": _indexed(dead_open, step=-1)}, now)
    assert (now - observed["as_of"]).total_seconds() == pytest.approx(2 * 3600, abs=2)
