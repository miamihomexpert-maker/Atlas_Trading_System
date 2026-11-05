# ATLAS Trading System

Paper trading only. Live trading is refused: `ATLAS_LIVE_TRADING=true` raises before a session starts, and the broker has no live connection.

Company and macro engines are not on this path. News is an assessment of events you supply. Quant participation is the promoted logistic model already stored in the local database, when one exists. If no model is active, the session still runs on technical agreement plus news.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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

`ATLAS_PAPER_APPROVED=true` is the operator approval for orders in this process. Risk limits still apply on the fill: 1% per trade, 10% exposure, and a 3% daily-loss halt. Omit the variable to score the watchlist without submitting.

The ledger is `database/atlas.sqlite3` unless `ATLAS_DATABASE_PATH` points somewhere else. A later session reloads the paper book from that file.

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

Telegram messages use `/approve TOKEN` and `/reject TOKEN` in the authorized private chat. Those tokens are consumed by `execute_paper_signal` when a caller supplies one. `ATLAS_PAPER_APPROVED` does not bypass remote approval.

## Tests

```bash
python -m pytest -q
```
