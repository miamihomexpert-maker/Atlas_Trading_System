from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from atlas.broker import PaperBroker
from atlas.config import LIVE_TRADING
from atlas.database import AtlasDatabase
from atlas.execution import execute_paper_signal
from atlas.logger import get_logger
from atlas.market_data import (
    feed_is_paused,
    feed_status,
    note_feed_failure,
    pause_note,
    take_skips,
)
from atlas.market_hours import session_clock
from atlas.risk import RiskLimits, calculate_position_size, validate_daily_loss
from atlas.technical import calculate_ema, calculate_rsi, calculate_sma
from atlas.baseline import auto_train_enabled, ensure_baseline_model
from atlas.company import describe_company, profile_for_symbol, resolve_company_loader
from atlas.learning import (
    build_prediction,
    describe_behavior,
    evaluate_prediction_gate,
    grade_prediction_outcomes,
    ohlc_bars,
    prediction_reference,
    remember_outcomes,
    remember_public,
    resolve_behavior_loader,
    stamp_learning_bias,
    assess_track_record,
    investor_audit_rows,
)
from atlas.news import http_url, yahoo_profile_url, yahoo_quote_url
from atlas.news_feed import resolve_news
from atlas.session import (
    _exit_signal,
    _latest_quote,
    _quote_is_stale,
    fetch_session_histories,
    hard_stop_enabled,
    resolve_timeframes,
)
from atlas.pipeline import run_paper_pipeline
from atlas.ranking import coverage_note, rank_scans, top_scans
from atlas.review import build_review, choose_bars, latest_bar
from atlas.universe import next_batch, scan_batch_size
from config.settings import load_settings


logger = get_logger()
_PAGE = (Path(__file__).with_name("dashboard_page.html")).read_text(encoding="utf-8")
# Short bars for the chart. They are not added to the decision timeframes.
CHART_ONLY_TIMEFRAMES = ("1m",)
CHART_REFRESH_SECONDS = 15
_CHART_FEED_NOTE = (
    "These bars come from the market-data feed. "
    "The feed is not a tick stream and can be delayed. "
    "No prices are filled in."
)


def _chart_timeframes(service):
    try:
        return list(service.chart_timeframes())
    except Exception as error:
        logger.info("Chart timeframes unavailable: %s", error)
        return list(CHART_ONLY_TIMEFRAMES)


def _chart_result(symbol, timeframe, candles, error, chart_only):
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "indicators": {"sma": 20, "ema": 20, "rsi": 14} if candles else None,
        "chart_only": chart_only,
        "refresh_seconds": CHART_REFRESH_SECONDS,
        "feed_note": _CHART_FEED_NOTE,
        "error": error,
    }


def _annotate_feed(payload):
    status = feed_status()
    payload["feed_paused"] = bool(status)
    payload["retry_after_seconds"] = status["seconds_remaining"] if status else 0
    return payload


def _candle_skip_message(skips=()):
    """Short honest line. It does not invent a bar."""
    note = pause_note() or ""
    if "timed out" in note.lower() or "timeout" in skips:
        return "Yahoo history timed out. No bars were returned."
    if note or "rate_limit" in skips or "paused" in skips:
        return "Yahoo rate limited. No bars were returned."
    if "busy" in skips:
        return "A market-data request is already running. No bars were returned."
    return "No bars were returned."


