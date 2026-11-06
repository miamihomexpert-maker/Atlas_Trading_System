from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from atlas.broker import PaperBroker
from atlas.company import describe_company, load_company_facts
from atlas.dashboard import PaperDashboard
from atlas.database import AtlasDatabase
from atlas.pipeline import run_paper_pipeline
from atlas.session import _attach_company_facts, run_paper_session
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


class _Funds:
    def __init__(self, holdings, sectors, assets, overview=None, description=""):
        self.top_holdings = holdings
        self.sector_weightings = sectors
        self.asset_classes = assets
        self.fund_overview = overview or {}
        self.description = description

    def quote_type(self):
        return "ETF"


class _FundTicker:
    """ETF profile. Statement properties must not be read."""

    def __init__(self):
        self.info = {
            "quoteType": "ETF",
            "shortName": "Test Fund",
            "longBusinessSummary": "Tracks the test index.",
            "currency": "USD",
        }
        self.funds_data = _Funds(
            holdings=pd.DataFrame(
                {
                    "Name": ["Alpha Co", "Missing Weight", "Zero Weight"],
                    "Holding Percent": [0.08, None, 0.0],
                },
                index=pd.Index(["AAA", "BBB", "CCC"], name="Symbol"),
            ),
            sectors={"technology": 0.32, "energy": None},
            assets={"stockPosition": 0.99, "bondPosition": None},
            overview={
                "categoryName": "Large Blend",
                "family": "Test Family",
                "legalType": None,
            },
        )

    @property
    def cashflow(self):
        raise AssertionError("fund cash-flow statement was read")

    @property
    def income_stmt(self):
        raise AssertionError("fund income statement was read")


class _CompanyTicker:
    """Operating company. The fund profile must not be read."""

    def __init__(self):
        self.info = {
            "quoteType": "EQUITY",
            "shortName": "Widget Co",
            "longBusinessSummary": "Makes widgets.",
            "financialCurrency": "USD",
        }
        self.cashflow = pd.DataFrame(
            [[-1200.0], [float("nan")]],
            index=["Capital Expenditure", "Repurchase Of Capital Stock"],
            columns=[pd.Timestamp("2025-09-30")],
        )
        self.income_stmt = pd.DataFrame(
            [[400.0]],
            index=["Research And Development"],
            columns=[pd.Timestamp("2025-09-30")],
        )

    @property
    def funds_data(self):
        raise AssertionError("company fund profile was read")


class _BrokenTicker:
    @property
    def info(self):
        raise RuntimeError("yahoo down")


class _EmptyFund:
    def __init__(self):
        self.info = {"quoteType": "ETF"}
        self.funds_data = _Funds(
            holdings=pd.DataFrame(columns=["Name", "Holding Percent"]),
            sectors={},
            assets={},
            description="",
        )


class _PartialFund:
    info = {
        "quoteType": "ETF",
        "longBusinessSummary": "Tracks a test index.",
    }

    @property
    def funds_data(self):
        raise RuntimeError("top holdings unavailable")


def _facts(symbol, ticker, stamp):
    return load_company_facts(
        symbol,
        ticker_factory=lambda _symbol: ticker,
        now=stamp,
    )


def test_fund_facts_come_from_the_feed_and_skip_operating_lines():
    stamp = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
    profile = _facts("FUND", _FundTicker(), stamp)
    copy = describe_company(profile)

    assert profile["status"] == "FETCHED"
    assert profile["kind"] == "fund"
    assert profile["quote_type"] == "ETF"
    assert profile["objective"] == "Tracks the test index."
    assert profile["name"] == "Test Fund"
    assert profile["category"] == "Large Blend"
    assert profile["family"] == "Test Family"
    assert profile["legal_type"] is None
    assert profile["guidance"] is None
    assert profile["allocation"] == []
    assert profile["fetched_at"] == stamp.isoformat()
    assert profile["source"] == "yfinance Ticker.info; yfinance Ticker.funds_data"
    assert profile["holdings"] == [
        {"symbol": "AAA", "name": "Alpha Co", "weight": 0.08},
        {"symbol": "CCC", "name": "Zero Weight", "weight": 0.0},
    ]
    assert profile["sectors"] == [{"key": "technology", "name": "Technology", "weight": 0.32}]
    assert profile["asset_classes"] == [{"key": "stockPosition", "name": "Stocks", "weight": 0.99}]
    text = " ".join(copy["lines"])
    assert "Alpha Co (AAA) is 8.00% of the fund." in text
    assert "Technology is 32.00% of the fund." in text
    assert "Stocks is 99.00% of the fund." in text
    assert "not shown" in copy["summary"]
    assert "BBB" not in text


def test_operating_company_shows_only_returned_statement_rows():
    stamp = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
    profile = _facts("CORP", _CompanyTicker(), stamp)
    copy = describe_company(profile)
    text = " ".join(copy["lines"])

    assert profile["kind"] == "company"
    assert profile["guidance"] is None
    assert profile["holdings"] == []
    assert profile["sectors"] == []
    assert [(item["key"], item["amount"], item["period"], item["row"]) for item in profile["allocation"]] == [
        ("capex", -1200.0, "2025-09-30", "Capital Expenditure"),
        ("rnd", 400.0, "2025-09-30", "Research And Development"),
    ]
    assert "Capital expenditure on the cash-flow statement for the period ending 2025-09-30 was -1,200.00 USD." in text
    assert "Research and development on the income statement for the period ending 2025-09-30 was 400.00 USD." in text
    assert "No share-repurchase amount was returned." in copy["missing"]
    assert "No dividend amount was returned." in copy["missing"]
    assert "No business-purchase amount was returned." in copy["missing"]
    assert "No management guidance figure was returned." in copy["missing"]
    assert "of the fund" not in text


