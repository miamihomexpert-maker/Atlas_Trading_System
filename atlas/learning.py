"""Public, auditable context for one symbol.

Outcomes come from this paper book's stored scans and a later market bar.
Holder and insider rows come only from fields the public feed returned.
Nothing here changes a WAIT into a BUY or a SELL, and nothing here submits
an order. Filings can be delayed. This is not a forecast of profit.
"""

from datetime import datetime, timezone
import os
import re

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
    from atlas.market_data import call_yahoo, feed_is_paused, pause_note

    normalized = str(symbol).strip().upper()
    fetched_at = _stamp(now)
    if feed_is_paused():
        return _failed(normalized, pause_note() or "Yahoo requests are paused.", fetched_at)
    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    ticker, error = call_yahoo(lambda: ticker_factory(normalized))
    if error is not None:
        return _failed(normalized, str(error), fetched_at)
    insider_error = holder_error = None
    insider_frame, error = call_yahoo(lambda: _read_frame(ticker, "insider_transactions"))
    if error is not None:
        insider_frame = None
        insider_error = str(error)
    holder_frame, error = call_yahoo(lambda: _read_frame(ticker, "institutional_holders"))
    if error is not None:
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


PREDICTION_MIN_GRADED = 8
PREDICTION_DISCLAIMER = (
    "This is a likelihood from public, often delayed data, not a forecast of profit. "
    "Filings can be late or incomplete. It does not read anyone's mind, and a missing motive stays missing. "
    "A paper order from this tab needs at least 8 graded predictions for this symbol, "
    "more of them followed by the predicted direction than not, and the later half more accurate than the earlier half. "
    "A thin or flat record leaves the scan as a wait."
)
_BUY_ACTION = re.compile(r"\b(purchase|purchased|buy|bought|acquisition)\b", re.I)
_SELL_ACTION = re.compile(r"\b(sale|sold|sell|selling)\b", re.I)


def classify_disclosed_action(text):
    """Return buy, sell, or None. Ambiguous or unlabeled text is not guessed."""
    if not isinstance(text, str) or not text.strip():
        return None
    buy = _BUY_ACTION.search(text) is not None
    sell = _SELL_ACTION.search(text) is not None
    if buy == sell:
        return None
    return "buy" if buy else "sell"