class PaperDashboard:
    """Local paper-only dashboard. A button click is the human approval."""

    def __init__(
        self,
        token,
        settings=None,
        database=None,
        histories_by_symbol=None,
        news_events=None,
        candle_loader=None,
        hard_stop=None,
        name_loader=None,
        company_loader=None,
        behavior_loader=None,
    ):
        if not token or not str(token).strip():
            raise ValueError("Dashboard token is required.")
        self.token = str(token)
        self.settings = settings or load_settings()
        if self.settings.live_trading_enabled or LIVE_TRADING:
            raise PermissionError("Live trading is disabled.")
        self._database = database
        self._owns_database = database is None
        if database is None:
            self._database = AtlasDatabase(
                os.getenv("ATLAS_DATABASE_PATH", "database/atlas.sqlite3")
            )
        self.histories_by_symbol = histories_by_symbol
        self._news_explicit = news_events is not None
        self.news_events = list(news_events or [])
        self.news_report = (
            {"status": "OVERRIDE", "error": None, "source": "caller"}
            if self._news_explicit
            else None
        )
        self._trained = False
        self.candle_loader = candle_loader
        self.name_loader = name_loader
        self._names = {}
        self.company_loader = resolve_company_loader(company_loader)
        self._companies = {}
        self.behavior_loader = resolve_behavior_loader(behavior_loader)
        self._behavior = {}
        self.timeframes = resolve_timeframes()
        self.hard_stop = hard_stop_enabled(hard_stop)
        self._batch_lock = threading.Lock()
        self._batch_thread = None
        self._scan_active = False
        self._scan_requested = False
        self._scan_loop_stop = None
        self._scan_loop_thread = None
        self._candle_cache = {}

    def chart_timeframes(self):
        """Decision timeframes plus display-only intervals. Order is the chart tabs."""
        frames = []
        for name in CHART_ONLY_TIMEFRAMES + tuple(self.timeframes):
            if name not in frames:
                frames.append(name)
        return frames

    @property
    def database(self):
        return self._database

    def close(self):
        self.stop_background_scans()
        if self._owns_database and self._database is not None:
            self._database.close()
            self._database = None

    def start_background_scans(self):
        """Scan on a side thread. Page reads do not wait for it."""
        if self.histories_by_symbol is not None:
            return
        if self._scan_loop_thread is not None and self._scan_loop_thread.is_alive():
            return
        self._scan_loop_stop = threading.Event()
        self._scan_loop_thread = threading.Thread(
            target=self._scan_loop,
            name="atlas-scan-loop",
            daemon=True,
        )
        self._scan_loop_thread.start()

    def stop_background_scans(self):
        if self._scan_loop_stop is not None:
            self._scan_loop_stop.set()
        loop = self._scan_loop_thread
        if loop is not None and loop.is_alive() and threading.current_thread() is not loop:
            loop.join(timeout=1)

    def _scan_loop(self):
        stop = self._scan_loop_stop
        while stop is not None and not stop.wait(2):
            if feed_is_paused():
                continue
            if self._scan_requested or self._scan_batch_due():
                self._scan_requested = False
                self._locked_scan()

    def request_scan(self):
        """Ask for one batch. Returns before Yahoo is called.

        A rate-limited feed records a zero-score pass and leaves stored cards.
        """
        if feed_is_paused():
            if self._batch_lock.acquire(blocking=False):
                try:
                    self._record_scored_pass(0)
                finally:
                    self._batch_lock.release()
            return "paused"
        self._scan_requested = True
        if self._scan_loop_thread is not None and self._scan_loop_thread.is_alive():
            return "started"
        if self._batch_thread is not None and self._batch_thread.is_alive():
            return "running"
        self._batch_thread = threading.Thread(
            target=self._locked_scan,
            name="atlas-scan",
            daemon=True,
        )
        self._batch_thread.start()
        return "started"

    def scan_pending(self):
        if self._scan_active:
            return True
        if self._batch_thread is not None and self._batch_thread.is_alive():
            return True
        return bool(self._scan_requested) and not feed_is_paused()

    def wait_for_scan(self, timeout=5):
        thread = self._batch_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def market_note(self):
        note = pause_note()
        if note:
            return note
        if self.scan_pending():
            return "A scan is running. Stored cards stay up until it finishes."
        return None

    def _locked_scan(self):
        if not self._batch_lock.acquire(blocking=False):
            return
        self._scan_active = True
        try:
            self._scan_requested = False
            if feed_is_paused():
                self._record_scored_pass(0)
                return
            self._scan_one_batch()
        except Exception as error:
            logger.info("Scan batch skipped: %s", error)
        finally:
            self._scan_active = False
            self._batch_lock.release()

    def _guard(self, token):
        if LIVE_TRADING or self.settings.live_trading_enabled:
            raise PermissionError("Live trading is disabled.")
        if not token or not secrets.compare_digest(str(token), self.token):
            raise PermissionError("Local approval token required.")

    def _broker(self):
        broker = PaperBroker(
            initial_cash=self.settings.paper_initial_cash,
            database=self.database,
        )
        if broker.account_info().get("trading_mode") != "PAPER":
            raise PermissionError("Live trading is disabled.")
        return broker

    def _instrument_name(self, symbol):
        """Use a name only when a loader returns one. Never invent one."""
        if self.name_loader is None:
            return None
        if symbol not in self._names:
            try:
                value = self.name_loader(symbol)
            except Exception:
                value = None
            if not isinstance(value, str) or not value.strip() or value.strip().upper() == symbol:
                value = None
            else:
                value = value.strip()
            self._names[symbol] = value
        return self._names[symbol]

    def _company_profile(self, symbol):
        if symbol not in self._companies:
            self._companies[symbol] = profile_for_symbol(symbol, self.company_loader)
        return self._companies[symbol]

    def _described(self, card, histories=None):
        if histories is None:
            try:
                histories = self._histories(card.get("symbol"))
            except Exception as error:
                logger.info("%s history skipped while describing: %s", card.get("symbol"), error)
                histories = None
        card["name"] = self._instrument_name(card["symbol"])
        card["company"] = self._company_profile(card["symbol"])
        card["company_copy"] = describe_company(card["company"])
        card["technical_copy"] = describe_technicals(card)
        card["links"] = _outbound_links(card)
        card["copy"] = describe_scan(card)
        card["behavior"] = self._behavior_for(card["symbol"])
        news = card.get("news") or {}
        card["prediction"] = self._prediction_for(
            card["symbol"],
            histories,
            news_items=news.get("items") or [],
        )
        return card

    def _prediction_for(self, symbol, histories, news_items=None):
        symbol = str(symbol).strip().upper()
        bars = ohlc_bars(histories)
        try:
            events = self.database.list_learning_events(symbol)
        except Exception as error:
            logger.info("%s prediction read failed: %s", symbol, error)
            events = []
        for outcome in grade_prediction_outcomes(events, bars):
            try:
                self.database.record_learning_event(
                    symbol,
                    outcome["source"],
                    outcome["source_key"],
                    outcome["summary"],
                    outcome["detail"],
                    outcome["effect"],
                )
            except Exception as error:
                logger.info("%s prediction grade skipped: %s", symbol, error)
        try:
            events = self.database.list_learning_events(symbol)
        except Exception as error:
            logger.info("%s prediction reread failed: %s", symbol, error)
            events = []
        prediction = build_prediction(symbol, events, bars, news_items=news_items)
        reference = prediction_reference(prediction)
        try:
            if reference is not None:
                self.database.record_learning_event(
                    symbol,
                    reference["source"],
                    reference["source_key"],
                    reference["summary"],
                    reference["detail"],
                    reference["effect"],
                )
            else:
                self.database.record_learning_event(
                    symbol,
                    "prediction_insufficient",
                    "insufficient",
                    prediction.get("lead") or "Not enough public data.",
                    {"status": prediction.get("status"), "direction": prediction.get("direction")},
                    "none",
                )
        except Exception as error:
            logger.info("%s prediction record skipped: %s", symbol, error)
        broker = self._broker()
        account = broker.account_info()
        daily = broker.daily_pnl()
        try:
            halted = not validate_daily_loss(account["equity"], daily)
        except (TypeError, ValueError):
            halted = True
        position = broker.positions().get(symbol)
        has_long = bool(position) and position.get("side", "LONG") != "SHORT" and position.get("quantity", 0) > 0
        gate = evaluate_prediction_gate(
            prediction,
            equity=account["equity"],
            exposure=broker.current_exposure(),
            daily_pnl=daily,
            halted=halted,
            has_long=has_long,
        )
        try:
            self.database.record_learning_event(
                symbol,
                "prediction_gate",
                gate["source_key"],
                gate["reason"],
                gate["audit"],
                gate["effect"],
            )
        except Exception as error:
            logger.info("%s prediction gate record skipped: %s", symbol, error)
        try:
            overall = [
                event for event in self.database.list_learning_events()
                if event.get("source") == "prediction_outcome"
            ]
        except Exception:
            overall = []
        prediction["gate"] = gate
        prediction["overall_track_record"] = assess_track_record(overall)
        for row in investor_audit_rows(prediction.get("investors")):
            try:
                self.database.record_learning_event(
                    symbol,
                    row["source"],
                    row["source_key"],
                    row["summary"],
                    row["detail"],
                    row["effect"],
                )
            except Exception as error:
                logger.info("%s investor record skipped: %s", symbol, error)
        return prediction

    def _learn(self, symbol, histories):
        try:
            remember_outcomes(self.database, symbol, histories)
        except Exception as error:
            logger.info("%s learning skipped: %s", symbol, error)

    def _behavior_for(self, symbol):
        symbol = str(symbol).strip().upper()
        if symbol in self._behavior:
            return self._behavior[symbol]
        status = "NOT_LOADED"
        error = None
        fetched_at = None
        if self.behavior_loader is not None:
            try:
                fetched = self.behavior_loader(symbol)
            except Exception as error_value:
                fetched = None
                status = "FETCH_FAILED"
                error = str(error_value)
            else:
                if not isinstance(fetched, dict):
                    status = "FETCH_FAILED"
                    error = "Behavior loader did not return a record."
                else:
                    status = fetched.get("status") or "FETCHED"
                    error = fetched.get("error")
                    fetched_at = fetched.get("fetched_at")
                    try:
                        remember_public(self.database, symbol, fetched)
                    except Exception as error_value:
                        logger.info("%s public behavior was not stored: %s", symbol, error_value)
        try:
            events = self.database.list_learning_events(symbol)
        except Exception as error_value:
            logger.info("%s learning read failed: %s", symbol, error_value)
            events = []
        payload = describe_behavior(symbol, events, status, error, fetched_at)
        self._behavior[symbol] = payload
        return payload

    def _with_learning_bias(self, cards):
        events = {}
        for card in cards:
            symbol = card.get("symbol")
            if symbol in events:
                continue
            try:
                events[symbol] = self.database.list_learning_events(symbol)
            except Exception as error:
                logger.info("%s learning bias skipped: %s", symbol, error)
                events[symbol] = []
        return stamp_learning_bias(cards, events)

    def _prepare(self):
        if not self._news_explicit:
            self.news_events, self.news_report = resolve_news(self.settings.instruments)
        if not self._trained and auto_train_enabled():
            ensure_baseline_model(self.database, self.settings.instruments)
            self._trained = True

    def _preview(self, symbol, broker, histories):
        self._prepare()
        return run_paper_pipeline(
            symbol,
            histories,
            broker,
            f"preview-{symbol}-{uuid.uuid4().hex[:8]}",
            approved=False,
            database=self.database,
            news_events=self.news_events,
            news_report=self.news_report,
            execute=False,
            max_data_age_seconds=self.settings.max_market_data_age_seconds,
        )

    def _histories(self, symbol):
        normalized = str(symbol).strip().upper()
        if self.histories_by_symbol is not None:
            return (
                self.histories_by_symbol.get(symbol)
                or self.histories_by_symbol.get(normalized)
            )
        return fetch_session_histories(normalized, self.timeframes)

    def _audit(self, action, symbol, detail):
        detail = dict(detail or {})
        detail.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        detail["mode"] = "PAPER"
        return self.database.record_audit_event(action, symbol, detail)

    def portfolio(self):
        broker = self._broker()
        account = broker.account_info()
        daily = broker.daily_pnl()
        halted = not validate_daily_loss(account["equity"], daily)
        positions = []
        for symbol, position in sorted(broker.positions().items()):
            positions.append({
                "symbol": symbol,
                "side": position.get("side", "LONG"),
                "quantity": position["quantity"],
                "average_price": position["average_price"],
                "last_price": position["last_price"],
                "stop_loss": position.get("stop_loss"),
                "profit_target": position.get("profit_target"),
            })
        fills = []
        for row in self.database.list_paper_orders()[-10:]:
            fills.append({
                "timestamp": row["timestamp"],
                "symbol": row["symbol"],
                "side": row["side"],
                "effect": row.get("effect"),
                "quantity": row["quantity"],
                "price": row["price"],
            })
        return {
            "mode": "PAPER",
            "live_trading": False,
            "hard_stop": self.hard_stop,
            "halted": halted,
            "halt_reason": "Daily loss limit reached." if halted else None,
            "cash": account["cash"],
            "equity": account["equity"],
            "daily_pnl": daily,
            "positions": positions,
            "recent_fills": fills,
            "instruments": list(self.settings.instruments),
            "timeframes": list(self.timeframes),
            "risk": _risk_usage(account, daily, broker.current_exposure(), halted),
        }

    def clock(self, now=None):
        return session_clock(now)

    def review(self):
        """Grade stored fills and scan decisions. Does not change a decision."""
        orders = self.database.list_paper_orders()
        observations = self.database.list_scan_observations()
        bars = {}
        symbols = {row["symbol"] for row in orders} | {row["symbol"] for row in observations}
        for symbol in symbols:
            histories = self._histories(symbol)
            if histories:
                bars[symbol] = histories
        report = build_review(orders, observations, bars)
        encoded = json.dumps(report, sort_keys=True)
        previous = self.database.get_metadata("paper_review")
        self.database.set_metadata("paper_review", encoded)
        if previous != encoded:
            self.database.record_audit_event(
                "PAPER_REVIEW",
                None,
                {
                    "scoreboard_note": report["scoreboard_note"],
                    "reviewed_count": (report["scoreboard"] or {}).get("reviewed_count"),
                },
            )
        return report

    def _record_observation(self, card, histories):
        decision = str(card.get("decision") or "").upper()
        if decision not in {"BUY", "WAIT", "SELL"}:
            return
        timeframe, bars = choose_bars(histories)
        last = latest_bar(bars)
        if last is None:
            return
        price = card.get("price")
        if isinstance(price, bool) or price is None:
            stored_price = None
        else:
            try:
                stored_price = float(price)
            except (TypeError, ValueError):
                stored_price = None
        model = card.get("model") or {}
        self.database.record_scan_observation(
            card["symbol"],
            decision,
            stored_price,
            last["time"],
            timeframe,
            model.get("status"),
            model.get("signal"),
            model.get("version"),
        )

    def alerts(self):
        rows = []
        for alert in self.database.list_exit_alerts("PENDING"):
            rows.append({
                "id": alert["id"],
                "symbol": alert["symbol"],
                "reason": alert["reason"],
                "price": alert["price"],
                "stop_loss": alert["stop_loss"],
                "profit_target": alert["profit_target"],
                "created_at": alert["created_at"],
                "message": (alert.get("detail") or {}).get("message"),
                "action": (alert.get("detail") or {}).get("action") or "sell",
            })
        return rows

    def candidates(self, force_scan=False):
        if self.histories_by_symbol is None:
            return self._candidates_from_universe(force_scan=force_scan)
        self._prepare()
        broker = self._broker()
        qualified = []
        skipped = []
        scans = []
        for symbol in self.settings.instruments:
            histories = self._histories(symbol)
            if not histories:
                scans.append(self._described(_empty_scan(symbol, "No market bars returned.")))
                skipped.append(f"{symbol} has no market bars.")
                continue
            try:
                preview = self._preview(symbol, broker, histories)
            except Exception as error:
                scans.append(self._described(_empty_scan(symbol, str(error))))
                skipped.append(f"{symbol} could not be scored: {error}")
                continue
            card = _scan_card(symbol, preview, broker)
            self._record_observation(card, histories)
            self._learn(symbol, histories)
            card = self._described(card, histories)
            scans.append(card)
            recommendation = preview.get("recommendation")
            if preview.get("decision") != "BUY" or not recommendation:
                skipped.append(
                    f"{card['symbol']} is {card['decision']}. {card['reason']} "
                    f"News {card['news']['status']}. Model {card['model']['status']}."
                )
                continue
            if card["buyable"]:
                qualified.append(card)
            else:
                skipped.append(
                    f"{card['symbol']} is BUY but risk limits permit no shares."
                )
        scans = self._with_learning_bias(scans)
        qualified = [card for card in scans if card.get("buyable")]
        qualified.sort(key=lambda item: (-(item["confidence"] or -1), -(item.get("learning_bias") or 0), item["symbol"]))
        top = qualified[:3]
        if len(qualified) >= 3:
            explanation = None
        else:
            explanation = (
                f"{len(qualified)} current BUY candidate"
                f"{'' if len(qualified) == 1 else 's'} from "
                f"{len(self.settings.instruments)} configured symbols. "
                + " ".join(skipped)
            ).strip()
        return {
            "candidates": top,
            "scans": scans,
            "top": top_scans(scans),
            "qualified_count": len(qualified),
            "explanation": explanation,
        }

    def _scan_batch_due(self):
        if not self.database.list_symbol_scans():
            return True
        stamp = self.database.get_metadata("scan_batch_at")
        if not stamp:
            return True
        try:
            when = datetime.fromisoformat(str(stamp))
        except ValueError:
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        try:
            limit = int(os.getenv("ATLAS_SCAN_BATCH_SECONDS", "90"))
        except ValueError:
            limit = 90
        age = (datetime.now(timezone.utc) - when).total_seconds()
        return age >= max(limit, 1)

    def _stops_requested(self):
        return self._scan_loop_stop is not None and self._scan_loop_stop.is_set()

    def _record_scored_pass(self, scored):
        """Remember how many names this pass actually scored. Cards stay put."""
        universe = list(self.settings.instruments)
        stored = {card["symbol"] for card in self.database.list_symbol_scans()}
        self.database.set_metadata(
            "scan_coverage",
            json.dumps({
                "universe_size": len(set(universe)),
                "scanned_count": len(stored & set(universe)),
                "batch_count": int(scored),
                "source": "config/sp500.txt",
            }),
        )

    def _feed_blocked(self, skips):
        if feed_is_paused():
            return True
        return any(reason in {"paused", "rate_limit", "timeout", "busy"} for reason in skips)

    def _scan_one_batch(self):
        universe = list(self.settings.instruments)
        if feed_is_paused():
            self._record_scored_pass(0)
            return
        start = int(self.database.get_metadata("scan_batch_index") or 0)
        batch, _nxt = next_batch(universe, start, scan_batch_size())
        if not batch:
            self.database.set_metadata("scan_batch_at", datetime.now(timezone.utc).isoformat())
            self._record_scored_pass(0)
            return
        if not self._news_explicit:
            self.news_events, self.news_report = resolve_news(batch)
            if feed_is_paused():
                self._record_scored_pass(0)
                return
        broker = self._broker()
        if auto_train_enabled() and not feed_is_paused():
            train = batch[:1] if len(universe) > scan_batch_size() else batch
            ensure_baseline_model(self.database, train)
            if feed_is_paused():
                self._record_scored_pass(0)
                return
        finished = 0
        scored = 0
        for symbol in batch:
            if self._stops_requested() or feed_is_paused():
                break
            take_skips()
            try:
                histories = self._histories(symbol)
            except Exception as error:
                if note_feed_failure(error) or feed_is_paused():
                    break
                self.database.upsert_symbol_scan(symbol, _empty_scan(symbol, str(error)))
                finished += 1
                scored += 1
                continue
            if self._feed_blocked(take_skips()):
                break
            if not histories:
                self.database.upsert_symbol_scan(symbol, _empty_scan(symbol, "No market bars returned."))
                finished += 1
                scored += 1
                continue
            try:
                preview = self._preview(symbol, broker, histories)
            except Exception as error:
                if note_feed_failure(error) or feed_is_paused():
                    break
                self.database.upsert_symbol_scan(symbol, _empty_scan(symbol, str(error)))
                finished += 1
                scored += 1
                continue
            if feed_is_paused():
                break
            card = _scan_card(symbol, preview, broker)
            self._record_observation(card, histories)
            self._learn(symbol, histories)
            card = self._described(card, histories)
            self.database.upsert_symbol_scan(symbol, card)
            finished += 1
            scored += 1
        if finished and universe:
            self.database.set_metadata(
                "scan_batch_index",
                str((start + finished) % len(universe)),
            )
            self.database.set_metadata(
                "scan_batch_at",
                datetime.now(timezone.utc).isoformat(),
            )
        self._record_scored_pass(scored)

    def _candidates_from_universe(self, force_scan=False):
        # A page read never waits on Yahoo. Refresh now only queues a batch.
        if force_scan:
            self.request_scan()
        broker = self._broker()
        equity = broker.account_info()["equity"]
        exposure = broker.current_exposure()
        sized = []
        for card in self.database.list_symbol_scans():
            card = dict(card)
            entry, stop = card.get("entry"), card.get("stop")
            held = broker.positions().get(card.get("symbol"))
            held_side = None if held is None else held.get("side", "LONG")
            sized_quantity = None
            if (
                card.get("decision") in {"BUY", "SELL"}
                and isinstance(entry, (int, float))
                and isinstance(stop, (int, float))
                and not isinstance(entry, bool)
                and not isinstance(stop, bool)
            ):
                sized_quantity = calculate_position_size(
                    equity, entry, stop, current_exposure=exposure
                )
                card["quantity"] = sized_quantity
            tradable = isinstance(sized_quantity, int) and sized_quantity > 0
            card["buyable"] = card.get("decision") == "BUY" and tradable and held_side != "SHORT"
            card["shortable"] = card.get("decision") == "SELL" and tradable and held_side != "LONG"
            sized.append(card)
        sized = self._with_learning_bias(sized)
        ranked = rank_scans(sized)
        top = [self._present_stored(card) for card in ranked[:3]]
        shown = list(top)
        seen = {card["symbol"] for card in shown}
        for symbol in broker.positions():
            if symbol in seen:
                continue
            match = next((card for card in sized if card["symbol"] == symbol), None)
            if match is not None:
                shown.append(self._present_stored(match))
        qualified = [card for card in ranked if card.get("buyable")]
        coverage = json.loads(self.database.get_metadata("scan_coverage") or "{}")
        return {
            "candidates": qualified[:3],
            "scans": shown,
            "top": top,
            "qualified_count": len(qualified),
            "explanation": coverage_note(
                coverage.get("universe_size") or len(self.settings.instruments),
                coverage.get("scanned_count") if coverage else len(sized),
                len(qualified),
                coverage.get("batch_count") or 0,
            ),
        }

    def _present_stored(self, card):
        """Refresh wording from fields already stored. Does not fetch."""
        card = dict(card)
        card["technical_copy"] = describe_technicals(card)
        card["links"] = _outbound_links(card)
        company = card.get("company") if isinstance(card.get("company"), dict) else None
        if company is not None:
            card["company_copy"] = describe_company(company)
        card["copy"] = describe_scan(card)
        return card

    def review_for_state(self):
        """Saved review, or the ledger with no new bar download."""
        if self.histories_by_symbol is not None:
            return self.review()
        raw = self.database.get_metadata("paper_review")
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and "fills" in parsed and "decisions" in parsed:
                return parsed
        return build_review(
            self.database.list_paper_orders(),
            self.database.list_scan_observations(),
            {},
        )

    def candles(self, symbol, timeframe):
        normalized = str(symbol).strip().upper()
        allowed = set(self.settings.instruments) | set(self._broker().positions())
        if normalized not in allowed:
            raise ValueError("Symbol is not on the watchlist or in the paper book.")
        requested = str(timeframe).strip()
        if requested not in self.chart_timeframes():
            raise ValueError("Timeframe is not available for the chart.")
        chart_only = requested not in self.timeframes
        if self.candle_loader is None and feed_is_paused():
            return self._cached_or_empty(
                normalized,
                requested,
                _candle_skip_message(),
                chart_only,
            )
        take_skips()
        try:
            if self.candle_loader is not None:
                frame = self.candle_loader(normalized, requested)
            else:
                histories = fetch_session_histories(normalized, (requested,))
                frame = histories.get(requested) if histories else None
        except Exception as error:
            note_feed_failure(error)
            logger.info("%s %s chart bars skipped: %s", normalized, requested, error)
            if feed_is_paused():
                return self._cached_or_empty(
                    normalized,
                    requested,
                    _candle_skip_message(),
                    chart_only,
                )
            return _annotate_feed(_chart_result(
                normalized,
                requested,
                [],
                str(error),
                chart_only,
            ))
        skips = take_skips()
        if frame is None or getattr(frame, "empty", True):
            if self.candle_loader is None and self._feed_blocked(skips):
                return self._cached_or_empty(
                    normalized,
                    requested,
                    _candle_skip_message(skips),
                    chart_only,
                )
            return _annotate_feed(_chart_result(
                normalized,
                requested,
                [],
                "No bars returned for this symbol and timeframe.",
                chart_only,
            ))
        shown = frame.tail(300)
        sma = calculate_sma(shown)
        ema = calculate_ema(shown)
        rsi = calculate_rsi(shown)
        candles = []
        for offset, (stamp, row) in enumerate(shown.iterrows()):
            volume = row["Volume"] if "Volume" in shown.columns else None
            candles.append({
                "time": stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": _finite_number(volume),
                "sma": _finite_number(sma.iloc[offset]),
                "ema": _finite_number(ema.iloc[offset]),
                "rsi": _finite_number(rsi.iloc[offset]),
            })
        payload = _chart_result(normalized, requested, candles, None, chart_only)
        self._candle_cache[(normalized, requested)] = payload
        return _annotate_feed(payload)

    def _cached_or_empty(self, symbol, timeframe, error, chart_only):
        cached = self._candle_cache.get((symbol, timeframe))
        if cached and cached.get("candles"):
            payload = dict(cached)
            payload["candles"] = list(cached["candles"])
            payload["error"] = error.replace(
                "No bars were returned.",
                "Showing the last bars returned.",
            )
            return _annotate_feed(payload)
        return _annotate_feed(_chart_result(symbol, timeframe, [], error, chart_only))

    def buy(self, token, symbol):
        self._guard(token)
        normalized = str(symbol).strip().upper()
        clicked_at = self._audit("DASHBOARD_BUY_CLICK", normalized, {})
        broker = self._broker()
        held = broker.positions().get(normalized)
        if held is not None and held.get("side") == "SHORT":
            return self._cover_short(normalized, broker, clicked_at)
        histories = self._histories(normalized)
        if not histories:
            self._audit("DASHBOARD_BUY_REJECTED", normalized, {"reason": "no bars"})
            raise ValueError(f"{normalized} has no market bars.")
        preview = self._preview(normalized, broker, histories)
        recommendation = preview.get("recommendation")
        if preview.get("decision") != "BUY" or not recommendation:
            return self._buy_from_prediction_gate(normalized, broker, histories, clicked_at)
        limits = RiskLimits()
        sized = calculate_position_size(
            broker.account_info()["equity"],
            recommendation["entry_price"],
            recommendation["stop_loss"],
            limits,
            current_exposure=broker.current_exposure(),
        )
        if sized <= 0:
            self._audit("DASHBOARD_BUY_REJECTED", normalized, {"reason": "risk size 0"})
            raise PermissionError("Risk limits permit no shares.")
        try:
            order = execute_paper_signal(
                recommendation,
                broker,
                f"dash-buy-{normalized}-{uuid.uuid4().hex[:12]}",
                approved=True,
                limits=limits,
                max_data_age_seconds=self.settings.max_market_data_age_seconds,
                approval_database=self.database,
            )
        except PermissionError as error:
            self._audit("DASHBOARD_BUY_REJECTED", normalized, {"reason": str(error)})
            raise
        self._audit(
            "DASHBOARD_BUY_FILLED",
            normalized,
            {"clicked_at": clicked_at, "order_id": order["order_id"], "quantity": order["quantity"]},
        )
        return {"ok": True, "order": _public_order(order)}

    def _cover_short(self, symbol, broker, clicked_at, alert_id=None):
        histories = self._histories(symbol)
        quote = _latest_quote(histories)
        if quote is None:
            self._audit("DASHBOARD_COVER_REJECTED", symbol, {"reason": "no price"})
            raise ValueError(f"{symbol} has no fresh price.")
        market_timestamp, price = quote
        if _quote_is_stale(market_timestamp, self.settings.max_market_data_age_seconds):
            self._audit("DASHBOARD_COVER_REJECTED", symbol, {"reason": "stale"})
            raise PermissionError("Signal rejected because source market data is stale.")
        from atlas.session import _cover_signal

        try:
            order = execute_paper_signal(
                _cover_signal(symbol, price, market_timestamp),
                broker,
                f"dash-cover-{symbol}-{uuid.uuid4().hex[:12]}",
                approved=True,
                max_data_age_seconds=self.settings.max_market_data_age_seconds,
                approval_database=self.database,
            )
        except PermissionError as error:
            self._audit("DASHBOARD_COVER_REJECTED", symbol, {"reason": str(error)})
            raise
        if alert_id is not None:
            self.database.set_exit_alert_status(alert_id, "ACCEPTED")
        self._audit(
            "DASHBOARD_COVER_FILLED",
            symbol,
            {
                "clicked_at": clicked_at,
                "order_id": order["order_id"],
                "quantity": order["quantity"],
                "effect": order.get("effect"),
                "position": "SHORT",
            },
        )
        return {"ok": True, "order": _public_order(order)}

    def _buy_from_prediction_gate(self, symbol, broker, histories, clicked_at):
        """Paper long from a passing prediction record. The scan decision is not rewritten."""
        self._learn(symbol, histories)
        prediction = self._prediction_for(symbol, histories)
        gate = prediction.get("gate") or {}
        audit = dict(gate.get("audit") or {})
        audit["clicked_at"] = clicked_at
        audit["scan_decision_unchanged"] = True
        if not gate.get("allow_buy"):
            self._audit("PREDICTION_GATE_BLOCKED", symbol, audit)
            reason = gate.get("reason") or "The prediction gate did not enable a buy."
            if gate.get("halted"):
                raise PermissionError(reason)
            if audit.get("quantity", 0) <= 0 and prediction.get("direction") == "up" and (prediction.get("track_record") or {}).get("passed"):
                raise PermissionError("Risk limits permit no shares.")
            raise ValueError(f"{symbol} is not a current BUY recommendation. {reason}")
        signal = {
            "instrument": symbol,
            "direction": "BUY",
            "entry_price": gate.get("entry"),
            "stop_loss": gate.get("stop"),
            "profit_target": gate.get("target"),
            "market_timestamp": gate.get("time"),
            "explanation": "Paper buy from the prediction gate. The scan decision was not changed.",
        }
        limits = RiskLimits()
        try:
            sized = calculate_position_size(
                broker.account_info()["equity"],
                signal["entry_price"],
                signal["stop_loss"],
                limits,
                current_exposure=broker.current_exposure(),
            )
        except (TypeError, ValueError):
            sized = 0
        if sized <= 0:
            audit["quantity"] = 0
            self._audit("PREDICTION_GATE_BLOCKED", symbol, audit)
            raise PermissionError("Risk limits permit no shares.")
        try:
            order = execute_paper_signal(
                signal,
                broker,
                f"dash-predict-{symbol}-{uuid.uuid4().hex[:12]}",
                approved=True,
                limits=limits,
                max_data_age_seconds=self.settings.max_market_data_age_seconds,
                approval_database=self.database,
            )
        except PermissionError as error:
            audit["reason"] = str(error)
            self._audit("PREDICTION_GATE_BLOCKED", symbol, audit)
            raise
        audit["order_id"] = order["order_id"]
        audit["quantity"] = order["quantity"]
        self._audit("PREDICTION_GATE_ALLOWED", symbol, audit)
        self._audit(
            "DASHBOARD_BUY_FILLED",
            symbol,
            {"clicked_at": clicked_at, "order_id": order["order_id"], "quantity": order["quantity"], "source": "prediction_gate"},
        )
        return {"ok": True, "order": _public_order(order)}

    def sell(self, token, symbol, alert_id=None):
        self._guard(token)
        normalized = str(symbol).strip().upper()
        action = "DASHBOARD_SELL_CLICK" if alert_id is None else "DASHBOARD_EXIT_ACCEPTED"
        clicked_at = self._audit(action, normalized, {"alert_id": alert_id})
        broker = self._broker()
        held = broker.positions().get(normalized)
        held_side = None if held is None else held.get("side", "LONG")
        if held_side == "SHORT":
            self._audit("DASHBOARD_SELL_REJECTED", normalized, {"reason": "already short"})
            raise ValueError(f"{normalized} is already a paper short. Buy covers it.")
        if held_side == "LONG":
            return self._close_long(normalized, broker, clicked_at, alert_id)
        histories = self._histories(normalized)
        if not histories:
            self._audit("DASHBOARD_SHORT_REJECTED", normalized, {"reason": "no bars"})
            raise ValueError(f"{normalized} has no market bars.")
        preview = self._preview(normalized, broker, histories)
        recommendation = preview.get("recommendation")
        if preview.get("decision") != "SELL" or not recommendation:
            self._audit(
                "DASHBOARD_SHORT_REJECTED",
                normalized,
                {"reason": preview.get("decision")},
            )
            raise ValueError(f"{normalized} is not a current SELL. A wait is not a short.")
        limits = RiskLimits()
        sized = calculate_position_size(
            broker.account_info()["equity"],
            recommendation["entry_price"],
            recommendation["stop_loss"],
            limits,
            current_exposure=broker.current_exposure(),
        )
        if sized <= 0:
            self._audit("DASHBOARD_SHORT_REJECTED", normalized, {"reason": "risk size 0"})
            raise PermissionError("Risk limits permit no shares.")
        try:
            order = execute_paper_signal(
                recommendation,
                broker,
                f"dash-short-{normalized}-{uuid.uuid4().hex[:12]}",
                approved=True,
                limits=limits,
                max_data_age_seconds=self.settings.max_market_data_age_seconds,
                approval_database=self.database,
            )
        except PermissionError as error:
            self._audit("DASHBOARD_SHORT_REJECTED", normalized, {"reason": str(error)})
            raise
        if order.get("effect") != "OPEN_SHORT":
            self._audit("DASHBOARD_SHORT_REJECTED", normalized, {"reason": order.get("effect")})
            raise ValueError(f"{normalized} sell did not open a short.")
        self._audit(
            "DASHBOARD_SHORT_FILLED",
            normalized,
            {
                "clicked_at": clicked_at,
                "order_id": order["order_id"],
                "quantity": order["quantity"],
                "effect": order.get("effect"),
                "position": "SHORT",
            },
        )
        return {"ok": True, "order": _public_order(order)}

    def _close_long(self, symbol, broker, clicked_at, alert_id=None):
        histories = self._histories(symbol)
        quote = _latest_quote(histories)
        if quote is None:
            raise ValueError(f"{symbol} has no fresh price.")
        market_timestamp, price = quote
        if _quote_is_stale(market_timestamp, self.settings.max_market_data_age_seconds):
            raise PermissionError("Signal rejected because source market data is stale.")
        signal = _exit_signal(symbol, price, market_timestamp)
        try:
            order = execute_paper_signal(
                signal,
                broker,
                f"dash-sell-{symbol}-{uuid.uuid4().hex[:12]}",
                approved=True,
                max_data_age_seconds=self.settings.max_market_data_age_seconds,
                approval_database=self.database,
            )
        except PermissionError as error:
            self._audit("DASHBOARD_SELL_REJECTED", symbol, {"reason": str(error)})
            raise
        if order.get("effect") != "CLOSE_LONG":
            raise ValueError(f"{symbol} sell did not close the long.")
        if alert_id is not None:
            self.database.set_exit_alert_status(alert_id, "ACCEPTED")
        self._audit(
            "DASHBOARD_SELL_FILLED",
            symbol,
            {
                "clicked_at": clicked_at,
                "order_id": order["order_id"],
                "quantity": order["quantity"],
                "effect": order.get("effect"),
                "position": "LONG",
            },
        )
        return {"ok": True, "order": _public_order(order)}

    def accept_alert(self, token, alert_id):
        self._guard(token)
        alert = self.database.get_exit_alert(alert_id)
        if alert is None or alert["status"] != "PENDING":
            raise ValueError("That exit recommendation is no longer pending.")
        symbol = str(alert["symbol"]).strip().upper()
        held = self._broker().positions().get(symbol)
        if held is not None and held.get("side") == "SHORT":
            clicked_at = self._audit(
                "DASHBOARD_EXIT_ACCEPTED",
                symbol,
                {"alert_id": alert["id"], "position": "SHORT"},
            )
            return self._cover_short(symbol, self._broker(), clicked_at, alert_id=alert["id"])
        return self.sell(token, alert["symbol"], alert_id=alert["id"])


