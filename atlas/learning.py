"""Public, auditable context for one symbol.

Outcomes come from this paper book's stored scans and a later market bar.
Holder and insider rows come only from fields the public feed returned.
Nothing here changes a WAIT into a BUY or a SELL, and nothing here submits
an order. Filings can be delayed. This is not a forecast of profit.
"""

from datetime import datetime, timezone
import os

import pandas as pd


DISCLAIMER = (
    "This is disclosed public activity and later market bars. "
    "It does not read anyone's mind, it does not use private information, "
    "and it is not a forecast of profit. Form 4 and 13F filings can be delayed. "
    "A 13F is a quarterly holder report, not a live tape."
)

_INSIDER_FIELDS = {
    "insider": ("insider", "insider name", "name"),
    "role": ("position", "relationship", "title"),
    "transaction": ("transaction", "transaction type", "text"),
    "shares": ("shares", "shares traded"),
    "value": ("value",),
    "date": ("start date", "date", "transaction date"),
    "url": ("url", "link"),
}
_HOLDER_FIELDS = {
    "holder": ("holder", "holder name", "name"),
    "shares": ("shares", "shares held"),
    "date": ("date reported", "date"),
    "value": ("value",),
    "percent": ("% out", "percent", "pct out", "% of shares"),
}


def behavior_enabled():
    """Public holder requests are on unless ATLAS_BEHAVIOR is false."""
    raw = os.getenv("ATLAS_BEHAVIOR")
    if raw is None:
        return True
    value = raw.strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise ValueError("ATLAS_BEHAVIOR must be a boolean value.")


def resolve_behavior_loader(loader=None):
    if loader is not None:
        return loader
    if not behavior_enabled():
        return None
    return load_public_behavior


def learning_bias_for(decision, summary):
    """Tie-break only. A wait, a missing count, and a non-decision contribute 0."""
    name = str(decision or "").upper()
    if name not in {"BUY", "SELL"} or not isinstance(summary, dict):
        return 0
    if name == "BUY":
        checked = summary.get("buy_checked")
        aligned = summary.get("buy_followed_by_rise")
    else:
        checked = summary.get("sell_checked")
        aligned = summary.get("sell_followed_by_fall")
    if isinstance(checked, bool) or isinstance(aligned, bool):
        return 0
    if not isinstance(checked, int) or not isinstance(aligned, int) or checked <= 0:
        return 0
    return aligned - (checked - aligned)


def outcome_from_observation(observation, bars):
    """Grade one stored scan against the next returned bar. None when there is no later bar."""
    from atlas.review import _first_after, _number

    if not isinstance(observation, dict):
        return None
    decision = str(observation.get("decision") or "").upper()
    if decision not in {"BUY", "SELL", "WAIT"}:
        return None
    later = _first_after(bars, observation.get("bar_time"))
    price = _number(observation.get("price"))
    if later is None or price is None or price <= 0:
        return None
    close = later.get("close")
    if close is None:
        return None
    if close > price:
        move = "up"
    elif close < price:
        move = "down"
    else:
        move = "flat"
    aligned = None
    if decision == "BUY":
        aligned = move == "up"
    elif decision == "SELL":
        aligned = move == "down"
    later_time = later.get("time")
    source_key = f"{observation.get('bar_time')}|{decision}|{later_time}"
    summary = (
        f"Stored {decision} at {price} on {observation.get('bar_time')}. "
        f"The next returned close was {close} ({move})."
    )
    if decision == "WAIT":
        summary += " The wait was left as a wait."
        effect = "none"
    else:
        summary += (
            " This count can only reorder cards that already share this decision and confidence."
        )
        effect = "ranking_tie_break"
    return {
        "source": "later_market_bar",
        "source_key": source_key,
        "summary": summary,
        "effect": effect,
        "detail": {
            "decision": decision,
            "price": price,
            "later_close": close,
            "later_time": later_time,
            "move": move,
            "aligned": aligned,
            "timeframe": observation.get("timeframe"),
        },
    }


def remember_outcomes(database, symbol, histories):
    """Persist grades for stored scans that now have a later bar."""
    from atlas.review import choose_bars

    if database is None:
        return []
    _timeframe, bars = choose_bars(histories)
    if not bars:
        return []
    symbol = str(symbol).strip().upper()
    stored = []
    for observation in database.list_scan_observations():
        if str(observation.get("symbol") or "").upper() != symbol:
            continue
        record = outcome_from_observation(observation, bars)
        if record is None:
            continue
        inserted = database.record_learning_event(
            symbol,
            record["source"],
            record["source_key"],
            record["summary"],
            record["detail"],
            record["effect"],
        )
        if inserted:
            stored.append(record)
    return stored


