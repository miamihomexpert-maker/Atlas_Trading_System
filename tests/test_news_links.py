from datetime import datetime, timezone

from atlas.dashboard import PaperDashboard
from atlas.database import AtlasDatabase
from atlas.news import NewsEvent, assess_news_impact, http_url, yahoo_profile_url, yahoo_quote_url
from atlas.news_feed import _event_from_yahoo
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


def _story(**overrides):
    content = {
        "id": "story-1",
        "contentType": "STORY",
        "title": "Fund beats a record",
        "summary": "The fund reported a record quarter.",
        "pubDate": "2026-11-06T15:00:00+00:00",
        "canonicalUrl": {"url": "https://example.com/story"},
        "clickThroughUrl": {"url": "javascript:alert(1)"},
        "previewUrl": "https://example.com/clip.mp4",
        "thumbnail": {"originalUrl": "https://example.com/thumb.jpg"},
        "provider": {"displayName": "Example Wire"},
    }
    content.update(overrides)
    return {"id": content["id"], "content": content}


def test_story_keeps_the_real_link_and_excerpt_and_drops_a_fake_video():
    event = _event_from_yahoo(_story(), "SPY")

    assert event.url == "https://example.com/story"
    assert event.summary == "The fund reported a record quarter."
    assert event.publisher == "Example Wire"
    assert event.thumbnail_url == "https://example.com/thumb.jpg"
    assert event.video_url is None


def test_video_url_is_kept_only_when_yahoo_marks_the_item_as_video():
    event = _event_from_yahoo(
        _story(contentType="VIDEO", previewUrl="https://example.com/clip.mp4"),
        "QQQ",
    )

    assert event.video_url == "https://example.com/clip.mp4"
    assert event.url == "https://example.com/story"


def test_missing_link_and_unsafe_urls_stay_empty():
    event = _event_from_yahoo(
        _story(
            canonicalUrl=None,
            clickThroughUrl={"url": "javascript:alert(1)"},
            summary="",
            thumbnail=None,
            provider={},
        ),
        "SPY",
    )

    assert event.url is None
    assert event.summary is None
    assert event.publisher is None
    assert event.thumbnail_url is None
    assert event.video_url is None
    assert http_url("javascript:alert(1)") is None
    assert yahoo_quote_url("SPY") == "https://finance.yahoo.com/quote/SPY"
    assert yahoo_profile_url("SPY") == "https://finance.yahoo.com/quote/SPY/profile"
    assert yahoo_quote_url("not a symbol") is None


def test_failed_feed_does_not_invent_clips_and_links_do_not_change_a_wait(tmp_path):
    now = datetime.now(timezone.utc)
    event = NewsEvent(
        event_id="story-1",
        title="Fund beats a record",
        category="OTHER",
        published_at=now,
        sentiment=0.0,
        impact=0.0,
        instruments=("XYZ",),
        url="https://example.com/story",
        summary="The fund reported a record quarter.",
        publisher="Example Wire",
    )
    impact = assess_news_impact([event], "XYZ", now=now)
    assert impact["headlines"] == ["Fund beats a record"]
    assert impact["items"][0]["url"] == "https://example.com/story"
    assert impact["items"][0]["summary"] == "The fund reported a record quarter."
    assert impact["items"][0]["video_url"] is None

    empty = assess_news_impact([], "XYZ", now=now)
    assert empty["headlines"] == []
    assert empty["items"] == []

    histories = {"XYZ": {"1h": recent_history(100, 1), "1d": recent_history(140, -1)}}
    database = AtlasDatabase(tmp_path / "news-links.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(instruments=("XYZ",), paper_initial_cash=10_000, live_trading_enabled=False),
        database=database,
        histories_by_symbol=histories,
        news_events=[event],
        candle_loader=lambda symbol, timeframe: histories[symbol][timeframe],
        company_loader=lambda symbol: {
            "symbol": symbol,
            "status": "FETCHED",
            "kind": "fund",
            "website": "javascript:alert(1)",
            "objective": "Tracks a test index.",
            "holdings": [],
            "sectors": [],
            "asset_classes": [],
            "allocation": [],
            "guidance": None,
        },
    )
    card = service.candidates()["scans"][0]
    assert card["decision"] == "WAIT"
    assert card["buyable"] is False
    assert card["news"]["items"][0]["url"] == "https://example.com/story"
    assert card["news"]["items"][0]["video_url"] is None
    assert card["links"]["quote"] == "https://finance.yahoo.com/quote/XYZ"
    assert card["links"]["profile"] == "https://finance.yahoo.com/quote/XYZ/profile"
    assert card["links"]["website"] is None
    database.close()


def test_relevant_news_tab_links_only_real_fields():
    from pathlib import Path

    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert "Relevant News" in page
    assert "The feed did not include a link." in page
    assert "The feed did not include an excerpt." in page
    assert "The feed did not include a company website." in page
    assert "Open the video" in page
    assert "Yahoo quote page" in page
    assert "youtube.com" not in page