def ohlc_bars(histories):
    """Printed open-high-low-close rows from the same timeframe the review uses."""
    from atlas.review import choose_bars

    name, _closes = choose_bars(histories)
    if not name or not isinstance(histories, dict):
        return []
    frame = histories.get(name)
    if frame is None or getattr(frame, "empty", True):
        return []
    bars = []
    for stamp, row in frame.iterrows():
        try:
            high = float(row["High"])
            low = float(row["Low"])
            close = float(row["Close"])
        except (TypeError, ValueError, KeyError):
            continue
        if min(high, low, close) <= 0:
            continue
        volume = None
        if "Volume" in getattr(frame, "columns", []):
            try:
                volume = float(row["Volume"])
            except (TypeError, ValueError):
                volume = None
            if volume is not None and volume < 0:
                volume = None
        bars.append({
            "time": stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp),
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
    return bars


def printed_levels(bars):
    """Entry, stop, and target taken only from prices that already printed."""
    if not bars:
        return {"entry": None, "stop": None, "target": None, "time": None}
    last = bars[-1]
    entry = last["close"]
    lows = [bar["low"] for bar in bars if bar["low"] < entry]
    highs = [bar["high"] for bar in bars if bar["high"] > entry]
    return {
        "entry": entry,
        "stop": min(lows) if lows else None,
        "target": max(highs) if highs else None,
        "time": last["time"],
    }


def _when(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _later_bar_moves(events):
    moves = []
    for event in events or []:
        if event.get("source") != "later_market_bar":
            continue
        detail = event.get("detail") or {}
        move = detail.get("move")
        if move not in {"up", "down", "flat"}:
            continue
        moves.append({
            "move": move,
            "time": _when(detail.get("later_time") or detail.get("bar_time")),
            "summary": event.get("summary"),
            "learned_at": event.get("learned_at"),
            "source": event.get("source"),
            "effect": event.get("effect"),
        })
    return moves


def _insider_rows(events):
    rows = []
    for event in events or []:
        if event.get("source") != "yahoo_insider_transaction":
            continue
        row = dict((event.get("detail") or {}).get("row") or {})
        row["learned_at"] = event.get("learned_at")
        row["source"] = event.get("source")
        row["effect"] = event.get("effect")
        row["summary"] = event.get("summary")
        row["action"] = classify_disclosed_action(row.get("transaction"))
        rows.append(row)
    return rows


def _holder_rows(events):
    rows = []
    for event in events or []:
        if event.get("source") != "yahoo_institutional_holder":
            continue
        row = dict((event.get("detail") or {}).get("row") or {})
        row["learned_at"] = event.get("learned_at")
        row["source"] = event.get("source")
        row["summary"] = event.get("summary")
        row["effect"] = event.get("effect")
        rows.append(row)
    return rows


def _count_pattern(rows, moves, action, move):
    """Disclosed actions dated after a stored bar that already moved that way."""
    cases = []
    for row in rows:
        if row.get("action") != action:
            continue
        acted = _when(row.get("date"))
        if acted is None:
            continue
        prior = [item for item in moves if item["time"] is not None and item["time"] < acted and item["move"] == move]
        if prior:
            cases.append(row)
    return cases


def _share_count(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.replace(",", "").strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _format_count(value):
    if value is None:
        return None
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def activity_share(insiders):
    """Percent of labeled shares. Rows without a share count are left out."""
    buy_shares = 0.0
    sell_shares = 0.0
    missing = 0
    labeled = 0
    for row in insiders or []:
        if row.get("action") not in {"buy", "sell"}:
            continue
        labeled += 1
        shares = _share_count(row.get("shares"))
        if shares is None:
            missing += 1
            continue
        if row["action"] == "buy":
            buy_shares += shares
        else:
            sell_shares += shares
    total = buy_shares + sell_shares
    if total <= 0:
        return {
            "buy_shares": None,
            "sell_shares": None,
            "total_shares": None,
            "buy_percent": None,
            "sell_percent": None,
            "missing_share_counts": missing,
            "note": "The feed did not include share counts on labeled purchases or sales, so no percentage is computed.",
        }
    sell_percent = int(round(100 * sell_shares / total))
    buy_percent = int(round(100 * buy_shares / total))
    note = (
        f"Of {_format_count(total)} labeled shares the feed returned, "
        f"{_format_count(sell_shares)} were sales and {_format_count(buy_shares)} were purchases. "
        f"Sales are {sell_percent} percent of that labeled share count, and purchases are {buy_percent} percent."
    )
    if missing:
        note += (
            f" {missing} labeled "
            f"{'row' if missing == 1 else 'rows'} had no share count and "
            f"{'is' if missing == 1 else 'are'} left out of the percentage."
        )
    return {
        "buy_shares": buy_shares,
        "sell_shares": sell_shares,
        "total_shares": total,
        "buy_percent": buy_percent,
        "sell_percent": sell_percent,
        "missing_share_counts": missing,
        "labeled_rows": labeled,
        "note": note,
    }


def volume_comparison(bars):
    """Compare the latest volume with the median of earlier bars. No volume, no percentage."""
    volumes = []
    for bar in bars or []:
        volume = _share_count(bar.get("volume"))
        if volume is None:
            continue
        volumes.append(volume)
    if len(volumes) < 6:
        return {
            "unusual": False,
            "percent_of_median": None,
            "note": "Not enough volume bars were returned to compare the latest bar with a typical bar.",
        }
    last = volumes[-1]
    prior = sorted(volumes[:-1])
    median = prior[len(prior) // 2]
    if median <= 0:
        return {
            "unusual": False,
            "percent_of_median": None,
            "note": "The earlier bars did not include a positive volume, so no volume comparison is computed.",
        }
    percent = int(round(100 * last / median))
    unusual = last >= (2 * median)
    note = (
        f"The latest bar volume is {_format_count(last)} and the median of the earlier bars is {_format_count(median)}. "
        f"The latest bar is {percent} percent of that median."
    )
    if unusual:
        note += " That is at least twice the median, so the volume is marked unusual."
    else:
        note += " That is not at least twice the median, so it is not marked unusual."
    return {
        "unusual": unusual,
        "last": last,
        "median": median,
        "percent_of_median": percent,
        "note": note,
    }


def build_investor_board(events, bars=None):
    """Dated public movements, what a later bar showed, and how each lines up with a stored lean."""
    leans = []
    for event in events or []:
        if event.get("source") != "prediction_lean":
            continue
        detail = event.get("detail") or {}
        when = _when(detail.get("reference_time"))
        if detail.get("direction") not in {"up", "down"} or when is None:
            continue
        leans.append({
            "direction": detail["direction"],
            "time": when,
            "source_key": event.get("source_key"),
        })
    grouped = {}
    for movement in _investor_movements(events):
        key = (movement["kind"], movement["name"])
        grouped.setdefault(key, []).append(movement)
    investors = []
    for (kind, name), movements in grouped.items():
        prepared = []
        for movement in movements:
            comparison, comparison_note, lean_direction = _compare_movement(movement, leans)
            outcome = _movement_outcome(movement, bars or [], lean_direction)
            in_track = comparison in {"lined_up", "differed"} and outcome is not None
            track_matched = bool(
                in_track
                and comparison == "lined_up"
                and outcome.get("aligned_with_prediction") is True
            )
            if outcome is None and comparison in {"lined_up", "differed"}:
                comparison_note += " No later bar is stored yet, so this comparison is not in the track record."
            public = {key: value for key, value in movement.items() if key != "time"}
            prepared.append({
                **public,
                "comparison": comparison,
                "comparison_note": comparison_note,
                "lean_direction": lean_direction,
                "outcome": outcome,
                "in_track_record": in_track,
                "track_matched": track_matched,
                "motive": None,
                "motive_note": "The feed did not include a motive.",
                "filings_note": "Filings can be delayed or incomplete.",
            })
        prepared.sort(key=lambda item: item["date"] or "")
        with_later = [item for item in prepared if item.get("outcome")]
        learned = (
            f"{len(prepared)} dated public movement{'s' if len(prepared) != 1 else ''} "
            f"are stored. {len(with_later)} have a later bar. "
            "Filings can be delayed or incomplete."
        )
        investors.append({
            "name": name,
            "kind": kind,
            "movements": prepared,
            "learned": learned,
        })
    investors.sort(key=lambda item: item["name"])
    if investors:
        note = "Each investor below was named by the public feed. No investor was added."
    else:
        note = "No major investor with a name and a date was returned. None is filled in."
    return {"investors": investors, "note": note, "filings_note": "Filings can be delayed or incomplete."}


def _investor_movements(events):
    movements = []
    for row in _insider_rows(events):
        name = row.get("insider")
        when = _when(row.get("date"))
        action = row.get("action")
        if not name or when is None or action not in {"buy", "sell"}:
            continue
        movements.append({
            "name": name,
            "kind": "insider",
            "role": row.get("role"),
            "action": action,
            "action_label": "disclosed purchase" if action == "buy" else "disclosed sale",
            "date": row.get("date"),
            "time": when,
            "shares": _share_count(row.get("shares")),
            "url": row.get("url"),
            "source": row.get("source") or "yahoo_insider_transaction",
            "learned_at": row.get("learned_at"),
            "source_key": _row_key(row),
        })
    for row in _holder_rows(events):
        name = row.get("holder")
        when = _when(row.get("date"))
        shares = _share_count(row.get("shares"))
        if not name or when is None or shares is None:
            continue
        movements.append({
            "name": name,
            "kind": "institution",
            "role": None,
            "action": None,
            "action_label": "reported holding",
            "date": row.get("date"),
            "time": when,
            "shares": shares,
            "url": row.get("url"),
            "source": row.get("source") or "yahoo_institutional_holder",
            "learned_at": row.get("learned_at"),
            "source_key": _row_key({"holder": name, "date": row.get("date"), "shares": shares}),
        })
    return movements


def _compare_movement(movement, leans):
    if movement.get("action") not in {"buy", "sell"}:
        return (
            "not_enough_data",
            "This row is a reported holding, not a disclosed buy or sale, so it is not compared with a lean.",
            None,
        )
    prior = [lean for lean in leans if lean["time"] <= movement["time"]]
    if not prior:
        return (
            "not_enough_data",
            "No stored prediction lean is dated on or before this movement.",
            None,
        )
    lean = max(prior, key=lambda item: item["time"])
    direction = lean["direction"]
    agrees = (
        (movement["action"] == "buy" and direction == "up")
        or (movement["action"] == "sell" and direction == "down")
    )
    word = "higher" if direction == "up" else "lower"
    if agrees:
        return (
            "lined_up",
            f"This {movement['action_label']} lines up with the stored lean toward a {word} close.",
            direction,
        )
    return (
        "differed",
        f"This {movement['action_label']} differs from the stored lean toward a {word} close.",
        direction,
    )


def _movement_outcome(movement, bars, lean_direction):
    later = None
    prior = None
    for bar in bars or []:
        when = _when(bar.get("time"))
        close = bar.get("close")
        if when is None or not isinstance(close, (int, float)) or isinstance(close, bool):
            continue
        if when <= movement["time"]:
            if prior is None or when >= prior[0]:
                prior = (when, close)
            continue
        if later is None or when < later[0]:
            later = (when, bar)
    if later is None or prior is None:
        return None
    reference = prior[1]
    close = later[1]["close"]
    if close > reference:
        move = "up"
    elif close < reference:
        move = "down"
    else:
        move = "flat"
    aligned = lean_direction in {"up", "down"} and move == lean_direction
    return {
        "move": move,
        "later_time": later[1].get("time"),
        "reference_price": reference,
        "later_close": close,
        "aligned_with_prediction": aligned,
        "note": (
            f"The next stored close after this movement was {move}. "
            "Filings can be delayed or incomplete."
        ),
    }


def investor_audit_rows(board):
    """One auditable row per dated movement. Nothing is added for a missing investor."""
    rows = []
    for investor in (board or {}).get("investors") or []:
        for movement in investor.get("movements") or []:
            rows.append({
                "source": "investor_movement",
                "source_key": movement.get("source_key") or investor["name"],
                "effect": "context_only",
                "summary": movement.get("comparison_note") or investor.get("learned"),
                "detail": {
                    "name": investor.get("name"),
                    "kind": investor.get("kind"),
                    "comparison": movement.get("comparison"),
                    "date": movement.get("date"),
                    "action": movement.get("action"),
                    "shares": movement.get("shares"),
                    "in_track_record": movement.get("in_track_record"),
                    "track_matched": movement.get("track_matched"),
                    "outcome": movement.get("outcome"),
                    "motive": None,
                },
            })
    return rows


def major_players(insiders, holders):
    """People and holders the feed returned. A missing action or motive stays missing."""
    players = []
    for row in insiders or []:
        action = row.get("action")
        players.append({
            "name": row.get("insider") or "A reported person",
            "role": row.get("role"),
            "kind": "insider",
            "action": action,
            "action_label": {
                "buy": "disclosed purchase",
                "sell": "disclosed sale",
            }.get(action, "transaction not labeled as a purchase or a sale"),
            "shares": _share_count(row.get("shares")),
            "date": row.get("date"),
            "url": row.get("url"),
            "source": row.get("source") or "yahoo_insider_transaction",
            "learned_at": row.get("learned_at"),
            "motive": None,
            "motive_note": "The feed did not include a motive.",
        })
    for row in holders or []:
        players.append({
            "name": row.get("holder") or "A reported holder",
            "role": None,
            "kind": "institution",
            "action": None,
            "action_label": "reported holding",
            "shares": _share_count(row.get("shares")),
            "percent_out": _share_count(row.get("percent")),
            "date": row.get("date"),
            "url": row.get("url"),
            "source": row.get("source") or "yahoo_institutional_holder",
            "learned_at": row.get("learned_at"),
            "motive": None,
            "motive_note": "The feed did not include a motive or a buy or sale for this holder.",
        })
    return players


def investigate_flow(insiders, holders, bars, news_items=None):
    """Look for a public why only when the returned activity is mostly buying or selling."""
    activity = activity_share(insiders)
    volume = volume_comparison(bars)
    players = major_players(insiders, holders)
    large_side = None
    if (
        activity.get("sell_percent") is not None
        and activity["sell_shares"] > activity["buy_shares"]
        and activity["sell_percent"] >= 50
    ):
        large_side = "selling"
    elif (
        activity.get("buy_percent") is not None
        and activity["buy_shares"] > activity["sell_shares"]
        and activity["buy_percent"] >= 50
    ):
        large_side = "buying"
    findings = []
    if large_side is None:
        why = "No large share of the labeled public activity was identified, so no investigation was opened."
        why_found = False
    else:
        action = "sell" if large_side == "selling" else "buy"
        for row in insiders or []:
            if row.get("action") != action:
                continue
            shares = _format_count(_share_count(row.get("shares")))
            findings.append({
                "kind": "insider",
                "summary": (
                    f"{row.get('insider') or 'A reported person'}"
                    f"{' (' + row['role'] + ')' if row.get('role') else ''}: "
                    f"disclosed {'sale' if action == 'sell' else 'purchase'}"
                    f"{' of ' + shares + ' shares' if shares else ''}"
                    f"{' on ' + str(row['date']) if row.get('date') else ''}. "
                    "The feed did not include a motive."
                ),
                "date": row.get("date"),
                "url": row.get("url"),
                "source": row.get("source") or "yahoo_insider_transaction",
            })
        if volume.get("unusual"):
            findings.append({
                "kind": "volume",
                "summary": volume["note"],
                "date": (bars[-1].get("time") if bars else None),
                "url": None,
                "source": "market_bars",
            })
        for item in news_items or []:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            if not title:
                continue
            findings.append({
                "kind": "news",
                "summary": f"Headline returned by the news feed: {title}. This is not labeled as the cause.",
                "date": item.get("published_at"),
                "url": item.get("url"),
                "source": item.get("source") or "news_feed",
            })
        for row in holders or []:
            shares = _format_count(_share_count(row.get("shares")))
            findings.append({
                "kind": "holder",
                "summary": (
                    f"{row.get('holder') or 'A reported holder'} has a reported holding"
                    f"{' of ' + shares + ' shares' if shares else ''}"
                    f"{' dated ' + str(row['date']) if row.get('date') else ''}. "
                    "The feed did not say this holder bought or sold."
                ),
                "date": row.get("date"),
                "url": row.get("url"),
                "source": row.get("source") or "yahoo_institutional_holder",
            })
        reasons = [item for item in findings if item["kind"] != "insider"]
        if reasons:
            why = (
                f"The labeled share counts are mostly {large_side}. "
                "The public items returned alongside that activity are listed with their sources. "
                "They are not a private motive."
            )
            why_found = True
        else:
            why = (
                f"The labeled share counts are mostly {large_side}. "
                "No unusual volume, related headline, or holder row was returned with them. "
                "No public reason was returned. The feed did not include a motive."
            )
            why_found = False
    return {
        "large_side": large_side,
        "why": why,
        "why_found": why_found,
        "activity": activity,
        "volume": volume,
        "findings": findings,
        "players": players,
    }


def build_prediction(symbol, events, bars=None, news_items=None):
    """Likelihood, tendencies, and track-record context. Does not submit an order."""
    symbol = str(symbol or "").strip().upper()
    events = list(events or [])
    moves = _later_bar_moves(events)
    insiders = _insider_rows(events)
    holders = _holder_rows(events)
    up = sum(1 for item in moves if item["move"] == "up")
    down = sum(1 for item in moves if item["move"] == "down")
    flat = sum(1 for item in moves if item["move"] == "flat")
    checked = up + down + flat
    past_lean = None
    if checked >= 3 and up > down:
        past_lean = "higher"
    elif checked >= 3 and down > up:
        past_lean = "lower"
    elif checked >= 3 and up == down and up > 0:
        past_lean = "split"
    if checked < 3:
        past_note = (
            "No stored scan has a later bar yet."
            if checked == 0
            else (
                f"{checked} stored later {'bar' if checked == 1 else 'bars'}: "
                f"{up} higher, {down} lower, {flat} flat. "
                "That is not enough to state a past likelihood."
            )
        )
    else:
        past_note = (
            f"Of {checked} later bars, {up} closed higher, {down} closed lower, and {flat} were flat."
        )
    buys = sum(1 for row in insiders if row.get("action") == "buy")
    sales = sum(1 for row in insiders if row.get("action") == "sell")
    unclassified = len(insiders) - buys - sales
    present_lean = None
    if buys + sales > 0 and buys > sales:
        present_lean = "more_disclosed_buying"
    elif buys + sales > 0 and sales > buys:
        present_lean = "more_disclosed_selling"
    elif buys > 0 and buys == sales:
        present_lean = "even"
    if not insiders:
        present_note = "The feed did not return an insider transaction."
    elif buys + sales == 0:
        present_note = (
            "The feed returned insider rows but did not label them as purchases or sales. "
            "The feed did not include a motive."
        )
    else:
        present_note = (
            f"Disclosed rows labeled {buys} purchase{'s' if buys != 1 else ''} "
            f"and {sales} sale{'s' if sales != 1 else ''}"
            + (f", plus {unclassified} unlabeled." if unclassified else ".")
            + " The feed did not include a motive."
        )
    holder_note = (
        f"The feed returned {len(holders)} institutional holder "
        f"{'row' if len(holders) == 1 else 'rows'}. It did not label those rows as buys or sells."
        if holders
        else "No institutional holder row was returned."
    )
    dip_cases = _count_pattern(insiders, moves, "buy", "down")
    strength_cases = _count_pattern(insiders, moves, "sell", "up")
    tendencies = []
    not_supported = []
    if len(dip_cases) >= 2:
        tendencies.append({
            "name": "buying_dips",
            "cases": len(dip_cases),
            "summary": (
                f"{len(dip_cases)} disclosed purchases are dated after a stored close that was lower. "
                "That repeated public timing is the basis for a buying-dips reading. "
                "The feed did not include a motive."
            ),
        })
    else:
        not_supported.append(
            "Not enough disclosed purchases dated after a lower stored close to call that a buying-dips tendency."
        )
    if len(strength_cases) >= 2:
        tendencies.append({
            "name": "selling_into_strength",
            "cases": len(strength_cases),
            "summary": (
                f"{len(strength_cases)} disclosed sales are dated after a stored close that was higher. "
                "That repeated public timing is the basis for a selling-into-strength reading. "
                "The feed did not include a motive."
            ),
        })
    else:
        not_supported.append(
            "Not enough disclosed sales dated after a higher stored close to call that a selling-into-strength tendency."
        )
    by_action = {"buy": set(), "sell": set()}
    for row in insiders:
        action = row.get("action")
        name = row.get("insider")
        if action in by_action and name:
            by_action[action].add(name)
    if len(by_action["buy"]) >= 2 or len(by_action["sell"]) >= 2:
        which = "purchases" if len(by_action["buy"]) >= len(by_action["sell"]) else "sales"
        people = by_action["buy"] if which == "purchases" else by_action["sell"]
        tendencies.append({
            "name": "clustered_disclosed_action",
            "cases": len(people),
            "summary": (
                f"{len(people)} reported people disclosed {which} in the rows returned. "
                "That is a cluster of the same disclosed action, not a shared private plan."
            ),
        })
    else:
        not_supported.append(
            "Not enough people disclosed the same action to call that clustering."
        )
    if holders and (len(by_action["buy"]) >= 2 or len(by_action["sell"]) >= 2):
        tendencies.append({
            "name": "alongside_reported_holders",
            "cases": len(holders),
            "summary": (
                "Those repeated disclosed actions were returned in the same read as institutional holder rows. "
                "The feed did not say the holders took the same action."
            ),
        })
    elif not holders:
        not_supported.append(
            "No institutional holder row was returned, so there is no comparison with large holders."
        )
    if buys >= 2:
        tendencies.append({
            "name": "repeated_disclosed_purchases",
            "cases": buys,
            "summary": "Disclosed purchases appear more than once in the returned rows. The feed did not include a motive.",
        })
    else:
        not_supported.append("Not enough repeated disclosed purchases to call that a pattern.")
    if sales >= 2:
        tendencies.append({
            "name": "repeated_disclosed_sales",
            "cases": sales,
            "summary": "Disclosed sales appear more than once in the returned rows. The feed did not include a motive.",
        })
    else:
        not_supported.append("Not enough repeated disclosed sales to call that a pattern.")
    votes_up = 0
    votes_down = 0
    if past_lean == "higher":
        votes_up += 1
    elif past_lean == "lower":
        votes_down += 1
    if present_lean == "more_disclosed_buying":
        votes_up += 1
    elif present_lean == "more_disclosed_selling":
        votes_down += 1
    if any(item["name"] in {"buying_dips", "repeated_disclosed_purchases"} for item in tendencies):
        votes_up += 1
    if any(item["name"] in {"selling_into_strength", "repeated_disclosed_sales"} for item in tendencies):
        votes_down += 1
    direction = None
    if votes_up > 0 and votes_down == 0:
        direction = "up"
    elif votes_down > 0 and votes_up == 0:
        direction = "down"
    outcomes = [event for event in events if event.get("source") == "prediction_outcome"]
    investors = build_investor_board(events, bars or [])
    extra_grades = []
    for investor in investors["investors"]:
        for movement in investor["movements"]:
            if not movement.get("in_track_record"):
                continue
            extra_grades.append({
                "matched": movement.get("track_matched") is True,
                "learned_at": (movement.get("outcome") or {}).get("later_time") or "",
                "direction": movement.get("lean_direction"),
            })
    record = assess_track_record(outcomes, extra_grades)
    learned = _learned_tendencies(outcomes)
    tendencies.extend(learned["tendencies"])
    not_supported.extend(learned["not_supported"])
    levels = printed_levels(bars or [])
    shown = bool(direction or past_lean in {"higher", "lower"} or present_lean in {
        "more_disclosed_buying", "more_disclosed_selling", "even",
    } or tendencies)
    if shown and direction == "up":
        lead = (
            "The public rows lean toward a higher close. "
            + past_note + " " + present_note
            + " This is a likelihood from public data, not a forecast of profit."
        )
    elif shown and direction == "down":
        lead = (
            "The public rows lean toward a lower close. "
            + past_note + " " + present_note
            + " This is a likelihood from public data, not a forecast of profit."
        )
    elif shown:
        lead = (
            "The public counts are shown below, and they do not agree on one direction. "
            "No single likelihood is stated. This is not a forecast of profit."
        )
    else:
        lead = (
            f"There is not enough public data to state a likelihood for {symbol}. "
            "No number is filled in."
        )
    sources = []
    for event in events:
        if event.get("source") not in {
            "later_market_bar",
            "yahoo_insider_transaction",
            "yahoo_institutional_holder",
            "prediction_outcome",
        }:
            continue
        row = (event.get("detail") or {}).get("row") or {}
        sources.append({
            "summary": event.get("summary"),
            "source": event.get("source"),
            "learned_at": event.get("learned_at"),
            "effect": event.get("effect"),
            "url": row.get("url"),
        })
    prediction = {
        "symbol": symbol,
        "status": "SHOWN" if shown else "INSUFFICIENT",
        "effect": "context_only" if shown else "none",
        "direction": direction,
        "disclaimer": PREDICTION_DISCLAIMER,
        "lead": lead,
        "past": {
            "checked": checked,
            "up": up,
            "down": down,
            "flat": flat,
            "lean": past_lean,
            "note": past_note,
        },
        "present": {
            "buys": buys,
            "sales": sales,
            "unclassified": unclassified,
            "holders": len(holders),
            "lean": present_lean,
            "note": present_note,
            "holder_note": holder_note,
        },
        "tendencies": tendencies,
        "not_supported": not_supported,
        "track_record": record,
        "levels": levels,
        "sources": sources,
        "sec_form4_search": _sec_search(symbol, "4"),
        "sec_13f_search": _sec_search(symbol, "13F-HR"),
        "changes_decision": False,
        "flow": investigate_flow(insiders, holders, bars or [], news_items),
        "investors": investors,
    }
    return prediction


def _learned_tendencies(outcomes):
    up_cases = [item for item in outcomes if (item.get("detail") or {}).get("direction") == "up"]
    down_cases = [item for item in outcomes if (item.get("detail") or {}).get("direction") == "down"]
    tendencies = []
    not_supported = []
    up_matched = sum(1 for item in up_cases if (item.get("detail") or {}).get("matched") is True)
    down_matched = sum(1 for item in down_cases if (item.get("detail") or {}).get("matched") is True)
    if len(up_cases) >= 2 and up_matched >= 2:
        tendencies.append({
            "name": "learned_higher_close",
            "cases": up_matched,
            "summary": (
                f"Stored leans toward a higher close were followed by a higher close "
                f"in {up_matched} of {len(up_cases)} graded cases."
            ),
        })
    else:
        not_supported.append(
            "Not enough graded leans toward a higher close to update that tendency."
        )
    if len(down_cases) >= 2 and down_matched >= 2:
        tendencies.append({
            "name": "learned_lower_close",
            "cases": down_matched,
            "summary": (
                f"Stored leans toward a lower close were followed by a lower close "
                f"in {down_matched} of {len(down_cases)} graded cases."
            ),
        })
    else:
        not_supported.append(
            "Not enough graded leans toward a lower close to update that tendency."
        )
    return {"tendencies": tendencies, "not_supported": not_supported}


def assess_track_record(outcomes, extra_grades=None):
    """Counts only. A passing record is at least 8 grades, more right than wrong, and a better later half.

    Graded prediction outcomes and confirmed major-investor comparisons are both counted.
    A comparison without a later public bar is not counted.
    """
    graded = []
    for event in outcomes or []:
        if event.get("source") != "prediction_outcome":
            continue
        detail = event.get("detail") or {}
        graded.append({
            "matched": detail.get("matched") is True,
            "learned_at": event.get("learned_at") or "",
            "direction": detail.get("direction"),
        })
    for grade in extra_grades or []:
        graded.append({
            "matched": grade.get("matched") is True,
            "learned_at": grade.get("learned_at") or "",
            "direction": grade.get("direction"),
        })
    graded.sort(key=lambda item: item["learned_at"])
    correct = sum(1 for item in graded if item["matched"])
    incorrect = len(graded) - correct
    half = len(graded) // 2
    older = graded[:half]
    recent = graded[-half:] if half else []
    older_correct = sum(1 for item in older if item["matched"])
    recent_correct = sum(1 for item in recent if item["matched"])
    improving = bool(half) and recent_correct * len(older) > older_correct * len(recent)
    more_right = correct > incorrect
    passed = len(graded) >= PREDICTION_MIN_GRADED and more_right and improving
    if len(graded) < PREDICTION_MIN_GRADED:
        reason = (
            f"Track record insufficient: {len(graded)} graded prediction"
            f"{'' if len(graded) == 1 else 's'} stored, and {PREDICTION_MIN_GRADED} are required."
        )
    elif not more_right:
        reason = (
            f"Track record is not more right than wrong: {correct} matched and {incorrect} did not."
        )
    elif not improving:
        reason = (
            f"Track record is not improving: the earlier {len(older)} matched {older_correct}, "
            f"and the latest {len(recent)} matched {recent_correct}."
        )
    else:
        reason = (
            f"Track record met: {correct} of {len(graded)} graded predictions matched, "
            f"and the latest {len(recent)} matched {recent_correct} "
            f"versus {older_correct} of the earlier {len(older)}."
        )
    return {
        "graded": len(graded),
        "correct": correct,
        "incorrect": incorrect,
        "older_graded": len(older),
        "older_correct": older_correct,
        "recent_graded": len(recent),
        "recent_correct": recent_correct,
        "improving": improving,
        "more_right_than_wrong": more_right,
        "passed": passed,
        "minimum": PREDICTION_MIN_GRADED,
        "reason": reason,
    }


def evaluate_prediction_gate(prediction, *, equity, exposure, daily_pnl, halted, has_long):
    """Paper button gate. It does not change the scan decision and does not submit."""
    from atlas.risk import calculate_position_size, validate_daily_loss

    prediction = prediction or {}
    record = prediction.get("track_record") or assess_track_record([])
    direction = prediction.get("direction")
    levels = prediction.get("levels") or {}
    entry = levels.get("entry")
    stop = levels.get("stop")
    quantity = 0
    if (
        isinstance(entry, (int, float))
        and isinstance(stop, (int, float))
        and not isinstance(entry, bool)
        and not isinstance(stop, bool)
        and entry > 0
        and stop > 0
        and stop != entry
    ):
        try:
            quantity = calculate_position_size(
                equity,
                entry,
                stop,
                current_exposure=exposure,
            )
        except (TypeError, ValueError):
            quantity = 0
    halt = bool(halted)
    if not halt and isinstance(equity, (int, float)) and isinstance(daily_pnl, (int, float)):
        try:
            halt = not validate_daily_loss(equity, daily_pnl)
        except (TypeError, ValueError):
            halt = True
    allow_buy = False
    allow_sell = False
    if prediction.get("status") != "SHOWN" or direction not in {"up", "down"}:
        reason = "The current prediction is not supported by public past and present rows."
    elif not record.get("passed"):
        reason = record.get("reason") or "Track record insufficient."
    elif halt:
        reason = "Daily-loss halt blocks a paper order from the prediction gate."
    elif direction == "up":
        if not isinstance(quantity, int) or quantity <= 0:
            reason = "Risk size is 0, so the prediction gate does not enable a buy."
        else:
            allow_buy = True
            reason = (
                "Track record met and this is a paper buy from the prediction gate. "
                "The scan decision is unchanged. You still have to confirm."
            )
    else:
        if not has_long:
            reason = (
                "Sell stays off. There is no open long to close, and this page does not open a short."
            )
        elif not isinstance(quantity, int) or quantity <= 0:
            reason = "Risk size is 0, so the prediction gate does not enable a sell."
        else:
            allow_sell = True
            reason = (
                "Track record met and this is a paper sell to close the open long from the prediction gate. "
                "It does not open a short."
            )
    audit = {
        "direction": direction,
        "graded": record.get("graded"),
        "correct": record.get("correct"),
        "incorrect": record.get("incorrect"),
        "improving": record.get("improving"),
        "quantity": quantity if isinstance(quantity, int) else 0,
        "halted": halt,
        "has_long": bool(has_long),
        "allow_buy": allow_buy,
        "allow_sell": allow_sell,
        "reason": reason,
        "sources": [item.get("source") for item in prediction.get("sources") or []],
    }
    source_key = (
        f"{direction}|{audit['graded']}|{audit['correct']}|{audit['incorrect']}|"
        f"{int(bool(record.get('improving')))}|{audit['quantity']}|{int(halt)}|"
        f"{int(bool(has_long))}|{int(allow_buy)}|{int(allow_sell)}"
    )
    return {
        "allow_buy": allow_buy,
        "allow_sell": allow_sell,
        "reason": reason,
        "buy_note": (
            reason
            if direction != "down"
            else "Buy stays off. The public rows do not lean toward a higher close."
        ),
        "sell_note": (
            reason
            if direction != "up"
            else "Sell stays off for a new short. This page only sells to close an open long."
        ),
        "quantity": audit["quantity"],
        "entry": entry,
        "stop": stop,
        "target": levels.get("target"),
        "time": levels.get("time"),
        "halted": halt,
        "audit": audit,
        "source_key": source_key,
        "effect": "context_only" if allow_buy or allow_sell else "none",
    }


def prediction_reference(prediction):
    """Stored lean used later to grade a real subsequent bar. None when no direction or price."""
    if prediction.get("status") != "SHOWN" or prediction.get("direction") not in {"up", "down"}:
        return None
    levels = prediction.get("levels") or {}
    entry = levels.get("entry")
    stamp = levels.get("time")
    if not isinstance(entry, (int, float)) or isinstance(entry, bool) or entry <= 0 or not stamp:
        return None
    past = prediction.get("past") or {}
    present = prediction.get("present") or {}
    source_key = (
        f"{stamp}|{prediction['direction']}|{past.get('up')}|{past.get('down')}|"
        f"{present.get('buys')}|{present.get('sales')}"
    )
    return {
        "source": "prediction_lean",
        "source_key": source_key,
        "effect": "context_only",
        "summary": prediction.get("lead"),
        "detail": {
            "direction": prediction["direction"],
            "reference_price": entry,
            "reference_time": stamp,
            "past": past,
            "present": present,
            "tendency_names": [item["name"] for item in prediction.get("tendencies") or []],
        },
    }


def grade_prediction_outcomes(events, bars):
    """Pair each stored lean with a later printed close. No outcome is invented."""
    graded = []
    already = {
        (event.get("detail") or {}).get("lean_key")
        for event in events or []
        if event.get("source") == "prediction_outcome"
    }
    for event in events or []:
        if event.get("source") != "prediction_lean":
            continue
        detail = event.get("detail") or {}
        direction = detail.get("direction")
        price = detail.get("reference_price")
        started = _when(detail.get("reference_time"))
        if direction not in {"up", "down"} or started is None:
            continue
        if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
            continue
        later = None
        for bar in bars or []:
            when = _when(bar.get("time"))
            if when is None or when <= started:
                continue
            if later is None or when < later[0]:
                later = (when, bar)
        if later is None:
            continue
        _when_later, bar = later
        close = bar.get("close")
        if not isinstance(close, (int, float)) or isinstance(close, bool):
            continue
        if close > price:
            move = "up"
        elif close < price:
            move = "down"
        else:
            move = "flat"
        matched = (direction == "up" and move == "up") or (direction == "down" and move == "down")
        lean_key = event.get("source_key")
        source_key = f"{lean_key}|{bar.get('time')}"
        if lean_key in already or source_key in already:
            continue
        graded.append({
            "source": "prediction_outcome",
            "source_key": source_key,
            "effect": "none",
            "summary": (
                f"Stored lean toward a {'higher' if direction == 'up' else 'lower'} close "
                f"was followed by a {move} move from {price} to {close}."
            ),
            "detail": {
                "lean_key": lean_key,
                "direction": direction,
                "reference_price": price,
                "later_close": close,
                "later_time": bar.get("time"),
                "move": move,
                "matched": matched,
            },
        })
    return graded


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