def summarize_outcomes(events):
    """Counts from stored later-bar grades. No expected return."""
    buy_checked = buy_up = sell_checked = sell_down = wait_checked = 0
    for event in events or []:
        if event.get("source") != "later_market_bar":
            continue
        detail = event.get("detail") or {}
        decision = str(detail.get("decision") or "").upper()
        if decision == "BUY":
            buy_checked += 1
            if detail.get("aligned") is True:
                buy_up += 1
        elif decision == "SELL":
            sell_checked += 1
            if detail.get("aligned") is True:
                sell_down += 1
        elif decision == "WAIT":
            wait_checked += 1
    if buy_checked or sell_checked or wait_checked:
        note = (
            f"Later bars checked {buy_checked} stored buy scan"
            f"{'' if buy_checked == 1 else 's'}"
            f" ({buy_up} followed by a higher close), "
            f"{sell_checked} stored sell scan"
            f"{'' if sell_checked == 1 else 's'}"
            f" ({sell_down} followed by a lower close), "
            f"and {wait_checked} wait"
            f"{'' if wait_checked == 1 else 's'} left as a wait. "
            "These counts do not change a wait into a buy or a sell."
        )
    else:
        note = "No stored scan has a later bar yet, so there is no outcome count."
    return {
        "buy_checked": buy_checked,
        "buy_followed_by_rise": buy_up,
        "sell_checked": sell_checked,
        "sell_followed_by_fall": sell_down,
        "wait_checked": wait_checked,
        "note": note,
    }


def stamp_learning_bias(cards, events_by_symbol):
    """Attach a tie-break. Does not change decision, buyable, or a recommendation."""
    stamped = []
    for card in cards or []:
        item = dict(card)
        events = (events_by_symbol or {}).get(item.get("symbol")) or []
        summary = summarize_outcomes(events)
        item["learning_bias"] = learning_bias_for(item.get("decision"), summary)
        item["learning_note"] = summary["note"]
        stamped.append(item)
    return stamped


def load_public_behavior(symbol, ticker_factory=None, now=None):
    """Read insider transactions and institutional holders. Empty stays empty."""
    normalized = str(symbol).strip().upper()
    fetched_at = _stamp(now)
    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    try:
        ticker = ticker_factory(normalized)
    except Exception as error:
        return _failed(normalized, str(error), fetched_at)
    insider_error = holder_error = None
    try:
        insider_frame = _read_frame(ticker, "insider_transactions")
    except Exception as error:
        insider_frame = None
        insider_error = str(error)
    try:
        holder_frame = _read_frame(ticker, "institutional_holders")
    except Exception as error:
        holder_frame = None
        holder_error = str(error)
    insiders = _rows_from_frame(insider_frame, _INSIDER_FIELDS, limit=6)
    holders = _rows_from_frame(holder_frame, _HOLDER_FIELDS, limit=6)
    errors = [item for item in (insider_error, holder_error) if item]
    if errors and not insiders and not holders:
        status = "FETCH_FAILED"
    elif insiders or holders:
        status = "FETCHED"
    else:
        status = "NO_ROWS"
    return {
        "symbol": normalized,
        "status": status,
        "error": "; ".join(errors) or None,
        "fetched_at": fetched_at,
        "source": "Yahoo Finance public holder data via yfinance",
        "insiders": insiders,
        "holders": holders,
    }


def remember_public(database, symbol, payload):
    """Store returned public rows. A repeated row is not audited again."""
    if database is None or not isinstance(payload, dict):
        return []
    symbol = str(symbol).strip().upper()
    stored = []
    for kind, source in (
        ("insiders", "yahoo_insider_transaction"),
        ("holders", "yahoo_institutional_holder"),
    ):
        for row in payload.get(kind) or []:
            if not isinstance(row, dict) or not row:
                continue
            source_key = _row_key(row)
            summary = _row_summary(kind, row)
            inserted = database.record_learning_event(
                symbol,
                source,
                source_key,
                summary,
                {
                    "row": row,
                    "feed_status": payload.get("status"),
                    "feed_source": payload.get("source"),
                    "fetched_at": payload.get("fetched_at"),
                },
                "context_only",
            )
            if inserted:
                stored.append(source_key)
    return stored


