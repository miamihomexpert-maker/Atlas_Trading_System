"""Headlines for the paper path.

Yahoo Finance via yfinance is the default feed. ``ATLAS_NEWS_FILE`` replaces
that fetch for tests and offline runs. Sentiment is a count of the words
below. A headline with none of those words has sentiment 0 and impact 0;
that is an unscored headline, not a neutral forecast.
"""

from datetime import datetime
import os
import re

from atlas.news import NewsEvent


_POSITIVE_WORDS = (
    "beat",
    "beats",
    "upgrade",
    "surge",
    "rally",
    "record",
    "growth",
    "strong",
)
_NEGATIVE_WORDS = (
    "miss",
    "misses",
    "downgrade",
    "plunge",
    "crash",
    "fraud",
    "investigation",
    "lawsuit",
    "layoff",
    "layoffs",
    "weak",
    "recession",
)
_CATEGORIES = (
    ("EARNINGS", ("earnings", "eps", "revenue", "guidance")),
    ("FED", ("fed", "fomc", "powell", "rate cut", "rate hike")),
    ("INFLATION", ("inflation", "cpi", "pce")),
    ("EMPLOYMENT", ("jobless", "payroll", "unemployment", "jobs report")),
    ("ECONOMIC", ("gdp", "recession", "economic")),
    ("GEOPOLITICAL", ("war", "sanction", "geopolitical")),
)


def _tokens(text):
    return set(re.findall(r"[a-z0-9]+", str(text).lower()))


def lexicon_sentiment(text):
    """Return (sentiment, impact, hit_count) from the fixed word lists."""
    tokens = _tokens(text)
    positive = sum(1 for word in _POSITIVE_WORDS if word in tokens)
    negative = sum(1 for word in _NEGATIVE_WORDS if word in tokens)
    hits = positive + negative
    if hits == 0:
        return 0.0, 0.0, 0
    sentiment = max(-1.0, min(1.0, (positive - negative) / hits))
    impact = min(1.0, hits / 4)
    return sentiment, impact, hits


def classify_category(text):
    lowered = str(text).lower()
    tokens = _tokens(text)
    for category, needles in _CATEGORIES:
        if any(
            (needle in lowered if " " in needle else needle in tokens)
            for needle in needles
        ):
            return category
    return "OTHER"


def load_news_file(path):
    """Read the JSON override used by ``ATLAS_NEWS_FILE``."""
    import json
    from pathlib import Path

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


def fetch_headlines(symbols, ticker_factory=None):
    """Pull Yahoo headlines for each symbol. Failures stay empty and named."""
    import yfinance as yf

    factory = ticker_factory or (lambda symbol: yf.Ticker(symbol))
    events = []
    errors = []
    seen = set()
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        if not normalized:
            continue
        try:
            payload = factory(normalized).news
        except Exception as error:
            errors.append(f"{normalized}: {error}")
            continue
        if not isinstance(payload, list):
            errors.append(f"{normalized}: news feed returned {type(payload).__name__}")
            continue
        for item in payload:
            event = _event_from_yahoo(item, normalized)
            if event is None or event.event_id in seen:
                continue
            seen.add(event.event_id)
            events.append(event)
    if events and not errors:
        status = "FETCHED"
    elif events and errors:
        status = "PARTIAL"
    elif errors:
        status = "FETCH_FAILED"
    else:
        status = "NO_RELEVANT_EVENTS"
    return events, {
        "status": status,
        "error": "; ".join(errors) or None,
        "source": "yfinance Ticker.news",
        "event_count": len(events),
    }


def resolve_news(symbols, ticker_factory=None):
    """File override when ``ATLAS_NEWS_FILE`` is set, otherwise Yahoo."""
    path = os.getenv("ATLAS_NEWS_FILE", "").strip()
    if path:
        try:
            events = load_news_file(path)
        except Exception as error:
            return [], {
                "status": "FETCH_FAILED",
                "error": str(error),
                "source": path,
                "event_count": 0,
            }
        return events, {
            "status": "FILE",
            "error": None,
            "source": path,
            "event_count": len(events),
        }
    return fetch_headlines(symbols, ticker_factory=ticker_factory)


def _event_from_yahoo(item, symbol):
    if not isinstance(item, dict):
        return None
    content = item.get("content") if isinstance(item.get("content"), dict) else item
    title = str(content.get("title") or "").strip()
    event_id = str(content.get("id") or item.get("id") or "").strip()
    published_raw = content.get("pubDate") or content.get("displayTime")
    if not title or not event_id or not published_raw:
        return None
    try:
        published_at = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if published_at.tzinfo is None:
        return None
    summary = str(content.get("summary") or content.get("description") or "")
    text = f"{title} {summary}"
    sentiment, impact, _hits = lexicon_sentiment(text)
    instruments = [symbol]
    finance = content.get("finance") if isinstance(content.get("finance"), dict) else {}
    for ticker in finance.get("stockTickers") or []:
        if not isinstance(ticker, dict):
            continue
        code = str(ticker.get("symbol") or "").strip().upper()
        if code and code not in instruments:
            instruments.append(code)
    return NewsEvent(
        event_id=event_id,
        title=title,
        category=classify_category(text),
        published_at=published_at,
        sentiment=sentiment,
        impact=impact,
        instruments=tuple(instruments),
    )
