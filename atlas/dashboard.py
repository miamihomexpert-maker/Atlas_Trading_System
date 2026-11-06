from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit
import uuid

from atlas.broker import PaperBroker
from atlas.config import LIVE_TRADING
from atlas.database import AtlasDatabase
from atlas.execution import execute_paper_signal
from atlas.logger import get_logger
from atlas.risk import RiskLimits, calculate_position_size, validate_daily_loss
from atlas.baseline import auto_train_enabled, ensure_baseline_model
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
from config.settings import load_settings


logger = get_logger()
_PAGE = (Path(__file__).with_name("dashboard_page.html")).read_text(encoding="utf-8")


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
        self.timeframes = resolve_timeframes()
        self.hard_stop = hard_stop_enabled(hard_stop)

    @property
    def database(self):
        return self._database

    def close(self):
        if self._owns_database and self._database is not None:
            self._database.close()
            self._database = None

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
        }

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
            })
        return rows

    def candidates(self):
        self._prepare()
        broker = self._broker()
        qualified = []
        skipped = []
        scans = []
        for symbol in self.settings.instruments:
            histories = self._histories(symbol)
            if not histories:
                scans.append(_empty_scan(symbol, "No market bars returned."))
                skipped.append(f"{symbol} has no market bars.")
                continue
            try:
                preview = self._preview(symbol, broker, histories)
            except Exception as error:
                scans.append(_empty_scan(symbol, str(error)))
                skipped.append(f"{symbol} could not be scored: {error}")
                continue
            card = _scan_card(symbol, preview, broker)
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
        qualified.sort(key=lambda item: (-(item["confidence"] or -1), item["symbol"]))
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
            "qualified_count": len(qualified),
            "explanation": explanation,
        }

    def candles(self, symbol, timeframe):
        normalized = str(symbol).strip().upper()
        allowed = set(self.settings.instruments) | set(self._broker().positions())
        if normalized not in allowed:
            raise ValueError("Symbol is not on the watchlist or in the paper book.")
        if timeframe not in self.timeframes:
            raise ValueError("Timeframe is not configured.")
        if self.candle_loader is not None:
            frame = self.candle_loader(normalized, timeframe)
        else:
            histories = fetch_session_histories(normalized, (timeframe,))
            frame = histories.get(timeframe) if histories else None
        if frame is None or getattr(frame, "empty", True):
            return {
                "symbol": normalized,
                "timeframe": timeframe,
                "candles": [],
                "error": "No bars returned for this symbol and timeframe.",
            }
        candles = []
        for stamp, row in frame.tail(300).iterrows():
            candles.append({
                "time": stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            })
        return {
            "symbol": normalized,
            "timeframe": timeframe,
            "candles": candles,
            "error": None,
        }

    def buy(self, token, symbol):
        self._guard(token)
        normalized = str(symbol).strip().upper()
        clicked_at = self._audit("DASHBOARD_BUY_CLICK", normalized, {})
        broker = self._broker()
        histories = self._histories(normalized)
        if not histories:
            self._audit("DASHBOARD_BUY_REJECTED", normalized, {"reason": "no bars"})
            raise ValueError(f"{normalized} has no market bars.")
        preview = self._preview(normalized, broker, histories)
        recommendation = preview.get("recommendation")
        if preview.get("decision") != "BUY" or not recommendation:
            self._audit(
                "DASHBOARD_BUY_REJECTED",
                normalized,
                {"reason": preview.get("decision")},
            )
            raise ValueError(f"{normalized} is not a current BUY recommendation.")
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

    def sell(self, token, symbol, alert_id=None):
        self._guard(token)
        normalized = str(symbol).strip().upper()
        action = "DASHBOARD_SELL_CLICK" if alert_id is None else "DASHBOARD_EXIT_ACCEPTED"
        clicked_at = self._audit(action, normalized, {"alert_id": alert_id})
        broker = self._broker()
        if normalized not in broker.positions():
            raise ValueError(f"{normalized} has no open paper position.")
        histories = self._histories(normalized)
        quote = _latest_quote(histories)
        if quote is None:
            raise ValueError(f"{normalized} has no fresh price.")
        market_timestamp, price = quote
        if _quote_is_stale(market_timestamp, self.settings.max_market_data_age_seconds):
            raise PermissionError("Signal rejected because source market data is stale.")
        signal = _exit_signal(normalized, price, market_timestamp)
        try:
            order = execute_paper_signal(
                signal,
                broker,
                f"dash-sell-{normalized}-{uuid.uuid4().hex[:12]}",
                approved=True,
                max_data_age_seconds=self.settings.max_market_data_age_seconds,
                approval_database=self.database,
            )
        except PermissionError as error:
            self._audit("DASHBOARD_SELL_REJECTED", normalized, {"reason": str(error)})
            raise
        if alert_id is not None:
            self.database.set_exit_alert_status(alert_id, "ACCEPTED")
        self._audit(
            "DASHBOARD_SELL_FILLED",
            normalized,
            {"clicked_at": clicked_at, "order_id": order["order_id"], "quantity": order["quantity"]},
        )
        return {"ok": True, "order": _public_order(order)}

    def accept_alert(self, token, alert_id):
        self._guard(token)
        alert = self.database.get_exit_alert(alert_id)
        if alert is None or alert["status"] != "PENDING":
            raise ValueError("That sell recommendation is no longer pending.")
        return self.sell(token, alert["symbol"], alert_id=alert["id"])


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
        "agreement": None,
        "scanned_at": None,
        "timeframes": {},
        "news": {"status": None, "headlines": [], "source": None},
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
    if recommendation and decision == "BUY":
        entry = recommendation.get("entry_price")
        stop = recommendation.get("stop_loss")
        target = recommendation.get("profit_target")
        if isinstance(entry, (int, float)) and isinstance(stop, (int, float)):
            quantity = calculate_position_size(
                broker.account_info()["equity"],
                entry,
                stop,
                current_exposure=broker.current_exposure(),
            )
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
        "buyable": decision == "BUY" and isinstance(quantity, int) and quantity > 0,
        "agreement": analysis.get("agreement"),
        "scanned_at": preview.get("scanned_at"),
        "timeframes": timeframes,
        "news": {
            "status": news.get("status"),
            "headlines": list(news.get("headlines") or []),
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


def _public_order(order):
    return {
        "order_id": order["order_id"],
        "symbol": order["symbol"],
        "side": order["side"],
        "quantity": order["quantity"],
        "price": order["price"],
        "status": order["status"],
    }


def _state_payload(service):
    portfolio = service.portfolio()
    ranked = service.candidates()
    return {
        "portfolio": portfolio,
        "candidates": ranked["candidates"],
        "scans": ranked.get("scans") or [],
        "candidate_explanation": ranked["explanation"],
        "qualified_count": ranked["qualified_count"],
        "alerts": service.alerts(),
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
                self._send(200, _state_payload(self.service))
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
        service = PaperDashboard(token=secrets.token_urlsafe(24), settings=settings)
    _DashboardHandler.service = service
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
