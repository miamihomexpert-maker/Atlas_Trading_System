# ATLAS Trading System

Paper trading only. Live trading is refused: `ATLAS_LIVE_TRADING=true` raises before a session starts, and the broker has no live connection.

News is Yahoo headlines unless `ATLAS_NEWS_FILE` is set. Company facts come from Yahoo for the configured symbols and are display-only: a fund shows its stated objective and asset allocation when the feed returns them, and an operating company shows statement rows only when those rows are present. Neither news, an unpromoted model, nor company facts can turn a technical wait into a buy. The macro engine is not on this path. A promoted logistic model must agree with the technical scan. If no model has passed the walk-forward gates, the technical scan still decides and the panel says why the model was not promoted.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Windows PowerShell

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\scripts\paper.ps1
```

`.\scripts\paper.ps1` loads `.env` if that file exists, without replacing variables already set in the terminal, then runs `python main.py --paper --loop`. `.env` stays gitignored. Do not put a bot token in a committed file. `.env.example` has empty Telegram fields.

Python also reads `.env` when `main.py` starts, and it does not override variables that are already set. Either of these is enough for a new terminal:

```powershell
.\scripts\paper.ps1
python main.py --paper --loop
```

One pass, the book, and module status:

```powershell
.\scripts\paper.ps1 --paper
.\scripts\paper.ps1 --status-book
.\scripts\paper.ps1 --status
```

## One paper session

`python main.py` and `python main.py --paper` do the same thing: one pass over the watchlist. They do not start a scheduler.

If `ATLAS_INSTRUMENTS` is unset, the watchlist is the S&P 500 constituent file `config/sp500.txt` (captured 2026-11-10 from the public Wikipedia table). Each pass scores `ATLAS_SCAN_BATCH` names (default 20) and stores the result. Later passes continue through the list. The dashboard top 3 are the best stored scan results so far: a risk-sizable BUY first, then other BUY results, then WAIT, then SELL, using the confidence the existing scan already computed. That order is not a forecast of profit. Set `ATLAS_INSTRUMENTS` to scan only the symbols you name.

```bash
export ATLAS_INSTRUMENTS=SPY,QQQ
export ATLAS_TIMEFRAMES=1h,1d
export ATLAS_PAPER_INITIAL_CASH=100000
export ATLAS_MAX_MARKET_DATA_AGE_SECONDS=900
export ATLAS_DATABASE_PATH=database/atlas.sqlite3
export ATLAS_PAPER_APPROVED=true
python main.py --paper
```

Bars come from Yahoo Finance through `yfinance`. The `1h` request uses a `1mo` period and the `1d` request uses `6mo`. Empty responses are reported as no data. This repository does not ship sample prices or performance.

Yahoo labels each bar at its open: a 1h bar is stamped on the half hour (13:30 covers 13:30–14:30) and a 1d bar is stamped at midnight New York time. The stale-data check ages the newest bar by when that bar was current — the fetch time while it is still forming, otherwise its end (16:00 New York for a daily bar) — not the open label. `ATLAS_MAX_MARKET_DATA_AGE_SECONDS` still rejects a bar that has already been closed longer than that limit. A shell value overrides `.env`.

`ATLAS_PAPER_APPROVED=true` is the operator approval for orders in this process. Risk limits still apply on the fill: 1% per trade, 10% exposure, and a 3% daily-loss halt. Omit the variable to score the watchlist without submitting.

The ledger is `database/atlas.sqlite3` unless `ATLAS_DATABASE_PATH` points somewhere else. A later session reloads the paper book, including each open position's stop and target.

## Dashboard

The dashboard is paper only, binds to `127.0.0.1`, and refuses live trading. Every figure on the page comes from the market-data feed, the SQLite ledger, or the paper pipeline. Order buttons do nothing until you confirm in the dialog. That click is the approval: the server re-checks risk limits and writes an audit row with a timestamp. A local token is embedded in the page and required on order requests. Telegram tokens are not sent to the browser.

Start the loop in one terminal and the dashboard in another:

```powershell
.\scripts\paper.ps1
```

```powershell
python main.py --dashboard
```

Open `http://127.0.0.1:8765`. The port is `ATLAS_DASHBOARD_PORT` (default 8765).

