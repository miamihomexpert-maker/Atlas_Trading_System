from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from urllib.parse import urlsplit
import re


_SUPPORTED_CATEGORIES = {
    "EARNINGS",
    "FED",
    "INFLATION",
    "EMPLOYMENT",
    "ECONOMIC",
    "GEOPOLITICAL",
    "OTHER",
}


_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def http_url(value):
    """Return an http(s) URL, or None when the value is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(char.isspace() for char in text):
        return None
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def yahoo_quote_url(symbol):
    """Yahoo Finance quote page for a configured symbol. None if the symbol is not a ticker."""
    normalized = str(symbol or "").strip().upper()
    if not _TICKER.match(normalized):
        return None
    return f"https://finance.yahoo.com/quote/{normalized}"


def yahoo_profile_url(symbol):
    """Yahoo Finance profile page for a configured symbol. None if the symbol is not a ticker."""
    quote = yahoo_quote_url(symbol)
    if quote is None:
        return None
    return quote + "/profile"


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    title: str
    category: str
    published_at: datetime
    sentiment: float
    impact: float
    instruments: tuple = ()
    url: str = None
    summary: str = None
    publisher: str = None
    thumbnail_url: str = None
    video_url: str = None

    def __post_init__(self):
        if not str(self.event_id).strip() or not str(self.title).strip():
            raise ValueError("News events require an ID and title.")
        category = str(self.category).strip().upper()
        if category not in _SUPPORTED_CATEGORIES:
            raise ValueError(f"Unsupported news category: {category}")
        if not isinstance(self.published_at, datetime):
            raise ValueError("published_at must be a datetime.")
        if not isfinite(self.sentiment) or not -1 <= self.sentiment <= 1:
            raise ValueError("sentiment must be between -1 and 1.")
        if not isfinite(self.impact) or not 0 <= self.impact <= 1:
            raise ValueError("impact must be between 0 and 1.")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must include a timezone.")
        normalized = tuple(
            str(instrument).strip().upper()
            for instrument in self.instruments
            if str(instrument).strip()
        )
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "instruments", normalized)
        object.__setattr__(self, "url", http_url(self.url))
        object.__setattr__(self, "thumbnail_url", http_url(self.thumbnail_url))
        object.__setattr__(self, "video_url", http_url(self.video_url))
        summary = str(self.summary).strip() if isinstance(self.summary, str) else ""
        publisher = str(self.publisher).strip() if isinstance(self.publisher, str) else ""
        object.__setattr__(self, "summary", summary or None)
        object.__setattr__(self, "publisher", publisher or None)


def assess_news_impact(events, instrument, now=None, max_age_hours=72):
    """Aggregate normalized event assessments; does not fetch or infer news."""
    symbol = str(instrument).strip().upper()
    if not symbol:
        raise ValueError("Instrument symbol is required.")
    if not isfinite(max_age_hours) or max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive and finite.")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("now must include a timezone.")

    matched = []
    for event in events:
        if not isinstance(event, NewsEvent):
            raise TypeError("events must contain NewsEvent values.")
        age_hours = (
            current_time.astimezone(timezone.utc)
            - event.published_at.astimezone(timezone.utc)
        ).total_seconds() / 3600
        is_relevant = (
            not event.instruments
            or symbol in event.instruments
            or event.category in {"FED", "INFLATION", "EMPLOYMENT", "ECONOMIC", "GEOPOLITICAL"}
        )
        if 0 <= age_hours <= max_age_hours and is_relevant:
            recency_weight = max(0.0, 1 - age_hours / max_age_hours)
            weight = event.impact * recency_weight
            matched.append((event, weight))

    total_weight = sum(weight for _, weight in matched)
    bias = (
        sum(event.sentiment * weight for event, weight in matched) / total_weight
        if total_weight
        else 0.0
    )
    return {
        "status": "AVAILABLE" if matched else "NO_RELEVANT_EVENTS",
        "instrument": symbol,
        "event_count": len(matched),
        "impact_strength": min(1.0, total_weight),
        "sentiment_bias": max(-1.0, min(1.0, bias)),
        "categories": sorted({event.category for event, _ in matched}),
        "headlines": [event.title for event, _ in matched],
        "items": [news_clip(event) for event, _ in matched],
        "source": "caller-supplied normalized events; no external news provider configured",
    }


def news_clip(event):
    """Fields the feed actually carried. Missing ones stay None."""
    return {
        "title": event.title,
        "url": event.url,
        "summary": event.summary,
        "publisher": event.publisher,
        "published_at": event.published_at.isoformat(),
        "thumbnail_url": event.thumbnail_url,
        "video_url": event.video_url,
    }


def apply_news_context(analysis, news_impact, conflict_threshold=0.65):
    """Reduce confidence and abstain on a strong, materially adverse news event."""
    if not 0 <= conflict_threshold <= 1:
        raise ValueError("conflict_threshold must be between 0 and 1.")
    result = dict(analysis)
    result["news_impact"] = dict(news_impact)
    direction = result.get("signal")
    bias = news_impact.get("sentiment_bias", 0.0)
    strength = news_impact.get("impact_strength", 0.0)
    if direction not in {"BUY", "SELL"} or strength < conflict_threshold:
        return result

    adverse = (direction == "BUY" and bias < 0) or (direction == "SELL" and bias > 0)
    if adverse:
        result["signal"] = "WAIT"
        result["reason"] = (
            str(result.get("reason", ""))
            + " Strong opposing news context; directional recommendation withheld."
        ).strip()
        confidence = result.get("confidence")
        if isinstance(confidence, (int, float)):
            result["confidence"] = round(
                max(0.0, confidence * (1 - 0.5 * strength)),
                4,
            )
    return result


def get_news_status():
    return "NEWS ASSESSMENT AVAILABLE; EXTERNAL NEWS PROVIDER NOT CONFIGURED"
