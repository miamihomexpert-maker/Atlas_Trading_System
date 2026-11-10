from datetime import datetime, timezone

import pandas as pd
import pytest

from atlas.baseline import ensure_baseline_model
from atlas.broker import PaperBroker
from atlas.dashboard import PaperDashboard
from atlas.database import AtlasDatabase
from atlas.news import NewsEvent
from atlas.news_feed import classify_category, lexicon_sentiment, resolve_news
from atlas.pipeline import run_paper_pipeline
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


def _bars(start, step, rows=40):
    frame = recent_history(start, step, rows=rows)
    return {"1h": frame, "1d": frame.copy()}


def _settings():
    return Settings(instruments=("XYZ", "ABC", "DEF"), paper_initial_cash=10_000)


def test_lexicon_scores_only_listed_words():
    positive, positive_impact, positive_hits = lexicon_sentiment("Company beats earnings")
    negative, negative_impact, negative_hits = lexicon_sentiment("Fraud investigation announced")
    unscored, unscored_impact, unscored_hits = lexicon_sentiment("Quarterly update published")

    assert positive > 0 and positive_hits == 1 and positive_impact > 0
    assert negative < 0 and negative_hits == 2
    assert unscored == 0 and unscored_impact == 0 and unscored_hits == 0
    assert classify_category("CPI rises") == "INFLATION"
    assert classify_category("warning on supply") == "OTHER"