The candle chart fills the center, with the watchlist on the left and Buy and Sell on the right. Drag the bars beside the chart to resize those lists. Signals, Technicals, News, Model, Company, Risk, Sell recommended, and Review are tabs beside the chart. Review shows ledger fills and stored scan decisions against a later bar when one exists, and it leaves a wait as a wait. The hit rate is shown only after a real result is counted. They start closed, so they do not take the chart's height. Click a tab to open that reading pane, click it again or press Close to put it away, and the last choice stays in this browser. The header shows the America/New_York regular-session clock. It counts down to the next 09:30 open or 16:00 close on a weekday. Exchange holidays are not marked. Refresh now reloads the page data without waiting for the 20-second poll. The chart reads each returned bar's open, high, low, close, and volume, and draws SMA 20, EMA 20, and RSI 14 only from those bars. Moving the pointer shows those fields in a box on the chart for the bar under the mouse. A 1m tab is chart display only and is not added to `ATLAS_TIMEFRAMES`. While the regular session is open, the selected symbol's chart reloads every 15 seconds from `/api/candles`. That request does not scan the universe. The feed can be delayed, and bars that were not returned are left blank. News headlines, excerpts, and links in the Relevant News tab use light text and amber links on the dark panel. Behavior, beside Company, shows later bars after stored scans and any insider or holder rows the public feed actually returned. It does not turn a wait into a buy or a sell by itself, and it is not a forecast of profit. Filings can be delayed. Predictions is the first tab. Investors sits beside it and lists only named, dated public movements, including whether each one lined up with a stored lean. A paper buy, or a sell that closes an open long, can also come from that track record after 8 graded leans that were more right than wrong and better in the later half, with a risk size above zero. A thin record leaves the scan as a wait. The prediction gate does not open a short. After you pull this page, open `http://127.0.0.1:8765/?v=23` so the browser loads it. The behavior page is `?v=22`. The technicals panel repeats the scan's own observations and says when a field was not returned. The risk panel shows the 1% per-trade, 10% exposure, and 3% daily-loss limits next to this paper account's cash, equity, P&L, and open-position exposure. Buy stays available for a real BUY with a risk size above zero, and also to cover an open paper short. That cover does not open a new long. Sell closes an open long and does not flip it into a short. With no long on, Sell opens a paper short only for a real SELL whose risk size is above zero. A wait is not a short. Positions and fills are marked long or short.

`ATLAS_HARD_STOP=false` by default. While the loop is running, a stop, target, or opposing signal becomes a **Sell recommended** alert with an OK button. Set `ATLAS_HARD_STOP=true` only if a touch of the stored stop should sell immediately through the same risk and paper-broker path. Target and opposing signals still wait for the button.

## Continuous paper loop

`python main.py --paper --loop` repeats the paper pass. The default pause is 300 seconds (`ATLAS_LOOP_INTERVAL_SECONDS`). Each open-market cycle fetches bars again. Orders still have to pass the existing stale-data check.

The loop runs only during the US regular session, 09:30 inclusive to 16:00 exclusive America/New_York, Monday through Friday. Outside that window it sleeps for the same interval and does not fetch or submit. Exchange holidays are not special-cased. Ctrl+C stops the loop.

On each cycle an open paper long is marked to the latest fresh close. It is sold through the same risk, approval, and paper-broker path if that close is at or through its stored stop, at or through its stored target, or the normal decision is a confirmed `SELL`. A stop or target exit uses the observed close as the paper fill price. The latest buy stores the stop and target, and a restart reads them back from SQLite.

## Book status

```bash
python main.py --status-book
```

Prints open positions (quantity, average, last price, stop, target), cash, equity, the ledger's daily P&L, and the latest fills. It does not submit orders.

## Status only

```bash
python main.py --status
```

This prints module status and does not fetch bars or submit orders.

## Company facts

