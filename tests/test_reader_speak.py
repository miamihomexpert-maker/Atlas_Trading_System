"""The reading pane speaks a summary. It does not read source rows."""

from pathlib import Path
import re


PAGE = Path("atlas/dashboard_page.html")


def _function_body(page, name):
    marker = f"function {name}"
    start = page.index(marker)
    open_brace = page.index("{", start)
    depth = 0
    for index in range(open_brace, len(page)):
        char = page[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return page[start:index + 1]
    raise AssertionError(f"{name} was not closed")


def _prediction_speak(page, scan):
    """Run the page's prediction speech rule: headline and why, nothing else."""
    body = _function_body(page, "predictionSpeakText")
    for banned in (
        "sources",
        "players",
        "findings",
        "holders",
        "insiders",
        "activity",
        "volume",
        "clip",
        "tendencies",
        "track_record",
        "disclaimer",
        "gate",
    ):
        assert banned not in body
    assert 'return [lead, why].filter(Boolean).join(" ");' in body
    assert "prediction.lead" in body
    assert "flow.why" in body
    if not scan:
        match = re.search(r'if \(!scan\) \{\s*return "([^"]+)";', body)
        assert match, body
        return match.group(1)
    lead_fallback = re.search(r'prediction\.lead \|\| "([^"]+)"', body).group(1)
    why_fallback = re.search(r'flow\.why \|\| "([^"]+)"', body).group(1)
    prediction = scan.get("prediction") or {}
    flow = prediction.get("flow") or {}
    lead = str(prediction.get("lead") or lead_fallback).strip()
    why = str(flow.get("why") or why_fallback).strip()
    return " ".join(part for part in (lead, why) if part)


def test_speak_button_reads_the_prediction_summary_and_skips_source_rows():
    page = PAGE.read_text(encoding="utf-8")
    assert 'name="atlas-dashboard-version" content="27"' in page
    bar = page[page.index('<div class="reader-bar">'):page.index('<div class="reader-body">')]
    assert 'id="reader-speak"' in bar
    assert bar.index('id="reader-speak"') < bar.index('id="reader-close"')
    assert "hidden" in bar
    assert "icon-speak" in bar
    assert "icon-stop" in bar
    assert 'aria-label="Read this summary aloud"' in bar

    speak_css = page[page.index("#reader-speak {"):page.index("#reader-speak[hidden]")]
    assert "min-width: 44px;" in speak_css
    assert "min-height: 44px;" in speak_css
    assert "padding: 11px 16px;" in speak_css
    assert "font-size: 18px;" in speak_css
    assert ".speaking .icon-stop { display: block; }" in page
    assert ".speaking .icon-speak { display: none; }" in page

    assert "speechSynthesis" in page
    assert "SpeechSynthesisUtterance" in page
    start = _function_body(page, "startSpeaking")
    assert "speechSynthesis.speak" in start
    assert "readerSpeakText()" in start
    assert "fetch(" not in start
    assert "/api/" not in start
    assert "stopSpeaking()" in _function_body(page, "applyTab")
    assert "stopSpeaking()" in _function_body(page, "focusSymbol")
    sync = _function_body(page, "syncSpeakButton")
    assert "speechReady()" in sync
    assert "button.hidden" in sync
    assert 'classList.contains("speaking")' in page

    render = _function_body(page, "renderPredictions")
    assert render.count('class="lead speak"') == 2
    assert 'class="clip"' in render
    assert "prediction.sources" in render
    assert "flow.findings" in render or "flow.findings || []" in render

    spoken = _prediction_speak(page, {
        "prediction": {
            "lead": "The public rows lean toward a lower close.",
            "disclaimer": "DISCLAIMER_ROW this is not the bold headline.",
            "flow": {
                "why": "The labeled share counts are mostly selling.",
                "findings": [{
                    "summary": "SOURCE_ROW Ada Example disclosed a sale of 800 shares.",
                    "source": "yahoo_insider_transaction",
                    "date": "2026-11-03",
                }],
                "players": [{
                    "name": "PLAYER_ROW Bea Example",
                    "motive_note": "MOTIVE_ROW the feed did not include a motive.",
                }],
                "activity": {"note": "ACTIVITY_NOTE percent of labeled shares."},
                "volume": {"note": "VOLUME_NOTE compared with a typical bar."},
            },
            "sources": [{"summary": "FILING_ROW a later bar was stored."}],
            "past": {"note": "PAST_ROW of 9 later bars."},
            "present": {"note": "PRESENT_ROW disclosed sales.", "holder_note": "HOLDER_ROW institutions."},
            "tendencies": [{"summary": "TENDENCY_ROW repeated disclosed sales."}],
            "track_record": {"reason": "TRACK_ROW not enough graded leans."},
            "gate": {"reason": "GATE_ROW the scan stays a wait."},
            "investors": {
                "note": "INVESTOR_ROW names from the feed.",
                "investors": [{"name": "Ada Example", "movements": [{"comparison_note": "MOVEMENT_ROW"}]}],
            },
        }
    })
    assert spoken == (
        "The public rows lean toward a lower close. "
        "The labeled share counts are mostly selling."
    )
    for leaked in (
        "DISCLAIMER_ROW",
        "SOURCE_ROW",
        "PLAYER_ROW",
        "MOTIVE_ROW",
        "ACTIVITY_NOTE",
        "VOLUME_NOTE",
        "FILING_ROW",
        "PAST_ROW",
        "PRESENT_ROW",
        "HOLDER_ROW",
        "TENDENCY_ROW",
        "TRACK_ROW",
        "GATE_ROW",
        "INVESTOR_ROW",
        "MOVEMENT_ROW",
        "800 shares",
    ):
        assert leaked not in spoken


def test_other_text_tabs_speak_a_summary_and_not_the_rows():
    page = PAGE.read_text(encoding="utf-8")
    reader = _function_body(page, "readerSpeakText")
    for tab in ("predictions", "investors", "signals", "company", "behavior", "risk", "review"):
        assert tab in reader
    assert "fetch(" not in reader
    assert ".clip" not in reader
    assert "review.fills" not in reader

    investors = _function_body(page, "investorSpeakText")
    assert "board.note" in investors
    assert "movements" not in investors

    signals = _function_body(page, "signalsSpeakText")
    assert "copy.decision" in signals
    assert "copy.why" in signals
    assert "timeframes" not in signals

    company = _function_body(page, "companySpeakText")
    assert "copy.summary" in company
    assert ".lines" not in company

    behavior = _function_body(page, "behaviorSpeakText")
    assert "behavior.disclaimer" in behavior
    assert "behavior.note" in behavior
    assert "insiders" not in behavior
    assert "holders" not in behavior

    assert "state.riskSpeak" in reader
    assert "state.reviewSpeak" in reader
    risk = _function_body(page, "renderRisk")
    assert "These are the paper account's limits. A buy is checked again before it is sent." in risk
    assert "state.riskSpeak" in risk
    review = _function_body(page, "renderReview")
    assert 'state.reviewSpeak = String(review.scoreboard_note || "No paper fills to review yet.")' in review