def test_failed_and_empty_company_feeds_stay_blank():
    failed = _facts("FUND", _BrokenTicker(), datetime(2026, 11, 6, tzinfo=timezone.utc))
    failed_copy = describe_company(failed)
    assert failed["status"] == "FETCH_FAILED"
    assert failed["error"] == "yahoo down"
    assert failed["holdings"] == []
    assert failed["allocation"] == []
    assert failed["guidance"] is None
    assert failed_copy["lines"] == []
    assert failed_copy["objective"] is None
    assert "yahoo down" in " ".join(failed_copy["missing"])
    assert "panel is empty" in failed_copy["summary"]

    empty = _facts("FUND", _EmptyFund(), datetime(2026, 11, 6, tzinfo=timezone.utc))
    empty_copy = describe_company(empty)
    assert empty["status"] == "EMPTY"
    assert empty["kind"] == "fund"
    assert empty["objective"] is None
    assert empty["holdings"] == []
    assert empty["allocation"] == []
    assert "no objective" in empty_copy["summary"]
    assert "not shown" in empty_copy["summary"]

    partial = _facts("FUND", _PartialFund(), datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert partial["status"] == "PARTIAL"
    assert partial["objective"] == "Tracks a test index."
    assert partial["holdings"] == []
    assert partial["allocation"] == []
    assert "top holdings unavailable" in partial["error"]


def test_company_facts_do_not_invent_a_buy(tmp_path):
    histories = {
        "XYZ": {"1h": recent_history(100, 1), "1d": recent_history(140, -1)},
    }
    broker = PaperBroker(initial_cash=10_000)
    preview = run_paper_pipeline(
        "XYZ",
        histories["XYZ"],
        broker,
        "preview-company",
        approved=False,
        news_events=[],
        execute=False,
    )
    assert "company" not in preview
    assert preview["decision"] == "WAIT"
    assert preview["technical_signal"] == "WAIT"

    database = AtlasDatabase(tmp_path / "company.sqlite3")
    summary = run_paper_session(
        settings=Settings(instruments=("XYZ",), paper_initial_cash=10_000, live_trading_enabled=False),
        broker=PaperBroker(initial_cash=10_000, database=database),
        database=database,
        histories_by_symbol=histories,
        news_events=[],
        approved=True,
        timeframes=("1h", "1d"),
        company_loader=lambda symbol: _facts(symbol, _FundTicker(), datetime(2026, 11, 6, tzinfo=timezone.utc)),
    )
    result = summary["results"][0]
    assert [item["symbol"] for item in summary["results"]] == ["XYZ"]
    assert result["decision"] == "WAIT"
    assert result["technical_signal"] == "WAIT"
    assert result["order"] is None
    assert result["company"]["holdings"][0]["weight"] == 0.08
    assert result["company"]["allocation"] == []
    assert "BUY" not in (result.get("analysis") or {}).get("reason", "")
    database.close()

    extra = [{"symbol": "XYZ", "decision": "WAIT"}, {"symbol": "EXTRA", "decision": "SELL"}]
    _attach_company_facts(
        extra,
        ("XYZ",),
        lambda symbol: _facts(symbol, _FundTicker(), datetime(2026, 11, 6, tzinfo=timezone.utc)),
    )
    assert extra[0]["decision"] == "WAIT"
    assert extra[0]["company"]["symbol"] == "XYZ"
    assert extra[0]["company"]["holdings"][0]["weight"] == 0.08
    assert "company" not in extra[1]


def test_dashboard_company_section_uses_the_feed(tmp_path):
    histories = {"XYZ": {"1h": recent_history(100, 1), "1d": recent_history(140, -1)}}
    database = AtlasDatabase(tmp_path / "dash-company.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(instruments=("XYZ",), paper_initial_cash=10_000, live_trading_enabled=False),
        database=database,
        histories_by_symbol=histories,
        news_events=[],
        candle_loader=lambda symbol, timeframe: histories[symbol][timeframe],
        company_loader=lambda symbol: _facts(
            symbol,
            _FundTicker(),
            datetime(2026, 11, 6, tzinfo=timezone.utc),
        ),
    )
    ranked = service.candidates()
    assert [card["symbol"] for card in ranked["scans"]] == ["XYZ"]
    card = ranked["scans"][0]
    assert card["decision"] == "WAIT"
    assert card["buyable"] is False
    assert card["company"]["objective"] == "Tracks the test index."
    assert card["company"]["holdings"][0] == {
        "symbol": "AAA",
        "name": "Alpha Co",
        "weight": 0.08,
    }
    assert "8.00%" in " ".join(card["company_copy"]["lines"])
    assert card["company_copy"]["source"].startswith("yfinance Ticker.info")
    assert card["company_copy"]["fetched_at"]
    with pytest.raises(ValueError, match="not a current BUY"):
        service.buy(service.token, "XYZ")
    assert service._broker().positions() == {}
    database.close()

    page = Path("atlas/dashboard_page.html").read_text(encoding="utf-8")
    assert 'id="company"' in page
    assert "company_copy" in page
    assert "renderCompany" in page
