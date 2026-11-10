"""The paper dashboard keeps the symbol and chart tab the user was viewing."""

from pathlib import Path
import json
import subprocess
import threading
from urllib.request import urlopen

import pandas as pd
import pytest

from atlas.config import LIVE_TRADING
from atlas.dashboard import PaperDashboard, make_server
from atlas.database import AtlasDatabase
from atlas.risk import RiskLimits
from atlas.session import resolve_timeframes
from config.settings import Settings
from tests.test_paper_pipeline import recent_history


PAGE = Path("atlas/dashboard_page.html")
ROOT = Path(__file__).resolve().parents[1]


def _page():
    return PAGE.read_text(encoding="utf-8")


def _selection_source():
    page = _page()
    start = page.index('const SELECTION_KEY = "atlas-paper-selection";')
    end = page.index("const LAYOUT_DEFAULTS")
    return page[start:end]


def _run_node(script):
    completed = subprocess.run(
        ["node"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout


def test_dashboard_script_parses(tmp_path):
    page = _page()
    script = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    path = tmp_path / "dashboard.js"
    path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        ["node", "--check", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_remembered_symbol_and_timeframe_survive_reload_and_refresh():
    script = _selection_source() + r"""
const store = {};
const localStorage = {
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  },
  setItem(key, value) { store[key] = String(value); },
};
const state = { symbol: null, timeframe: null };
const frames = ["1m", "1h", "1d"];
const cards = ["ABT", "ADBE", "AFL"];
const watchlist = ["ADBE", "ABT", "AFL", "A"];
const results = [];
function check(name, actual, expected) {
  results.push({
    name,
    actual,
    expected,
    ok: JSON.stringify(actual) === JSON.stringify(expected),
  });
}

state.symbol = "ADBE";
state.timeframe = "1h";
saveSelection();
state.symbol = "AFL";
state.timeframe = "1d";
saveSelection();
const restored = loadSelection();
check("click stores the latest symbol and timeframe", restored, {
  symbol: "AFL",
  timeframe: "1d",
});
check(
  "reload keeps a symbol that is still on the watchlist",
  resolveSelection(restored.symbol, restored.timeframe, watchlist, cards, frames),
  { symbol: "AFL", timeframe: "1d" },
);
check(
  "a watchlist name outside the top cards stays selected",
  resolveSelection("A", "1h", watchlist, cards, frames),
  { symbol: "A", timeframe: "1h" },
);
localStorage.setItem(SELECTION_KEY, JSON.stringify({ symbol: "afl", timeframe: "1h" }));
check(
  "lowercase storage still matches the watchlist symbol",
  resolveSelection(loadSelection().symbol, loadSelection().timeframe, watchlist, cards, frames),
  { symbol: "AFL", timeframe: "1h" },
);

localStorage.setItem(SELECTION_KEY, JSON.stringify({ symbol: " adbe ", timeframe: "1h" }));
check("stored text is cleaned on read", loadSelection(), { symbol: "ADBE", timeframe: "1h" });
check(
  "refresh keeps the cleaned symbol and the chosen tab",
  resolveSelection(loadSelection().symbol, loadSelection().timeframe, watchlist, cards, frames),
  { symbol: "ADBE", timeframe: "1h" },
);
check("share-class symbols stay readable", cleanSymbol("brk-b"), "BRK-B");
check("an empty chart list does not invent a symbol", resolveSelection("ADBE", "1h", [], [], []), {
  symbol: "ADBE",
  timeframe: "1h",
});
check(
  "a data refresh with no timeframe list keeps the open tab",
  resolveSelection("ADBE", "1d", watchlist, cards, []),
  { symbol: "ADBE", timeframe: "1d" },
);

const failed = results.filter((item) => !item.ok);
if (failed.length) {
  console.error(JSON.stringify(failed, null, 2));
  process.exit(1);
}
console.log(JSON.stringify(results.map((item) => item.name)));
"""
    output = _run_node(script)
    assert "reload keeps a symbol that is still on the watchlist" in output
    assert "click stores the latest symbol and timeframe" in output


def test_missing_symbol_falls_back_to_the_first_visible_card():
    script = _selection_source() + r"""
const store = {};
const localStorage = {
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  },
  setItem(key, value) { store[key] = String(value); },
};
const state = { symbol: null, timeframe: null };
const frames = ["1m", "1h", "1d"];
const cards = ["ABT", "ADBE", "AFL"];
const watchlist = ["ADBE", "ABT", "AFL"];
const results = [];
function check(name, actual, expected) {
  results.push({
    name,
    actual,
    expected,
    ok: JSON.stringify(actual) === JSON.stringify(expected),
  });
}

check(
  "a symbol that left the watchlist and the cards falls back to the first card",
  resolveSelection("ZZZZ", "1d", watchlist, cards, frames),
  { symbol: "ABT", timeframe: "1d" },
);
check(
  "fallback uses the card order when the watchlist is sorted differently",
  resolveSelection("GONE", "1h", ["AFL", "ADBE", "ABT"], cards, frames),
  { symbol: "ABT", timeframe: "1h" },
);
check(
  "a bad timeframe tab falls back to 1m when that tab exists",
  resolveSelection("ADBE", "5m", watchlist, cards, frames),
  { symbol: "ADBE", timeframe: "1m" },
);
check(
  "without 1m the first returned chart tab is used",
  resolveSelection("ADBE", "5m", watchlist, cards, ["1h", "1d"]),
  { symbol: "ADBE", timeframe: "1h" },
);
check(
  "a first visit with nothing stored opens the first card on 1m",
  resolveSelection(null, null, watchlist, cards, frames),
  { symbol: "ABT", timeframe: "1m" },
);
localStorage.setItem(SELECTION_KEY, "not-json");
check("broken storage does not throw", loadSelection(), { symbol: null, timeframe: null });
localStorage.setItem(SELECTION_KEY, JSON.stringify({ symbol: "<ABT>", timeframe: "1M" }));
check("markup and a bad tab are ignored", loadSelection(), { symbol: null, timeframe: null });
check(
  "ignored storage then resolves to the first card",
  resolveSelection(loadSelection().symbol, loadSelection().timeframe, watchlist, cards, frames),
  { symbol: "ABT", timeframe: "1m" },
);

const failed = results.filter((item) => !item.ok);
if (failed.length) {
  console.error(JSON.stringify(failed, null, 2));
  process.exit(1);
}
console.log("ok");
"""
    assert _run_node(script).strip() == "ok"


def test_page_restores_selection_without_resetting_the_live_chart():
    page = _page()
    assert 'name="atlas-dashboard-version" content="25"' in page
    assert 'const SELECTION_KEY = "atlas-paper-selection";' in page
    assert "localStorage.getItem(SELECTION_KEY)" in page
    assert "localStorage.setItem(SELECTION_KEY" in page
    tail = page[page.index('getElementById("refresh-now")'):]
    assert tail.index("refresh(true)") < tail.index("loadSelection()")
    assert tail.index("const restoredSelection = loadSelection();") < tail.index(
        "state.symbol = restoredSelection.symbol;"
    )
    assert tail.index("state.timeframe = restoredSelection.timeframe;") < tail.index(
        "refresh(false)"
    )
    assert "location.reload" not in page
    assert "state.symbol = symbols[0]" not in page

    choose = page[page.index("function chooseSymbol"):page.index("function chooseTimeframe")]
    assert choose.index("saveSelection()") < choose.index("loadCandles()")
    timeframe = page[page.index("function chooseTimeframe"):page.index("function selectSymbol")]
    assert "saveSelection()" in timeframe
    assert "state.symbol" not in timeframe
    assert "chooseSymbol(button.dataset.symbol)" in page
    assert "chooseTimeframe(button.dataset.tf)" in page

    switchers = page[page.index("function renderSwitchers"):page.index("function paintSwitchers")]
    assert "resolveSelection(state.symbol, state.timeframe, allowed, symbols, frames)" in switchers
    assert "(state.scans || [])" in switchers
    assert "(state.top || [])" in switchers
    refresh = page[page.index("async function refresh"):page.index("function scheduleScanFollowUp")]
    assert "renderSwitchers(payload.portfolio)" in refresh
    assert "loadCandles()" in refresh

    live = tail[tail.index("if (!sessionStillOpen()) return;"):tail.index("CHART_REFRESH_MS);")]
    assert "loadCandles({ live: true })" in live
    assert "resolveSelection" not in live
    assert "state.symbol" not in live
    assert "saveSelection" not in live
    assert "symbols[0]" not in live
    assert 'if (!candles.length)' in page
    assert "No bars returned." in page
    assert "if (payload.feed_note) parts.push(payload.feed_note);" in page
    dashboard = (ROOT / "atlas" / "dashboard.py").read_text(encoding="utf-8")
    assert "No prices are filled in." in dashboard


def test_selection_memory_does_not_change_risk_or_trade_gates(monkeypatch):
    monkeypatch.delenv("ATLAS_TIMEFRAMES", raising=False)
    assert LIVE_TRADING is False
    limits = RiskLimits()
    assert limits.risk_per_trade_fraction == 0.01
    assert limits.max_position_fraction == 0.10
    assert limits.max_daily_loss_fraction == 0.03
    assert resolve_timeframes() == ("1h", "1d")
    assert "1m" not in resolve_timeframes()

    page = _page()
    assert 'const buyOn = held === "SHORT" || !!scan.buyable || !!gate.allow_buy;' in page
    assert (
        'const sellOn = held === "LONG" || (held === null && !!scan.shortable) || !!gate.allow_sell;'
        in page
    )
    assert 'buyOn ? "" : "disabled"' in page
    assert 'sellOn ? "" : "disabled"' in page
    assert "Live trading stays off." in page
    assert "is chart display only. Buy and sell still use the decision timeframes." in page
    dashboard = (ROOT / "atlas" / "dashboard.py").read_text(encoding="utf-8")
    session = (ROOT / "atlas" / "session.py").read_text(encoding="utf-8")
    assert 'CHART_ONLY_TIMEFRAMES = ("1m",)' in dashboard
    assert "1m is chart display only. Decision timeframes stay on ATLAS_TIMEFRAMES." in session
    assert 'os.getenv("ATLAS_TIMEFRAMES", "1h,1d")' in session


def _frames(start):
    frame = recent_history(start, 1)
    return {"1h": frame, "1d": frame.copy()}


def _expected_gates(scan, positions):
    held = None
    for position in positions:
        if position["symbol"] == scan["symbol"] and position["quantity"] > 0:
            held = "SHORT" if position.get("side") == "SHORT" else "LONG"
            break
    gate = ((scan.get("prediction") or {}).get("gate") or {})
    buy_on = held == "SHORT" or bool(scan.get("buyable")) or bool(gate.get("allow_buy"))
    sell_on = (
        held == "LONG"
        or (held is None and bool(scan.get("shortable")))
        or bool(gate.get("allow_sell"))
    )
    return buy_on, sell_on


def test_browser_reload_and_refresh_keep_the_open_symbol(tmp_path):
    chrome = subprocess.run(
        ["google-chrome", "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if chrome.returncode != 0:
        pytest.skip("google-chrome is not available")

    symbols = ("ABT", "ADBE", "AFL", "A")
    histories = {symbol: _frames(100 + offset * 15) for offset, symbol in enumerate(symbols)}

    def load(symbol, timeframe):
        frame = (histories.get(symbol) or {}).get(timeframe)
        if frame is None or getattr(frame, "empty", True):
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        return frame

    database = AtlasDatabase(tmp_path / "selection.sqlite3")
    service = PaperDashboard(
        token="local-test-token",
        settings=Settings(
            instruments=symbols,
            paper_initial_cash=10_000,
            live_trading_enabled=False,
        ),
        database=database,
        histories_by_symbol=histories,
        news_events=[],
        candle_loader=load,
        hard_stop=False,
    )
    server = make_server(service, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        payload = json.loads(urlopen(base + "/api/state", timeout=30).read().decode("utf-8"))
        assert payload["portfolio"]["mode"] == "PAPER"
        assert payload["portfolio"]["live_trading"] is False
        assert payload["portfolio"]["timeframes"] == ["1h", "1d"]
        assert payload["chart_timeframes"] == ["1m", "1h", "1d"]
        assert payload["portfolio"]["risk"]["per_trade"] == 0.01
        assert payload["portfolio"]["risk"]["max_exposure"] == 0.10
        assert payload["portfolio"]["risk"]["daily_loss"] == 0.03
        assert [card["symbol"] for card in payload["top"]]
        assert len(payload["scans"]) == 4

        empty = json.loads(urlopen(
            base + "/api/candles?symbol=ADBE&timeframe=1m",
            timeout=10,
        ).read().decode("utf-8"))
        assert empty["candles"] == []
        assert empty["chart_only"] is True
        assert "No bars returned" in empty["error"]

        script = r"""
const http = require("http");
const net = require("net");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const pageUrl = process.argv[2];
const steps = [];

function note(name, detail) { steps.push({ name, detail }); }

function request(url, method = "GET") {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method }, (res) => {
      let body = "";
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => resolve({ status: res.statusCode, body }));
    });
    req.on("error", reject);
    req.end();
  });
}

function freePort() {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function main() {
  const debugPort = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "atlas-chrome-"));
  const chrome = spawn("google-chrome", [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--remote-allow-origins=*",
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    "about:blank",
  ], { stdio: "ignore" });
  let ws;
  try {
    let version;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      try {
        const response = await request(`http://127.0.0.1:${debugPort}/json/version`);
        version = JSON.parse(response.body);
        break;
      } catch (error) {
        await delay(100);
      }
    }
    if (!version) throw new Error("Chrome debugging port did not open");
    const opened = await request(
      `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(pageUrl)}`,
      "PUT",
    );
    if (opened.status !== 200) throw new Error(`json/new ${opened.status} ${opened.body}`);
    const target = JSON.parse(opened.body);
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve);
      ws.addEventListener("error", () => reject(new Error("debugger websocket failed")));
    });
    let nextId = 1;
    const pending = new Map();
    const pageErrors = [];
    ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        const { resolve, reject } = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
      } else if (message.method === "Runtime.exceptionThrown") {
        pageErrors.push(message.params.exceptionDetails);
      }
    });
    function send(method, params = {}) {
      const id = nextId++;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
      });
    }
    await send("Runtime.enable");
    await send("Page.enable");
    async function evaluate(expression) {
      const result = await send("Runtime.evaluate", {
        expression,
        awaitPromise: true,
        returnByValue: true,
      });
      if (result.exceptionDetails) {
        const detail = result.exceptionDetails;
        const description = detail.exception && detail.exception.description;
        throw new Error(`${detail.text || "page error"} ${description || ""}`.trim());
      }
      return result.result && result.result.value;
    }
    const readExpression = `(() => {
      const note = (document.getElementById("chart-note") || {}).textContent || "";
      const symbolBtn = document.querySelector("#watchlist .symbol-btn.active");
      const tfBtn = document.querySelector("#timeframes .tf-btn.active");
      const card = document.querySelector("#candidates .card.selected");
      const firstCard = document.querySelector("#candidates .card");
      const buy = document.getElementById("action-buy");
      const sell = document.getElementById("action-sell");
      let stored = null;
      try { stored = JSON.parse(localStorage.getItem("atlas-paper-selection") || "null"); }
      catch (error) { stored = { error: String(error) }; }
      return {
        note,
        symbol: symbolBtn ? symbolBtn.dataset.symbol : null,
        timeframe: tfBtn ? tfBtn.dataset.tf : null,
        card: card ? card.dataset.pick : null,
        firstCard: firstCard ? firstCard.dataset.pick : null,
        buyDisabled: buy ? buy.disabled : null,
        sellDisabled: sell ? sell.disabled : null,
        stored,
        names: [...document.querySelectorAll("#watchlist [data-symbol]")].map((el) => el.dataset.symbol),
        cards: [...document.querySelectorAll("#candidates .card")].map((el) => el.dataset.pick),
        version: (document.querySelector('meta[name="atlas-dashboard-version"]') || {}).content || "",
      };
    })()`;
    async function read() { return evaluate(readExpression); }
    async function waitFor(label, predicate) {
      let last = null;
      for (let attempt = 0; attempt < 80; attempt += 1) {
        try {
          last = await read();
          if (predicate(last)) return last;
        } catch (error) {
          last = { error: String(error) };
        }
        await delay(150);
      }
      throw new Error(`${label} ${JSON.stringify(last)} pageErrors ${JSON.stringify(pageErrors).slice(0, 800)}`);
    }

    const openedPage = await waitFor("first paint", (snap) => snap.names.length === 4 && snap.note);
    if (openedPage.version !== "25") throw new Error(`version ${openedPage.version}`);
    if (!openedPage.note.includes("No bars returned")) {
      throw new Error(`1m chart invented bars: ${openedPage.note}`);
    }
    if (openedPage.note.includes("bars from the market-data feed")) {
      throw new Error(`empty 1m chart claimed bars: ${openedPage.note}`);
    }
    if (!openedPage.note.includes("chart display only")) {
      throw new Error(`1m lost the display-only note: ${openedPage.note}`);
    }
    note("opened on the default tab", {
      symbol: openedPage.symbol,
      timeframe: openedPage.timeframe,
      firstCard: openedPage.firstCard,
    });

    const picked = openedPage.names.find((name) => !openedPage.cards.includes(name))
      || openedPage.names.find((name) => name !== openedPage.firstCard);
    if (!picked) throw new Error(`no alternate symbol in ${JSON.stringify(openedPage.names)}`);
    const clicked = await evaluate(`(() => {
      const button = [...document.querySelectorAll("#watchlist [data-symbol]")]
        .find((el) => el.dataset.symbol === ${JSON.stringify(picked)});
      if (!button) return false;
      button.click();
      return true;
    })()`);
    if (!clicked) throw new Error("watchlist click failed");
    await evaluate(`document.querySelector('#timeframes [data-tf="1d"]').click()`);
    const chosen = await waitFor("chosen chart", (snap) => (
      snap.symbol === picked && snap.timeframe === "1d" && snap.note.includes(`${picked} 1d`)
      && snap.note.includes("bars from the market-data feed")
      && snap.stored && snap.stored.symbol === picked && snap.stored.timeframe === "1d"
    ));
    note("click stores the watchlist symbol and 1d", {
      symbol: chosen.symbol,
      timeframe: chosen.timeframe,
      card: chosen.card,
    });

    const beforeLive = await read();
    await evaluate("loadCandles({ live: true })");
    const afterLive = await read();
    if (afterLive.symbol !== beforeLive.symbol || afterLive.timeframe !== beforeLive.timeframe) {
      throw new Error(`live chart refresh moved selection ${JSON.stringify(beforeLive)} -> ${JSON.stringify(afterLive)}`);
    }
    note("15s chart refresh kept the selection", {
      symbol: afterLive.symbol,
      timeframe: afterLive.timeframe,
    });

    const refreshHandler = await evaluate(`String(document.getElementById("refresh-now").onclick)`);
    if (!refreshHandler.includes("refresh(true)") || refreshHandler.includes("location.reload")) {
      throw new Error(`refresh button handler changed: ${refreshHandler}`);
    }
    await evaluate("refresh(true)");
    const refreshed = await waitFor("refresh now", (snap) => (
      snap.symbol === picked && snap.timeframe === "1d" && snap.note.includes(`${picked} 1d`)
    ));
    note("Refresh now stayed on the open symbol", {
      symbol: refreshed.symbol,
      timeframe: refreshed.timeframe,
      buyDisabled: refreshed.buyDisabled,
      sellDisabled: refreshed.sellDisabled,
    });

    await send("Page.reload", { ignoreCache: true });
    const reloaded = await waitFor("reload", (snap) => (
      snap.symbol === picked && snap.timeframe === "1d" && snap.note.includes(`${picked} 1d`)
      && snap.stored && snap.stored.symbol === picked
    ));
    note("reload restored the symbol and 1d", {
      symbol: reloaded.symbol,
      timeframe: reloaded.timeframe,
      buyDisabled: reloaded.buyDisabled,
      sellDisabled: reloaded.sellDisabled,
    });

    await evaluate(`localStorage.setItem("atlas-paper-selection", JSON.stringify({ symbol: "ZZZZ", timeframe: "1d" }))`);
    await send("Page.reload", { ignoreCache: true });
    const fallback = await waitFor("stale symbol", (snap) => (
      snap.names.length === 4 && snap.note && !snap.note.includes("ZZZZ")
      && snap.symbol && snap.symbol !== "ZZZZ" && snap.timeframe === "1d"
      && snap.note.includes(`${snap.symbol} 1d`)
    ));
    if (fallback.symbol !== fallback.firstCard) {
      throw new Error(`fallback ${fallback.symbol} was not the first card ${fallback.firstCard}`);
    }
    if (!fallback.names.includes(fallback.symbol)) {
      throw new Error(`fallback left the watchlist ${JSON.stringify(fallback)}`);
    }
    note("stale symbol fell back to the first card", {
      symbol: fallback.symbol,
      timeframe: fallback.timeframe,
      buyDisabled: fallback.buyDisabled,
      sellDisabled: fallback.sellDisabled,
    });
    if (pageErrors.length) {
      throw new Error(`page errors ${JSON.stringify(pageErrors).slice(0, 800)}`);
    }
    console.log(JSON.stringify({ steps }));
  } finally {
    if (ws) ws.close();
    chrome.kill("SIGKILL");
  }
}

main().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""
        driver = tmp_path / "selection-browser.js"
        driver.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            ["node", str(driver), base + "/"],
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr or completed.stdout)
        report = json.loads(completed.stdout)
        by_name = {item["name"]: item["detail"] for item in report["steps"]}
        assert by_name["click stores the watchlist symbol and 1d"]["timeframe"] == "1d"
        chosen = by_name["click stores the watchlist symbol and 1d"]["symbol"]
        assert chosen != by_name["opened on the default tab"]["firstCard"]
        assert by_name["15s chart refresh kept the selection"]["symbol"] == chosen
        assert by_name["Refresh now stayed on the open symbol"]["symbol"] == chosen
        assert by_name["Refresh now stayed on the open symbol"]["timeframe"] == "1d"
        assert by_name["reload restored the symbol and 1d"]["symbol"] == chosen
        assert by_name["reload restored the symbol and 1d"]["timeframe"] == "1d"
        assert by_name["stale symbol fell back to the first card"]["symbol"] != "ZZZZ"
        assert by_name["stale symbol fell back to the first card"]["timeframe"] == "1d"

        scans = {card["symbol"]: card for card in payload["scans"]}
        positions = payload["portfolio"]["positions"]
        for label in (
            "Refresh now stayed on the open symbol",
            "reload restored the symbol and 1d",
            "stale symbol fell back to the first card",
        ):
            detail = by_name[label]
            buy_on, sell_on = _expected_gates(scans[detail["symbol"]], positions)
            assert detail["buyDisabled"] is (not buy_on)
            assert detail["sellDisabled"] is (not sell_on)
    finally:
        server.shutdown()
        server.server_close()
        database.close()