def test_news_file_override_skips_the_feed(tmp_path, monkeypatch):
    path = tmp_path / "news.json"
    path.write_text(
        """
        [{
          "event_id": "file-1",
          "title": "Operator supplied headline",
          "category": "EARNINGS",
          "published_at": "2026-11-06T15:00:00+00:00",
          "sentiment": 0,
          "impact": 0,
          "instruments": ["XYZ"]
        }]
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("ATLAS_NEWS_FILE", str(path))

    def fail_factory(_symbol):
        raise AssertionError("file override must not fetch")

    events, report = resolve_news(["XYZ"], ticker_factory=fail_factory)

    assert report["status"] == "FILE"
    assert events[0].title == "Operator supplied headline"
    assert events[0].sentiment == 0


def test_news_fetch_failure_is_recorded_and_not_invented(monkeypatch):
    monkeypatch.delenv("ATLAS_NEWS_FILE", raising=False)

    class Broken:
        @property
        def news(self):
            raise RuntimeError("feed down")

    events, report = resolve_news(["SPY"], ticker_factory=lambda _symbol: Broken())

    assert events == []
    assert report["status"] == "FETCH_FAILED"
    assert "feed down" in report["error"]

    broker = PaperBroker(initial_cash=10_000)
    result = run_paper_pipeline(
        "XYZ",
        _bars(100, 1),
        broker,
        "preview",
        approved=False,
        news_events=[],
        news_report=report,
        execute=False,
    )
    assert result["news"]["status"] == "FETCH_FAILED"
    assert result["news"]["headlines"] == []
    assert result["decision"] == "BUY"


def test_unpromoted_model_does_not_invent_a_buy(tmp_path):
    database = AtlasDatabase(tmp_path / "short.sqlite3")
    short = pd.DataFrame(
        {
            "Open": [1] * 10,
            "High": [1] * 10,
            "Low": [1] * 10,
            "Close": [1] * 10,
            "Volume": [1] * 10,
        }
    )
    report = ensure_baseline_model(
        database,
        ["XYZ"],
        history_loader=lambda _symbol: short,
    )
    assert report["status"] == "INSUFFICIENT_HISTORY"
    assert database.active_model() is None

    rising = _bars(100, 1)
    broker = PaperBroker(initial_cash=10_000, database=database)
    preview = run_paper_pipeline(
        "XYZ",
        rising,
        broker,
        "still-technical",
        approved=False,
        database=database,
        news_events=[],
        execute=False,
    )
    assert preview["decision"] == "BUY"
    assert preview["adaptive"]["status"] == "INSUFFICIENT_HISTORY"
    database.close()


def test_active_model_wait_blocks_buy_and_agreement_keeps_it(tmp_path):
    database = AtlasDatabase(tmp_path / "models.sqlite3")
    model = {
        "coefficients": [0, 0, 0, 0, 0, 0, 0, 0],
        "mean": [0] * 7,
        "scale": [1] * 7,
        "threshold": 0.55,
    }
    database.save_model_candidate(
        "flat",
        {
            "sample_count": 80,
            "accuracy": 0.6,
            "net_return_pct": 1,
            "maximum_drawdown": 0.1,
        },
        model,
    )
    assert database.promote_model("flat")["promoted"]
    broker = PaperBroker(initial_cash=10_000, database=database)
    waited = run_paper_pipeline(
        "XYZ",
        _bars(100, 1),
        broker,
        "model-wait",
        approved=False,
        database=database,
        news_events=[],
        execute=False,
    )
    assert waited["technical_signal"] == "BUY"
    assert waited["adaptive"]["prediction"]["signal"] == "WAIT"
    assert waited["decision"] == "WAIT"
    assert waited["recommendation"] is None

    database._connection.execute("DELETE FROM atlas_metadata WHERE key = 'active_model'")
    database._connection.execute(
        "UPDATE model_versions SET status = 'archived' WHERE version = 'flat'"
    )
    database._connection.commit()
    bullish = {
        "coefficients": [8, 0, 0, 0, 0, 0, 0, 0],
        "mean": [0] * 7,
        "scale": [1] * 7,
        "threshold": 0.55,
    }
    database.save_model_candidate(
        "up",
        {
            "sample_count": 80,
            "accuracy": 0.6,
            "net_return_pct": 2,
            "maximum_drawdown": 0.1,
            "candidate_comparison": {
                "sample_count": 80,
                "accuracy": 0.6,
                "net_return_pct": 2,
                "maximum_drawdown": 0.05,
            },
            "incumbent_comparison": {
                "version": "flat",
                "sample_count": 80,
                "accuracy": 0.55,
                "net_return_pct": 1,
                "maximum_drawdown": 0.1,
            },
        },
        bullish,
    )
    assert database.promote_model("up")["promoted"]
    confirmed = run_paper_pipeline(
        "XYZ",
        _bars(100, 1),
        broker,
        "model-buy",
        approved=False,
        database=database,
        news_events=[],
        execute=False,
    )
    assert confirmed["decision"] == "BUY"
    assert confirmed["recommendation"]["direction"] == "BUY"
    assert confirmed["adaptive"]["status"] == "ACTIVE"
    database.close()


def test_dashboard_buy_list_stays_empty_when_nothing_qualifies(tmp_path):
    histories = {
        "XYZ": _bars(140, -1),
        "ABC": _bars(100, 0),
    }
    database = AtlasDatabase(tmp_path / "empty.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(instruments=("XYZ", "ABC"), paper_initial_cash=10_000),
        database=database,
        histories_by_symbol=histories,
        news_events=[],
        hard_stop=False,
    )
    ranked = service.candidates()
    assert ranked["candidates"] == []
    assert ranked["qualified_count"] == 0
    assert "XYZ" in ranked["explanation"]
    assert "ABC" in ranked["explanation"]
    by_symbol = {card["symbol"]: card for card in ranked["scans"]}
    assert set(by_symbol) == {"XYZ", "ABC"}
    assert by_symbol["XYZ"]["buyable"] is False
    assert by_symbol["ABC"]["buyable"] is False
    assert by_symbol["XYZ"]["reason"]
    assert by_symbol["XYZ"]["timeframes"]
    if by_symbol["XYZ"]["decision"] == "SELL":
        assert by_symbol["XYZ"]["shortable"] is True
        assert isinstance(by_symbol["XYZ"]["entry"], float)
    else:
        assert by_symbol["XYZ"]["entry"] is None
        assert by_symbol["XYZ"]["shortable"] is False
    database.close()


def test_dashboard_shows_a_confirmed_buy_without_padding(tmp_path):
    histories = {"XYZ": _bars(100, 1)}
    database = AtlasDatabase(tmp_path / "one.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(
            instruments=("XYZ", "ABC", "DEF"),
            paper_initial_cash=10_000,
        ),
        database=database,
        histories_by_symbol=histories,
        news_events=[
            NewsEvent(
                event_id="n1",
                title="Unscored headline",
                category="OTHER",
                published_at=datetime.now(timezone.utc),
                sentiment=0,
                impact=0,
                instruments=("XYZ",),
            )
        ],
        hard_stop=False,
    )
    ranked = service.candidates()
    assert [item["symbol"] for item in ranked["candidates"]] == ["XYZ"]
    assert ranked["candidates"][0]["quantity"] > 0
    assert ranked["candidates"][0]["entry"] > ranked["candidates"][0]["stop"]
    buy = next(card for card in ranked["scans"] if card["symbol"] == "XYZ")
    assert buy["buyable"] is True
    assert buy["news"]["headlines"] == ["Unscored headline"]
    missing = {card["symbol"]: card for card in ranked["scans"]}
    assert missing["ABC"]["decision"] == "NO_DATA"
    assert missing["DEF"]["decision"] == "NO_DATA"
    assert missing["ABC"]["buyable"] is False
    assert "ABC" in ranked["explanation"] or "DEF" in ranked["explanation"]
    database.close()