def lookup_instrument_name(symbol):
    """Return Yahoo's short or long name when the feed includes one."""
    from atlas.market_data import call_yahoo, feed_is_paused

    if feed_is_paused():
        return None

    def read():
        import yfinance as yf

        return getattr(yf.Ticker(str(symbol).strip().upper()), "info", None) or {}

    info, error = call_yahoo(read)
    if error is not None or not isinstance(info, dict):
        return None
    for key in ("shortName", "longName"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _outbound_links(card):
    """Yahoo pages for this symbol, plus a website only when the feed returned one."""
    company = card.get("company") if isinstance(card.get("company"), dict) else {}
    return {
        "quote": yahoo_quote_url(card.get("symbol")),
        "profile": yahoo_profile_url(card.get("symbol")),
        "website": http_url(company.get("website")),
    }


def describe_scan(card):
    """Plain-language lines for one symbol. Uses only fields already on the card."""
    decision = card.get("decision") or "unavailable"
    decision_text = {
        "BUY": "This is a buy. The checks that have to agree are in favor of buying.",
        "SELL": "This is a sell. The checks that have to agree are in favor of selling.",
        "WAIT": "This is a wait. It is not a buy or a sell right now.",
        "NO_DATA": "There are no price bars, so there is nothing to trade.",
    }.get(decision, "The scan did not return a buy or a sell.")
    reason = str(card.get("reason") or "").strip()
    if "Insufficient timeframe agreement" in reason:
        why = "The timeframes do not agree, so this stays a wait."
    elif reason:
        why = reason if reason.endswith(".") else reason + "."
    elif decision == "WAIT":
        why = "The timeframes do not agree on a buy or a sell."
    else:
        why = decision_text
    lines = []
    labels = {
        "1m": "1 minute",
        "5m": "5 minute",
        "15m": "15 minute",
        "30m": "30 minute",
        "1h": "1 hour",
        "1d": "daily",
        "1w": "weekly",
    }
    signal_words = {"BUY": "buy", "SELL": "sell", "WAIT": "wait"}
    trend_words = {"BULLISH": "up", "BEARISH": "down", "NEUTRAL": "flat"}
    for name, frame in (card.get("timeframes") or {}).items():
        label = labels.get(name, name)
        signal = signal_words.get(frame.get("signal"), "no clear signal")
        parts = [f"The {label} chart says {signal}."]
        trend = trend_words.get(frame.get("trend"))
        if trend:
            parts.append(f"The trend is {trend}.")
        if frame.get("bullish_score") is not None and frame.get("bearish_score") is not None:
            parts.append(
                f"It counted {frame['bullish_score']} reasons to buy and {frame['bearish_score']} reasons to sell."
            )
        lines.append(" ".join(parts))
    news = card.get("news") or {}
    news_status = news.get("status")
    headlines = news.get("headlines") or []
    if news_status == "FETCH_FAILED":
        news_text = "The news feed did not return headlines."
    elif not news_status:
        news_text = "News has not been loaded for this symbol."
    elif not headlines:
        news_text = "No headlines were matched to this symbol."
    else:
        news_text = (
            f"The feed returned {len(headlines)} headline."
            if len(headlines) == 1
            else f"The feed returned {len(headlines)} headlines."
        )
    model = card.get("model") or {}
    model_status = model.get("status")
    probability = model.get("probability_up")
    probability_text = None
    if isinstance(probability, (int, float)) and not isinstance(probability, bool):
        probability_text = (
            f"It calculated a {float(probability):.0%} chance the next daily bar closes higher."
        )
    if model_status == "CANDIDATE_NOT_PROMOTED":
        model_text = (
            "A model was trained, but it did not pass the checks required before it can help decide trades."
        )
        if probability_text:
            model_text += " " + probability_text + " That number is not being used for the decision."
    elif model_status == "ACTIVE":
        model_text = "A promoted model is allowed to confirm or withhold a buy or sell."
        if probability_text:
            model_text += " " + probability_text
        if model.get("signal"):
            model_text += f" The model itself says {str(model['signal']).lower()}."
    elif model_status == "INSUFFICIENT_HISTORY":
        model_text = "There was not enough price history to train a model."
    elif model_status == "NO_ACTIVE_MODEL":
        model_text = "No model is stored yet."
    elif model_status:
        model_text = str(model.get("reason") or "The model did not return a decision.")
        if probability_text:
            model_text += " " + probability_text
    else:
        model_text = "No model result is available."
    buy_enabled = bool(card.get("buyable"))
    return {
        "decision": decision_text,
        "why": why,
        "timeframes": lines,
        "news": news_text,
        "model": model_text,
        "buy_enabled": buy_enabled,
        "buy_note": (
            "Buy is available. You still have to confirm before any paper order is sent."
            if buy_enabled
            else why
        ),
        "sell_note": (
            "Sell can open a paper short at the risk size. You still have to confirm. "
            "An open long is closed instead and is not flipped into a short."
            if card.get("shortable")
            else "No open paper position. Sell stays off unless a real sell has a risk size above zero."
        ),
    }


_TECHNICAL_FIELDS = (
    ("trend", "Trend"),
    ("rsi", "RSI"),
    ("macd", "MACD"),
    ("bullish_score", "Reasons to buy"),
    ("bearish_score", "Reasons to sell"),
    ("breakout", "Breakout"),
    ("vwap", "VWAP"),
)
_TECHNICAL_EXTRAS = (
    ("momentum", "Momentum"),
    ("volume", "Volume"),
    ("volatility", "Volatility"),
    ("mean_reversion", "Mean reversion"),
    ("vwap_price", "VWAP price"),
    ("z_score", "Z score"),
    ("momentum_pct", "Momentum percent"),
    ("volume_ratio", "Volume ratio"),
)
_TIMEFRAME_LABELS = {
    "1m": "1 minute",
    "5m": "5 minute",
    "15m": "15 minute",
    "30m": "30 minute",
    "1h": "1 hour",
    "1d": "daily",
    "1w": "weekly",
}


def describe_technicals(card):
    """Plain readout of scan observations. Missing fields stay missing."""
    frames = card.get("timeframes") or {}
    if not frames:
        return {
            "lines": ["The scan did not return technical observations for this symbol."],
        }
    lines = []
    for name, frame in frames.items():
        label = _TIMEFRAME_LABELS.get(name, name)
        observation = frame if isinstance(frame, dict) else {}
        lines.append(f"{label.capitalize()} chart")
        for key, title in _TECHNICAL_FIELDS:
            value = observation.get(key)
            if value is None or value == "":
                lines.append(f"The {label} scan did not return {title}.")
            else:
                lines.append(f"{title} on the {label} chart: {value}.")
        for key, title in _TECHNICAL_EXTRAS:
            if key not in observation or observation.get(key) is None or observation.get(key) == "":
                continue
            lines.append(f"{title} on the {label} chart: {observation[key]}.")
    return {"lines": lines}


def _empty_scan(symbol, reason):
    return {
        "symbol": str(symbol).strip().upper(),
        "decision": "NO_DATA",
        "reason": reason,
        "price": None,
        "entry": None,
        "stop": None,
        "target": None,
        "quantity": None,
        "confidence": None,
        "reasons": [reason],
        "buyable": False,
        "shortable": False,
        "agreement": None,
        "scanned_at": None,
        "timeframes": {},
        "news": {"status": None, "headlines": [], "items": [], "source": None},
        "model": {
            "status": None,
            "reason": None,
            "version": None,
            "probability_up": None,
            "signal": None,
        },
    }


def _scan_card(symbol, preview, broker):
    analysis = preview.get("analysis") or {}
    recommendation = preview.get("recommendation")
    decision = preview.get("decision") or analysis.get("status") or "unavailable"
    quantity = None
    entry = stop = target = None
    held = broker.positions().get(str(symbol).strip().upper())
    held_side = None if held is None else held.get("side", "LONG")
    if recommendation and decision in {"BUY", "SELL"}:
        entry = recommendation.get("entry_price")
        stop = recommendation.get("stop_loss")
        target = recommendation.get("profit_target")
        if (
            isinstance(entry, (int, float))
            and isinstance(stop, (int, float))
            and not isinstance(entry, bool)
            and not isinstance(stop, bool)
        ):
            quantity = calculate_position_size(
                broker.account_info()["equity"],
                entry,
                stop,
                current_exposure=broker.current_exposure(),
            )
    tradable = isinstance(quantity, int) and quantity > 0
    news = preview.get("news") or {}
    adaptive = preview.get("adaptive") or {}
    prediction = adaptive.get("prediction") or {}
    timeframes = {}
    for name, frame in (analysis.get("timeframes") or {}).items():
        strategy = (frame or {}).get("strategy") or {}
        observations = strategy.get("observations") or {}
        timeframes[name] = {
            "signal": strategy.get("signal"),
            "status": strategy.get("status"),
            "confidence": strategy.get("confidence"),
            "trend": observations.get("trend"),
            "rsi": observations.get("rsi"),
            "macd": observations.get("macd"),
            "breakout": observations.get("breakout"),
            "vwap": observations.get("vwap"),
            "bullish_score": observations.get("bullish_score"),
            "bearish_score": observations.get("bearish_score"),
            "momentum": observations.get("momentum"),
            "volume": observations.get("volume"),
            "volatility": observations.get("volatility"),
            "mean_reversion": observations.get("mean_reversion"),
            "vwap_price": _finite_number(observations.get("vwap_price")),
            "z_score": _finite_number(observations.get("z_score")),
            "momentum_pct": _finite_number(observations.get("momentum_pct")),
            "volume_ratio": _finite_number(observations.get("volume_ratio")),
        }
    return {
        "symbol": (recommendation or {}).get("instrument") or str(symbol).strip().upper(),
        "decision": decision,
        "reason": analysis.get("reason") or "",
        "price": analysis.get("price"),
        "entry": entry,
        "stop": stop,
        "target": target,
        "quantity": quantity,
        "confidence": _buy_confidence(preview),
        "reasons": _reasons(preview),
        "buyable": decision == "BUY" and tradable and held_side != "SHORT",
        "shortable": decision == "SELL" and tradable and held_side != "LONG",
        "agreement": analysis.get("agreement"),
        "scanned_at": preview.get("scanned_at"),
        "timeframes": timeframes,
        "news": {
            "status": news.get("status"),
            "headlines": [str(item) for item in (news.get("headlines") or [])],
            "items": [item for item in (news.get("items") or []) if isinstance(item, dict)],
            "source": news.get("source"),
        },
        "model": {
            "status": adaptive.get("status"),
            "reason": adaptive.get("reason"),
            "version": adaptive.get("version"),
            "probability_up": prediction.get("probability_up"),
            "signal": prediction.get("signal"),
        },
    }


def _buy_confidence(preview):
    scores = []
    for frame in ((preview.get("analysis") or {}).get("timeframes") or {}).values():
        strategy = (frame or {}).get("strategy") or {}
        if strategy.get("signal") != "BUY":
            continue
        value = strategy.get("confidence")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            scores.append(float(value))
    return max(scores) if scores else None


def _reasons(preview):
    reasons = []
    recommendation = preview.get("recommendation") or {}
    if recommendation.get("explanation"):
        reasons.append(str(recommendation["explanation"]))
    reasons.append(f"Technical decision: {preview.get('decision')}")
    news = preview.get("news") or {}
    reasons.append(f"News: {news.get('status', 'not assessed')}")
    headlines = news.get("headlines") or []
    if headlines:
        reasons.append("Headlines: " + " | ".join(str(item) for item in headlines[:3]))
    prediction = (preview.get("adaptive") or {}).get("prediction") or {}
    if isinstance(prediction.get("probability_up"), (int, float)):
        reasons.append(
            f"Model probability up {float(prediction['probability_up']):.4f} "
            f"signal {prediction.get('signal')}"
        )
    adaptive = preview.get("adaptive") or {}
    model = adaptive.get("status", "not assessed")
    if adaptive.get("version"):
        model = f"{model} {adaptive['version']}"
    reasons.append(f"Model: {model}")
    for frame in ((preview.get("analysis") or {}).get("timeframes") or {}).values():
        strategy = (frame or {}).get("strategy") or {}
        if strategy.get("signal") != "BUY":
            continue
        observations = strategy.get("observations") or {}
        bits = []
        for key in ("trend", "breakout", "rsi", "macd", "vwap"):
            if observations.get(key):
                bits.append(f"{key}={observations[key]}")
        if bits:
            reasons.append("Signals: " + ", ".join(bits))
        break
    return reasons


def chart_tooltip_fields(bar, indicators=None):
    """Labeled tooltip rows from one returned bar.

    A missing price, volume, or study is left out. A volume of 0 is kept
    because that is a real value. No other number is filled in.
    """
    bar = bar or {}
    indicators = indicators or {}
    rows = []
    if bar.get("time"):
        rows.append({"label": "Time", "value": str(bar["time"])})
    for label, key in (("Open", "open"), ("High", "high"), ("Low", "low"), ("Close", "close")):
        value = _finite_number(bar.get(key))
        if value is not None:
            rows.append({"label": label, "value": value})
    volume = _finite_number(bar.get("volume"))
    if volume is not None:
        rows.append({"label": "Volume", "value": volume})
    for key, label in (("sma", "SMA"), ("ema", "EMA"), ("rsi", "RSI")):
        value = _finite_number(bar.get(key))
        if value is None:
            continue
        period = indicators.get(key)
        name = f"{label} {period}".strip() if period else label
        rows.append({"label": name, "value": value})
    return rows


def _finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _risk_usage(account, daily_pnl, exposure, halted, limits=None):
    """Limits from RiskLimits and usage from the paper account. No estimates."""
    limits = limits or RiskLimits()
    equity = account.get("equity")
    exposure_fraction = None
    loss_limit = None
    exposure_cap = None
    loss_used = None
    if isinstance(equity, (int, float)) and not isinstance(equity, bool) and equity > 0:
        exposure_fraction = float(exposure) / float(equity)
        loss_limit = float(equity) * limits.max_daily_loss_fraction
        exposure_cap = float(equity) * limits.max_position_fraction
        if (
            isinstance(daily_pnl, (int, float))
            and not isinstance(daily_pnl, bool)
            and daily_pnl < 0
        ):
            loss_used = min(1.0, (-float(daily_pnl)) / float(equity))
        else:
            loss_used = 0.0
    return {
        "per_trade": limits.risk_per_trade_fraction,
        "max_exposure": limits.max_position_fraction,
        "daily_loss": limits.max_daily_loss_fraction,
        "cash": account.get("cash"),
        "equity": equity,
        "daily_pnl": daily_pnl,
        "exposure": exposure,
        "exposure_fraction": exposure_fraction,
        "exposure_cap": exposure_cap,
        "loss_limit": loss_limit,
        "loss_used_fraction": loss_used,
        "halted": bool(halted),
    }


def _public_order(order):
    return {
        "order_id": order["order_id"],
        "symbol": order["symbol"],
        "side": order["side"],
        "effect": order.get("effect"),
        "quantity": order["quantity"],
        "price": order["price"],
        "status": order["status"],
    }


def _state_payload(service, force_scan=False):
    try:
        portfolio = service.portfolio()
    except Exception as error:
        logger.info("Portfolio read failed: %s", error)
        portfolio = {
            "mode": "PAPER",
            "live_trading": False,
            "cash": None,
            "equity": None,
            "daily_pnl": None,
            "positions": [],
            "recent_fills": [],
            "instruments": [],
            "timeframes": [],
            "error": str(error),
        }
    try:
        ranked = service.candidates(force_scan=force_scan)
    except Exception as error:
        logger.info("Scan read failed: %s", error)
        ranked = {
            "candidates": [],
            "scans": [],
            "top": [],
            "qualified_count": 0,
            "explanation": str(error),
        }
    try:
        alerts = service.alerts()
    except Exception as error:
        logger.info("Alert read failed: %s", error)
        alerts = []
    try:
        clock = service.clock()
    except Exception as error:
        logger.info("Clock read failed: %s", error)
        clock = None
    try:
        review = service.review_for_state()
    except Exception as error:
        logger.info("Review read failed: %s", error)
        review = {
            "fills": [],
            "decisions": [],
            "scoreboard": None,
            "scoreboard_note": str(error),
            "model": {"note": str(error), "graded": 0, "matched": 0},
        }
    status = feed_status()
    return {
        "portfolio": portfolio,
        "candidates": ranked["candidates"],
        "scans": ranked.get("scans") or [],
        "candidate_explanation": ranked["explanation"],
        "top": ranked.get("top") or ranked["scans"],
        "qualified_count": ranked["qualified_count"],
        "alerts": alerts,
        "clock": clock,
        "review": review,
        "chart_timeframes": _chart_timeframes(service),
        "chart_refresh_seconds": CHART_REFRESH_SECONDS,
        "market_note": service.market_note(),
        "scan_pending": service.scan_pending(),
        "feed_paused": bool(status),
        "feed_retry_after_seconds": status["seconds_remaining"] if status else 0,
    }


class _DashboardHandler(BaseHTTPRequestHandler):
    service = None

    def log_message(self, fmt, *args):
        logger.info("dashboard " + fmt, *args)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in {"127.0.0.1", "localhost"}

    def _send(self, status, payload, content_type="application/json"):
        if isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _reject_remote(self):
        if self._host_ok():
            return False
        self._send(403, {"ok": False, "error": "Dashboard is localhost only."})
        return True

    def do_GET(self):
        if self._reject_remote():
            return
        path = self.path.split("?", 1)[0]
        try:
            if path in {"/", "/index.html"}:
                page = _PAGE.replace("__ATLAS_LOCAL_TOKEN__", self.service.token)
                self._send(200, page, "text/html; charset=utf-8")
                return
            if path == "/api/state":
                params = parse_qs(urlsplit(self.path).query)
                force = (params.get("scan") or ["0"])[0] in {"1", "true", "yes"}
                self._send(200, _state_payload(self.service, force_scan=force))
                return
            if path == "/api/candles":
                params = parse_qs(urlsplit(self.path).query)
                self._send(200, self.service.candles(
                    (params.get("symbol") or [""])[0],
                    (params.get("timeframe") or [""])[0],
                ))
                return
            self._send(404, {"ok": False, "error": "Not found."})
        except Exception as error:
            logger.info("Dashboard read failed: %s", error)
            self._send(400, {"ok": False, "error": str(error)})

    def do_POST(self):
        if self._reject_remote():
            return
        if self.headers.get("X-Atlas-Token") != self.service.token:
            self._send(403, {"ok": False, "error": "Local approval token required."})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "JSON body required."})
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/buy":
                result = self.service.buy(self.service.token, body.get("symbol"))
            elif path == "/api/sell":
                result = self.service.sell(self.service.token, body.get("symbol"))
            elif path == "/api/alerts/accept":
                result = self.service.accept_alert(self.service.token, body.get("alert_id"))
            else:
                self._send(404, {"ok": False, "error": "Not found."})
                return
            self._send(200, result)
        except PermissionError as error:
            self._send(403, {"ok": False, "error": str(error)})
        except ValueError as error:
            self._send(400, {"ok": False, "error": str(error)})
        except Exception as error:
            logger.info("Dashboard order failed: %s", error)
            self._send(400, {"ok": False, "error": str(error)})