The company panel reads Yahoo Finance through `yfinance` for each configured symbol. SPY and QQQ are funds, so the panel shows the stated investment objective, top holdings, sector weights, and asset-class weights when that fund profile returns them. It does not invent capital expenditure, buybacks, dividends, research, acquisitions, or guidance for a fund. If a later symbol is an operating company (`quoteType` EQUITY), the panel adds the latest cash-flow or income-statement amount only for a row the statement actually contained. A failed or empty feed stays blank and says why. `ATLAS_COMPANY_FACTS=false` skips the fetch. These facts are not an input to the buy or sell decision.

## News

Headlines come from Yahoo Finance through `yfinance` for the configured symbols. A fetch error is recorded as `FETCH_FAILED` and does not invent events. The Relevant News tab links each item to the article URL Yahoo returned, and shows the summary, publisher, time, and thumbnail when those fields are present. A video link is shown only when Yahoo marks the item as a video and includes an address. A headline with no address is shown with a note that the feed did not include a link. The symbol also links to its Yahoo quote page and Yahoo profile page. A company website is linked only when the company feed returns one. Sentiment is counted from a fixed word list in `atlas/news_feed.py`. A headline with none of those words is stored with sentiment 0 and impact 0, which means it was not scored.

`ATLAS_NEWS_FILE` replaces the feed for tests or an offline run:

```bash
export ATLAS_NEWS_FILE=news.json
```

```json
[
  {
    "event_id": "example-1",
    "title": "Example headline supplied by the operator",
    "category": "EARNINGS",
    "published_at": "2026-11-05T15:00:00+00:00",
    "sentiment": 0,
    "impact": 0,
    "instruments": ["SPY"]
  }
]
```

## Baseline model

`python main.py --train` fits the existing logistic model on two years of daily bars for each configured symbol and promotes it only if the walk-forward gates already in the database pass. `ATLAS_AUTO_TRAIN=true` (the default) does that fit on the first paper or dashboard run when no model is active. A short history is `INSUFFICIENT_HISTORY`. A model that fails the gates stays a candidate: its probability can be shown, and it does not create or veto a BUY. A promoted model has to agree with the technical scan before a recommendation is confirmed. The model does not submit orders.

```powershell
git pull
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py --train
python main.py --paper --loop
python main.py --dashboard
```

Categories for a file override: `EARNINGS`, `FED`, `INFLATION`, `EMPLOYMENT`, `ECONOMIC`, `GEOPOLITICAL`, `OTHER`. A file sentiment below zero with impact at least 0.65 withholds a BUY. The Yahoo feed only reaches that veto when the word list actually matches.

## Quant model

`python main.py --train` is the baseline fit. When `active_model()` returns a version, the paper pass requires that prediction to match the technical `BUY` or `SELL`. A probability at or above the model threshold (default 0.55) is `BUY`. A probability at or below `1 - threshold` is `SELL`. Values in between are `WAIT` and block a directional technical signal. The model never submits an order by itself.

## Approval

Local paper fills:

```bash
export ATLAS_PAPER_APPROVED=true
```

Remote approval is optional and is not polled by this command. Turn it on and the session will not submit:

```bash
export ATLAS_REQUIRE_REMOTE_APPROVAL=true
export ATLAS_TELEGRAM_BOT_TOKEN=your-bot-token
export ATLAS_TELEGRAM_CHAT_ID=your-private-chat-id
```

Telegram messages use `/approve TOKEN` and `/reject TOKEN` in the authorized private chat. Those tokens are consumed by `execute_paper_signal` when a caller supplies one. `ATLAS_PAPER_APPROVED` does not bypass remote approval. With remote approval required, the loop will not submit protective exits either.

If both `ATLAS_TELEGRAM_BOT_TOKEN` and `ATLAS_TELEGRAM_CHAT_ID` are set, a paper fill open, a paper fill close, and a daily-loss halt each send one Telegram message. If either variable is unset, nothing is sent and the session continues. A Telegram failure does not roll back a fill.

## Tests

```bash
python -m pytest -q
```
