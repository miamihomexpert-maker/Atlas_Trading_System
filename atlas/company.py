"""Company facts for the configured symbols.

Yahoo Finance, through yfinance, is the only source. A fund (the default
watchlist is SPY and QQQ) contributes its stated objective and asset
allocation. An operating company contributes statement rows for capital
expenditure, buybacks, dividends, business purchases, and research and
development only when that row is present. Management guidance is not read
from analyst estimates. Nothing here changes a trading decision.
"""

from datetime import date, datetime, timezone
import math
import os
import re

from atlas.news import http_url


_FUND_TYPES = {"ETF", "MUTUALFUND"}
_COMPANY_TYPES = {"EQUITY"}
_SOURCE_INFO = "yfinance Ticker.info"
_SOURCE_FUNDS = "yfinance Ticker.funds_data"
_SOURCE_CASH = "yfinance Ticker.cashflow"
_SOURCE_INCOME = "yfinance Ticker.income_stmt"

_ALLOCATION_ROWS = (
    ("capex", "Capital expenditure", "cash flow", ("capitalexpenditure",)),
    ("buybacks", "Share repurchases", "cash flow", ("repurchaseofcapitalstock",)),
    (
        "dividends",
        "Dividends paid",
        "cash flow",
        ("commonstockdividendpaid", "cashdividendspaid"),
    ),
    (
        "mna",
        "Business purchases",
        "cash flow",
        ("purchaseofbusiness", "netbusinesspurchaseandsale"),
    ),
    (
        "rnd",
        "Research and development",
        "income statement",
        ("researchanddevelopment",),
    ),
)
_ASSET_LABELS = {
    "cashPosition": "Cash",
    "stockPosition": "Stocks",
    "bondPosition": "Bonds",
    "preferredPosition": "Preferred stock",
    "convertiblePosition": "Convertible securities",
    "otherPosition": "Other",
}
_MISSING_ALLOCATION = (
    ("capex", "No capital-expenditure amount was returned."),
    ("buybacks", "No share-repurchase amount was returned."),
    ("dividends", "No dividend amount was returned."),
    ("mna", "No business-purchase amount was returned."),
    ("rnd", "No research-and-development amount was returned."),
)


