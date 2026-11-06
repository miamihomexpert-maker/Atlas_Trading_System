# ATLAS Trading System

Paper trading only. Live trading is refused: `ATLAS_LIVE_TRADING=true` raises before a session starts, and the broker has no live connection.

Company and macro engines are not on this path. News is an assessment of events you supply. Quant participation is the promoted logistic model already stored in the local database, when one exists. If no model is active, the session still runs on technical agreement plus news.

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

## News

There is no news provider. Omit `ATLAS_NEWS_FILE` and the session records that no events were supplied. To apply events, point the variable at a JSON list:

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
    "sentiment": -0.8,
    "impact": 0.9,
    "instruments": ["SPY"]
  }
]
```

Categories: `EARNINGS`, `FED`, `INFLATION`, `EMPLOYMENT`, `ECONOMIC`, `GEOPOLITICAL`, `OTHER`. Sentiment is from -1 to 1. Impact is from 0 to 1. A strong opposing event withholds the directional recommendation.

## Quant model

Train and promote a model separately with `atlas.adaptive.train_candidate` and `AtlasDatabase.promote_model`. When `active_model()` returns a version, the paper pass requires that prediction to match the technical `BUY` or `SELL`. A probability at or above the model threshold (default 0.55) is `BUY`. A probability at or below `1 - threshold` is `SELL`. Values in between are `WAIT` and block the order. No active model means the technical decision is not waiting on a prediction.

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
