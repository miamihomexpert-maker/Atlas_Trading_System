from datetime import datetime, timezone

from atlas.learning import assess_track_record, build_investor_board, build_prediction


def _lean(direction, when):
    return {
        "source": "prediction_lean",
        "source_key": f"lean-{when}",
        "learned_at": when,
        "effect": "context_only",
        "summary": "Stored lean.",
        "detail": {"direction": direction, "reference_time": when, "reference_price": 100.0},
    }


def _insider(name, action, when, shares=10, url="https://example.com/form4"):
    return {
        "source": "yahoo_insider_transaction",
        "source_key": f"{name}|{action}|{when}",
        "learned_at": when,
        "effect": "context_only",
        "summary": f"{name} disclosed {action}. The feed did not include a motive.",
        "detail": {
            "row": {
                "insider": name,
                "transaction": "Purchase" if action == "buy" else "Sale",
                "date": when,
                "shares": shares,
                "url": url,
            }
        },
    }


def _bars():
    return [
        {"time": "2026-11-02T00:00:00+00:00", "high": 101, "low": 99, "close": 100, "volume": 1000},
        {"time": "2026-11-04T00:00:00+00:00", "high": 112, "low": 108, "close": 110, "volume": 1000},
    ]


def test_a_disclosed_purchase_lines_up_with_an_up_lean():
    board = build_investor_board(
        [_lean("up", "2026-11-01T00:00:00+00:00"), _insider("Ada Example", "buy", "2026-11-03T00:00:00+00:00")],
        _bars(),
    )
    movement = board["investors"][0]["movements"][0]
    assert board["investors"][0]["name"] == "Ada Example"
    assert movement["comparison"] == "lined_up"
    assert movement["in_track_record"] is True
    assert movement["track_matched"] is True
    assert movement["motive"] is None
    assert "motive" in movement["motive_note"].lower()
    assert "delayed" in movement["filings_note"]


def test_a_disclosed_sale_differs_from_an_up_lean():
    board = build_investor_board(
        [_lean("up", "2026-11-01T00:00:00+00:00"), _insider("Ada Example", "sell", "2026-11-03T00:00:00+00:00")],
        _bars(),
    )
    movement = board["investors"][0]["movements"][0]
    assert movement["comparison"] == "differed"
    assert movement["in_track_record"] is True
    assert movement["track_matched"] is False


def test_a_movement_without_a_name_or_date_is_left_out():
    nameless = _insider("Ada Example", "buy", "2026-11-03T00:00:00+00:00")
    nameless["detail"]["row"]["insider"] = None
    undated = _insider("Bea Example", "buy", "2026-11-03T00:00:00+00:00")
    undated["detail"]["row"]["date"] = None
    board = build_investor_board(
        [_lean("up", "2026-11-01T00:00:00+00:00"), nameless, undated],
        _bars(),
    )
    assert board["investors"] == []
    assert "None is filled in" in board["note"]


def test_a_later_bar_moves_a_comparison_into_the_track_record():
    events = [
        _lean("up", "2026-11-01T00:00:00+00:00"),
        _insider("Ada Example", "buy", "2026-11-03T00:00:00+00:00"),
    ]
    before = build_prediction("XYZ", events, bars=[
        {"time": "2026-11-02T00:00:00+00:00", "high": 101, "low": 99, "close": 100, "volume": 1000},
    ])
    assert before["investors"]["investors"][0]["movements"][0]["in_track_record"] is False
    assert before["track_record"]["graded"] == 0
    assert before["changes_decision"] is False

    after = build_prediction("XYZ", events, bars=_bars())
    movement = after["investors"]["investors"][0]["movements"][0]
    assert movement["comparison"] == "lined_up"
    assert movement["outcome"]["move"] == "up"
    assert after["track_record"]["graded"] == 1
    assert after["track_record"]["correct"] == 1
    assert after["track_record"]["passed"] is False


def test_investors_tab_is_present_and_an_empty_feed_adds_no_rows():
    page = open("atlas/dashboard_page.html", encoding="utf-8").read()
    assert 'data-tab="investors"' in page
    assert 'data-panel="investors"' in page
    assert 'data-tab="predictions"' in page
    assert "None is filled in" in page
    assert 'content="29"' in page
    prediction = build_prediction("XYZ", [])
    assert prediction["investors"]["investors"] == []
    assert prediction["flow"]["players"] == []
    assert prediction["flow"]["activity"]["sell_percent"] is None
    assert "no percentage" in prediction["flow"]["activity"]["note"]
    assert prediction["changes_decision"] is False


def test_large_selling_keeps_its_source_and_a_sale_without_context_has_no_why():
    sale = _insider("Ada Example", "sell", "2026-11-03T00:00:00+00:00", shares=800)
    explained = build_prediction("XYZ", [sale], news_items=[{
        "title": "Holder trims a stake",
        "url": "https://example.com/story",
        "published_at": "2026-11-03T12:00:00+00:00",
    }])
    assert explained["flow"]["large_side"] == "selling"
    assert explained["flow"]["activity"]["sell_percent"] == 100
    assert explained["flow"]["why_found"] is True
    assert any(item["url"] == "https://example.com/story" for item in explained["flow"]["findings"])
    assert explained["flow"]["players"][0]["motive"] is None

    unexplained = build_prediction("XYZ", [sale])
    assert unexplained["flow"]["why_found"] is False
    assert "No public reason was returned" in unexplained["flow"]["why"]
    assert "did not include a motive" in unexplained["flow"]["why"]


def test_track_record_counts_do_not_pass_on_one_investor_comparison():
    record = assess_track_record([], [{"matched": True, "learned_at": "2026-11-04T00:00:00+00:00"}])
    assert record["graded"] == 1
    assert record["passed"] is False
    assert "insufficient" in record["reason"].lower()