def company_facts_enabled():
    """Tests set ``ATLAS_COMPANY_FACTS=false`` so they do not call Yahoo."""
    return os.getenv("ATLAS_COMPANY_FACTS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def get_company_status():
    return (
        "COMPANY FACTS: Yahoo Finance fund profile and statement rows "
        "when the feed returns them; empty when it does not"
    )


def blank_profile(symbol, status, error=None, source=None, fetched_at=None,
                  quote_type=None, kind=None):
    return {
        "symbol": str(symbol or "").strip().upper(),
        "status": status,
        "error": error,
        "source": source,
        "fetched_at": fetched_at,
        "quote_type": quote_type,
        "kind": kind,
        "name": None,
        "objective": None,
        "category": None,
        "family": None,
        "legal_type": None,
        "holdings": [],
        "sectors": [],
        "asset_classes": [],
        "allocation": [],
        "guidance": None,
        "website": None,
    }


def resolve_company_loader(loader=None):
    """Return a callable, or None when company facts are turned off."""
    if loader is not None:
        return loader
    if not company_facts_enabled():
        return None
    return load_company_facts


def profile_for_symbol(symbol, loader=None, now=None):
    """Load one profile. A missing loader records that facts were not loaded."""
    if loader is None:
        return blank_profile(symbol, status="NOT_LOADED", fetched_at=_stamp(now))
    try:
        profile = loader(symbol)
    except Exception as error:
        return blank_profile(
            symbol,
            status="FETCH_FAILED",
            error=str(error),
            fetched_at=_stamp(now),
        )
    if not isinstance(profile, dict):
        return blank_profile(
            symbol,
            status="FETCH_FAILED",
            error="Company loader did not return a profile.",
            fetched_at=_stamp(now),
        )
    return profile


def load_company_facts(symbol, ticker_factory=None, now=None):
    """Read one symbol from Yahoo. Failures stay empty and named."""
    fetched_at = _stamp(now)
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return blank_profile(
            symbol,
            status="EMPTY",
            error="No symbol was provided.",
            fetched_at=fetched_at,
        )
    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    try:
        ticker = ticker_factory(normalized)
    except Exception as error:
        return blank_profile(
            normalized,
            status="FETCH_FAILED",
            error=str(error),
            source="yfinance Ticker",
            fetched_at=fetched_at,
        )
    return _read_ticker(normalized, ticker, fetched_at)


def describe_company(profile):
    """Plain sentences from fields already on the profile. No new figures."""
    profile = profile or {}
    status = profile.get("status")
    symbol = profile.get("symbol") or "This symbol"
    kind = profile.get("kind")
    name = profile.get("name")
    title = f"{name} ({symbol})" if name else symbol
    lines = []
    missing = []
    objective = None
    if status == "NOT_LOADED":
        summary = "Company facts were not loaded for this symbol."
    elif status == "FETCH_FAILED":
        summary = "The company feed failed, so this panel is empty."
        if profile.get("error"):
            missing.append(str(profile["error"]))
    elif status == "EMPTY" and kind == "fund":
        summary = (
            f"{title} is a fund, and the feed returned no objective, holdings, "
            "or sector weights. Operating-company spending lines are not shown."
        )
    elif status == "EMPTY" and kind == "company":
        summary = (
            f"{title} is an operating company, and the feed returned no "
            "objective and no statement amounts."
        )
    elif status == "EMPTY":
        summary = (
            "The company feed returned no objective, holdings, sector weights, "
            "or spending figures."
        )
    elif kind == "fund":
        summary = (
            f"{title} is a fund. This panel shows the stated objective and "
            "where the fund's assets are allocated. It is not an operating "
            "company, so capital expenditure, buybacks, dividends, research, "
            "acquisitions, and management guidance are not shown."
        )
    elif kind == "company":
        summary = (
            f"{title} is an operating company. The lines below are amounts "
            "the statement feed actually included."
        )
    else:
        summary = (
            f"{title} was returned without a fund or company label. "
            "Only the fields the feed included are shown."
        )
    if status in {"FETCHED", "PARTIAL"}:
        if profile.get("objective"):
            objective = "Stated objective: " + str(profile["objective"])
        if profile.get("category"):
            lines.append(
                f"The fund category returned by the feed is {profile['category']}."
            )
        if profile.get("family"):
            lines.append(
                f"The fund family returned by the feed is {profile['family']}."
            )
        if profile.get("legal_type"):
            lines.append(
                f"The legal type returned by the feed is {profile['legal_type']}."
            )
        for item in profile.get("asset_classes") or []:
            phrase = _weight_phrase(item.get("weight"))
            if phrase and item.get("name"):
                lines.append(f"{item['name']} is {phrase} of the fund.")
        for item in profile.get("sectors") or []:
            phrase = _weight_phrase(item.get("weight"))
            if phrase and item.get("name"):
                lines.append(f"{item['name']} is {phrase} of the fund.")
        for item in profile.get("holdings") or []:
            phrase = _weight_phrase(item.get("weight"))
            if not phrase:
                continue
            holding_name = item.get("name")
            holding_symbol = item.get("symbol")
            if holding_name and holding_symbol:
                label = f"{holding_name} ({holding_symbol})"
            else:
                label = holding_name or holding_symbol
            if label:
                lines.append(f"{label} is {phrase} of the fund.")
        for item in profile.get("allocation") or []:
            sentence = _allocation_sentence(item)
            if sentence:
                lines.append(sentence)
        if kind == "fund":
            if not profile.get("holdings"):
                missing.append("No holdings were returned.")
            if not profile.get("sectors"):
                missing.append("No sector weights were returned.")
            if not profile.get("asset_classes"):
                missing.append("No asset-class weights were returned.")
        if kind == "company":
            present = {item.get("key") for item in profile.get("allocation") or []}
            for key, sentence in _MISSING_ALLOCATION:
                if key not in present:
                    missing.append(sentence)
            missing.append("No management guidance figure was returned.")
        if status == "PARTIAL" and profile.get("error"):
            missing.append(f"Part of the company feed failed: {profile['error']}")
    return {
        "summary": summary,
        "objective": objective,
        "lines": lines,
        "missing": missing,
        "source": profile.get("source"),
        "fetched_at": profile.get("fetched_at"),
        "status": status,
    }


def _read_ticker(symbol, ticker, fetched_at):
    info, info_error = _read_attr(ticker, "info")
    if info_error:
        return blank_profile(
            symbol,
            status="FETCH_FAILED",
            error=info_error,
            source=_SOURCE_INFO,
            fetched_at=fetched_at,
        )
    if not isinstance(info, dict):
        return blank_profile(
            symbol,
            status="FETCH_FAILED",
            error=f"info was {type(info).__name__}, not a dictionary.",
            source=_SOURCE_INFO,
            fetched_at=fetched_at,
        )

    quote_type = _text(info.get("quoteType"))
    quote_type = quote_type.upper() if quote_type else None
    profile = blank_profile(
        symbol,
        status="EMPTY",
        source=_SOURCE_INFO,
        fetched_at=fetched_at,
        quote_type=quote_type,
        kind=_kind(quote_type),
    )
    profile["name"] = _company_name(info, symbol)
    profile["objective"] = _text(info.get("longBusinessSummary"))
    profile["website"] = http_url(info.get("website"))
    currency = _text(info.get("financialCurrency")) or _text(info.get("currency"))
    errors = []
    sources = [_SOURCE_INFO]

    if profile["kind"] != "company":
        funds, funds_error = _read_attr(ticker, "funds_data")
        sources.append(_SOURCE_FUNDS)
        if funds_error:
            errors.append(funds_error)
        elif funds is not None:
            _apply_funds(profile, funds, errors)
            if not profile["quote_type"]:
                profile["quote_type"] = _quote_type_from_funds(funds)
                profile["kind"] = _kind(profile["quote_type"])

    if profile["kind"] == "company":
        _apply_statements(profile, ticker, currency, errors, sources)

    profile["source"] = "; ".join(sources)
    profile["error"] = "; ".join(errors) or None
    profile["status"] = _status(profile, errors)
    profile["guidance"] = None
    return profile


def _apply_funds(profile, funds, errors):
    overview = getattr(funds, "fund_overview", None)
    if isinstance(overview, dict):
        profile["category"] = _text(overview.get("categoryName"))
        profile["family"] = _text(overview.get("family"))
        profile["legal_type"] = _text(overview.get("legalType"))
    if not profile["objective"]:
        description, description_error = _read_attr(funds, "description")
        if description_error:
            errors.append(description_error)
        else:
            profile["objective"] = _text(description)
    for label, reader in (
        ("holdings", lambda: _holdings_from(getattr(funds, "top_holdings", None))),
        ("sectors", lambda: _named_weights(getattr(funds, "sector_weightings", None))),
        (
            "asset_classes",
            lambda: _named_weights(
                getattr(funds, "asset_classes", None),
                labels=_ASSET_LABELS,
            ),
        ),
    ):
        try:
            profile[label] = reader()
        except Exception as error:
            errors.append(f"{label}: {error}")


def _apply_statements(profile, ticker, currency, errors, sources):
    cash, cash_error = _read_attr(ticker, "cashflow")
    sources.append(_SOURCE_CASH)
    if cash_error:
        errors.append(f"cashflow: {cash_error}")
    else:
        _append_rows(profile, cash, currency, {"capex", "buybacks", "dividends", "mna"})
    income, income_error = _read_attr(ticker, "income_stmt")
    sources.append(_SOURCE_INCOME)
    if income_error:
        errors.append(f"income statement: {income_error}")
    else:
        _append_rows(profile, income, currency, {"rnd"})


def _append_rows(profile, frame, currency, wanted):
    found = _statement_rows(frame)
    if not found:
        return
    for key, label, statement, aliases in _ALLOCATION_ROWS:
        if key not in wanted:
            continue
        for alias in aliases:
            match = found.get(alias)
            if match is None:
                continue
            row_name, series = match
            latest = _latest_cell(series)
            if latest is None:
                continue
            period, amount = latest
            profile["allocation"].append({
                "key": key,
                "label": label,
                "statement": statement,
                "row": str(row_name),
                "period": period,
                "amount": amount,
                "currency": currency,
            })
            break


def _statement_rows(frame):
    if frame is None or not hasattr(frame, "index") or not hasattr(frame, "loc"):
        return {}
    if getattr(frame, "empty", False):
        return {}
    rows = {}
    for index in frame.index:
        label = index[0] if isinstance(index, tuple) else index
        key = _compact(label)
        if not key or key in rows:
            continue
        rows[key] = (label, frame.loc[index])
    return rows


def _latest_cell(series):
    index = getattr(series, "index", None)
    if index is None:
        number = _number(series)
        if number is None:
            return None
        return None, number
    for column in index:
        number = _number(series[column])
        if number is not None:
            return _period_label(column), number
    return None


def _holdings_from(value):
    if value is None:
        return []
    if hasattr(value, "iterrows"):
        holdings = []
        for symbol, row in value.iterrows():
            holding = _holding(
                symbol,
                _cell(row, ("Name", "name", "holdingName")),
                _cell(row, ("Holding Percent", "weight", "holdingPercent")),
            )
            if holding is not None:
                holdings.append(holding)
        return holdings
    if isinstance(value, list):
        holdings = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if "weight" in item:
                raw_weight = item.get("weight")
            elif "Holding Percent" in item:
                raw_weight = item.get("Holding Percent")
            else:
                raw_weight = item.get("holdingPercent")
            holding = _holding(
                item.get("symbol") or item.get("Symbol"),
                item.get("name") or item.get("Name") or item.get("holdingName"),
                raw_weight,
            )
            if holding is not None:
                holdings.append(holding)
        return holdings
    return []


def _holding(symbol, name, weight):
    number = _number(weight)
    if number is None:
        return None
    symbol_text = _text(symbol)
    name_text = _text(name)
    if not symbol_text and not name_text:
        return None
    return {"symbol": symbol_text, "name": name_text, "weight": number}


def _named_weights(mapping, labels=None):
    if not isinstance(mapping, dict):
        return []
    items = []
    for key, raw in mapping.items():
        number = _number(raw)
        key_text = _text(key)
        if number is None or not key_text:
            continue
        if labels and key_text in labels:
            name = labels[key_text]
        else:
            name = _display_key(key_text)
        items.append({"key": key_text, "name": name, "weight": number})
    return items


def _status(profile, errors):
    has_facts = bool(
        profile["objective"]
        or profile["name"]
        or profile["category"]
        or profile["family"]
        or profile["legal_type"]
        or profile["holdings"]
        or profile["sectors"]
        or profile["asset_classes"]
        or profile["allocation"]
    )
    if has_facts and errors:
        return "PARTIAL"
    if has_facts:
        return "FETCHED"
    if errors:
        return "FETCH_FAILED"
    return "EMPTY"


def _kind(quote_type):
    if quote_type in _FUND_TYPES:
        return "fund"
    if quote_type in _COMPANY_TYPES:
        return "company"
    if quote_type:
        return "other"
    return None


def _quote_type_from_funds(funds):
    value = getattr(funds, "quote_type", None)
    if callable(value):
        try:
            value = value()
        except Exception:
            return None
    text = _text(value)
    return text.upper() if text else None


def _company_name(info, symbol):
    for key in ("shortName", "longName"):
        text = _text(info.get(key))
        if text and text.upper() != symbol:
            return text
    return None


def _allocation_sentence(item):
    amount = _number(item.get("amount"))
    if amount is None or not item.get("label"):
        return None
    statement = "cash-flow statement" if item.get("statement") == "cash flow" else "income statement"
    amount_text = f"{amount:,.2f}"
    if item.get("currency"):
        amount_text = f"{amount_text} {item['currency']}"
    else:
        amount_text = f"{amount_text} (as returned)"
    period = item.get("period")
    where = (
        f"for the period ending {period}"
        if period
        else "The feed did not label the period."
    )
    if period:
        sentence = (
            f"{item['label']} on the {statement} {where} was {amount_text}."
        )
    else:
        sentence = f"{item['label']} on the {statement} was {amount_text}. {where}"
    if item.get("row"):
        sentence += f" The statement row was \"{item['row']}\"."
    return sentence


def _weight_phrase(value):
    number = _number(value)
    if number is None:
        return None
    if abs(number) <= 1.5:
        return f"{number * 100:.2f}%"
    return f"{number} (as returned)"


def _display_key(key):
    text = str(key).replace("_", " ").strip()
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    if not text:
        return text
    return text[:1].upper() + text[1:]


def _compact(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _cell(row, keys):
    getter = getattr(row, "get", None)
    if getter is None:
        return None
    for key in keys:
        if key in getattr(row, "index", ()):
            return getter(key)
    return None


def _read_attr(obj, name):
    try:
        return getattr(obj, name), None
    except Exception as error:
        return None, str(error)


def _text(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    return text or None


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        if "raw" not in value:
            return None
        return _number(value.get("raw"))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _period_label(column):
    if hasattr(column, "to_pydatetime"):
        try:
            column = column.to_pydatetime()
        except Exception:
            pass
    if hasattr(column, "date") and not isinstance(column, date):
        try:
            column = column.date()
        except Exception:
            pass
    if hasattr(column, "isoformat"):
        text = column.isoformat()
        return text[:10] if "T" in text else text
    text = str(column).strip()
    return text or None


def _stamp(now):
    if now is None:
        current = datetime.now(timezone.utc)
    elif callable(now):
        current = now()
    else:
        current = now
    if isinstance(current, datetime):
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.isoformat()
    return str(current)