def describe_behavior(symbol, events, status="NOT_LOADED", error=None, fetched_at=None):
    """Lines for the dashboard. Missing fields stay missing."""
    symbol = str(symbol or "").strip().upper()
    events = list(events or [])
    summary = summarize_outcomes(events)
    insiders = [
        event for event in events if event.get("source") == "yahoo_insider_transaction"
    ]
    holders = [
        event for event in events if event.get("source") == "yahoo_institutional_holder"
    ]
    lines = [
        DISCLAIMER,
        summary["note"],
        "Public filings are context only. They do not change a wait into a buy or a sell, and they do not submit an order.",
        "When two cards already share a decision and a confidence, later-bar counts can change their order. A wait stays a wait.",
    ]
    if status == "NOT_LOADED":
        lines.append(
            "The public holder feed is off. Stored scan outcomes from this paper book are still shown."
        )
    elif status == "FETCH_FAILED":
        lines.append(
            "The latest public holder read failed"
            + (f": {error}." if error else ".")
            + " Rows below are only ones stored from an earlier read."
        )
    elif status == "NO_ROWS":
        lines.append("The public feed returned no insider transactions and no institutional holders.")
    elif status == "FETCHED":
        lines.append("The public feed returned the rows listed below. Fields it left out are not shown.")
    return {
        "symbol": symbol,
        "status": status,
        "error": error,
        "fetched_at": fetched_at,
        "disclaimer": DISCLAIMER,
        "note": summary["note"],
        "lines": lines,
        "outcomes": summary,
        "insiders": [_public_row(event) for event in insiders],
        "holders": [_public_row(event) for event in holders],
        "sec_form4_search": _sec_search(symbol, "4"),
        "sec_13f_search": _sec_search(symbol, "13F-HR"),
    }


def _public_row(event):
    detail = event.get("detail") or {}
    row = dict(detail.get("row") or {})
    row["learned_at"] = event.get("learned_at")
    row["source"] = event.get("source")
    row["effect"] = event.get("effect")
    row["summary"] = event.get("summary")
    return row


def _failed(symbol, error, fetched_at):
    return {
        "symbol": symbol,
        "status": "FETCH_FAILED",
        "error": error,
        "fetched_at": fetched_at,
        "source": "Yahoo Finance public holder data via yfinance",
        "insiders": [],
        "holders": [],
    }


def _read_frame(ticker, name):
    value = getattr(ticker, name)
    if callable(value):
        value = value()
    if value is None:
        return None
    if not isinstance(value, pd.DataFrame):
        return None
    return value


def _rows_from_frame(frame, fields, limit):
    if frame is None or getattr(frame, "empty", True):
        return []
    columns = {str(column).strip().lower(): column for column in frame.columns}
    rows = []
    for _, raw in frame.head(limit).iterrows():
        item = {}
        for key, aliases in fields.items():
            for alias in aliases:
                if alias not in columns:
                    continue
                value = _plain(raw[columns[alias]])
                if value is None:
                    continue
                item[key] = value
                break
        if item:
            rows.append(item)
    return rows


def _plain(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    return text


def _row_key(row):
    parts = []
    for key in ("insider", "holder", "role", "transaction", "date", "shares", "value"):
        if key in row and row[key] is not None:
            parts.append(f"{key}={row[key]}")
    return "|".join(parts) or "row-without-fields"


def _row_summary(kind, row):
    if kind == "insiders":
        who = row.get("insider") or "A reported person"
        role = f" ({row['role']})" if row.get("role") else ""
        action = row.get("transaction") or "a reported transaction"
        when = f" on {row['date']}" if row.get("date") else ""
        shares = f", {row['shares']} shares" if row.get("shares") is not None else ""
        return f"{who}{role}: {action}{when}{shares}. The feed did not include a motive."
    holder = row.get("holder") or "A reported holder"
    when = f" reported {row['date']}" if row.get("date") else "reported without a date"
    shares = f", {row['shares']} shares" if row.get("shares") is not None else ""
    return f"{holder}, {when}{shares}."


def _sec_search(symbol, form):
    if not symbol:
        return None
    return (
        "https://www.sec.gov/cgi-bin/browse-edgar?"
        f"action=getcompany&CIK={symbol}&type={form}&owner=include&count=10"
    )


def _stamp(now):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()
