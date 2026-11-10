from atlas.ranking import card_from_pipeline, coverage_note, rank_scans, top_scans
from atlas.risk import RiskLimits
from atlas.universe import UNIVERSE_SOURCE, load_universe, next_batch, resolve_instruments


def _card(symbol, decision, buyable=False, confidence=None):
    return {
        "symbol": symbol,
        "decision": decision,
        "buyable": buyable,
        "confidence": confidence,
        "price": 10.0,
    }


def test_universe_file_is_the_sp500_constituent_list():
    symbols = load_universe()
    text = open("config/sp500.txt", encoding="utf-8").read()
    assert "wikipedia.org/wiki/List_of_S%26P_500_companies" in text
    assert "not a forecast" in text
    assert len(symbols) >= 500
    assert len(symbols) == len(set(symbols))
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "BRK-B" in symbols
    assert "SPY" not in symbols
    assert "QQQ" not in symbols
    assert UNIVERSE_SOURCE.startswith("S&P 500")


def test_explicit_symbol_list_still_overrides(monkeypatch):
    monkeypatch.setenv("ATLAS_INSTRUMENTS", "XYZ,ABC")
    assert resolve_instruments() == ("XYZ", "ABC")
    monkeypatch.delenv("ATLAS_INSTRUMENTS")
    assert resolve_instruments() == load_universe()


def test_batches_walk_the_full_list_without_dropping_names():
    symbols = ["A", "B", "C", "D", "E"]
    first, index = next_batch(symbols, 0, 2)
    second, index = next_batch(symbols, index, 2)
    third, index = next_batch(symbols, index, 2)
    assert first == ["A", "B"]
    assert second == ["C", "D"]
    assert third == ["E", "A"]
    assert index == 1
    seen = []
    cursor = 0
    for _ in range(5):
        batch, cursor = next_batch(symbols, cursor, 2)
        seen.extend(batch)
    assert set(symbols) <= set(seen)


def test_ranking_uses_scan_fields_and_does_not_invent_a_return():
    cards = [
        _card("WAIT1", "WAIT", confidence=0.9),
        _card("BUY0", "BUY", buyable=False, confidence=0.2),
        _card("SELL1", "SELL", confidence=0.99),
        _card("BUY1", "BUY", buyable=True, confidence=0.4),
        _card("BUY2", "BUY", buyable=True, confidence=0.8),
    ]
    before = [set(card) for card in cards]
    ranked = rank_scans(cards)
    for card, keys in zip(cards, before):
        assert set(card) == keys
        assert "expected_return" not in card
    assert [card["symbol"] for card in ranked] == ["BUY2", "BUY1", "BUY0", "WAIT1", "SELL1"]
    shown = top_scans(ranked)
    assert [card["symbol"] for card in shown] == ["BUY2", "BUY1", "BUY0"]
    assert shown[2]["buyable"] is False


def test_fewer_than_three_buys_keeps_the_next_real_results():
    cards = [
        _card("ONE", "BUY", buyable=True, confidence=0.5),
        _card("HOLD", "WAIT", confidence=0.4),
        _card("OUT", "SELL", confidence=0.7),
    ]
    shown = top_scans(cards)
    assert [card["symbol"] for card in shown] == ["ONE", "HOLD", "OUT"]
    assert shown[1]["decision"] == "WAIT"
    assert shown[1]["buyable"] is False
    note = coverage_note(503, 20, 1, 20)
    assert "not been scanned yet" in note
    assert "not a forecast of profit" in note.lower()
    assert "risk-sizable buy" in note
    empty = coverage_note(503, 20, 0, 20)
    assert "Buy stays off" in empty
    assert RiskLimits().risk_per_trade_fraction == 0.01
    assert RiskLimits().max_position_fraction == 0.10
    assert RiskLimits().max_daily_loss_fraction == 0.03


def test_pipeline_card_sizes_a_buy_with_the_existing_risk_limit():
    card = card_from_pipeline(
        {
            "symbol": "AAPL",
            "decision": "BUY",
            "analysis": {"price": 100.0, "reason": "Timeframes agree."},
            "recommendation": {
                "entry_price": 100.0,
                "stop_loss": 90.0,
                "profit_target": 120.0,
            },
            "scanned_at": "2026-11-10T15:00:00+00:00",
        },
        equity=10_000,
        exposure=0,
    )
    assert card["buyable"] is True
    assert card["quantity"] > 0
    assert card["decision"] == "BUY"
    waiting = card_from_pipeline(
        {"symbol": "MSFT", "decision": "WAIT", "analysis": {"price": 50, "reason": "Wait."}},
        equity=10_000,
        exposure=0,
    )
    assert waiting["decision"] == "WAIT"
    assert waiting["buyable"] is False
    assert waiting["quantity"] is None