def make_server(service, port=0):
    """Bind a paper dashboard on localhost. Port 0 asks the OS for a free port."""
    _DashboardHandler.service = service
    return ThreadingHTTPServer(("127.0.0.1", int(port)), _DashboardHandler)


def serve_dashboard(port=None, service=None):
    """Serve the paper dashboard on 127.0.0.1 only. This call blocks."""
    if LIVE_TRADING:
        raise PermissionError("Live trading is disabled.")
    settings = load_settings()
    if settings.live_trading_enabled:
        raise PermissionError("Live trading is disabled.")
    port = int(port or os.getenv("ATLAS_DASHBOARD_PORT", "8765"))
    if service is None:
        service = PaperDashboard(
            token=secrets.token_urlsafe(24),
            settings=settings,
            name_loader=lookup_instrument_name,
        )
    _DashboardHandler.service = service
    service.start_background_scans()
    server = ThreadingHTTPServer(("127.0.0.1", port), _DashboardHandler)
    host, bound_port = server.server_address
    logger.info(
        "ATLAS paper dashboard http://%s:%s  PAPER ONLY. Live trading is disabled.",
        host,
        bound_port,
    )
    print(
        f"ATLAS paper dashboard http://{host}:{bound_port}  "
        "PAPER ONLY. Live trading is disabled."
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()
