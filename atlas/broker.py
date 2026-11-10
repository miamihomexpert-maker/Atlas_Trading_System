from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite

from atlas.risk import RiskLimits, calculate_position_size, validate_daily_loss


class PaperBroker:
    """In-memory paper broker for longs and shorts. It has no live-broker connection.

    Fills enforce risk limits even when ``approved=True``. When a database is
    attached and already has a ledger, cash and positions are rebuilt from it.
    A short is a positive quantity with side SHORT. Selling a long only reduces
    that long. Buying a short only covers it.
    """

    def __init__(self, initial_cash=100_000.0, database=None):
        if not isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("Initial paper cash must be a positive finite amount.")
        self._cash = float(initial_cash)
        self._positions = {}
        self._orders = {}
        self._history = []
        self._realized_pnl = 0.0
        self._database = database
        if self._database is not None and self._database_has_state():
            self._restore_from_database()
        elif self._database is not None:
            self._database.record_initial_account(self.account_info())
        self._pnl_day = datetime.now(timezone.utc).date().isoformat()
        self._day_open_equity = self.account_info()["equity"]

    def _database_has_state(self):
        has_state = getattr(self._database, "has_paper_state", None)
        return bool(has_state()) if callable(has_state) else False

    def _restore_from_database(self):
        opening = self._database.first_equity_snapshot()
        if opening is not None:
            self._cash = float(opening["equity"])
            self._realized_pnl = float(opening["realized_pnl"])
        for row in self._database.replay_paper_orders():
            order = {
                "symbol": str(row["symbol"]).strip().upper(),
                "side": str(row["side"]).strip().upper(),
                "quantity": int(row["quantity"]),
                "price": float(row["price"]),
                "client_order_id": str(row["order_id"]),
                "effect": row.get("effect") or None,
            }
            realized_delta = self._apply_position_change(order)
            filled = {
                **order,
                "status": "FILLED",
                "order_id": order["client_order_id"],
                "realized_pnl": realized_delta,
            }
            self._orders[filled["order_id"]] = filled
            self._history.append(filled)
        self._reconcile_marks_to_latest_equity()
        self._restore_position_plans()

    def _restore_position_plans(self):
        list_plans = getattr(self._database, "list_position_plans", None)
        if not callable(list_plans):
            return
        for plan in list_plans():
            symbol = str(plan["symbol"]).strip().upper()
            if symbol not in self._positions:
                continue
            self._positions[symbol]["stop_loss"] = float(plan["stop_loss"])
            self._positions[symbol]["profit_target"] = float(plan["profit_target"])
            if plan.get("side") in {"LONG", "SHORT"}:
                self._positions[symbol]["side"] = plan["side"]

    def _reconcile_marks_to_latest_equity(self):
        latest = self._database.latest_equity_snapshot()
        if not latest or not self._positions:
            return
        target_position_value = float(latest["equity"]) - self._cash
        current_value = sum(self._signed_value(position) for position in self._positions.values())
        if current_value == 0 or not isfinite(target_position_value):
            return
        scale = target_position_value / current_value
        if not isfinite(scale) or scale <= 0:
            return
        for position in self._positions.values():
            position["last_price"] = position["last_price"] * scale

    def _signed_value(self, position):
        value = position["quantity"] * position["last_price"]
        if position.get("side", "LONG") == "SHORT":
            return -value
        return value

    def _held_side(self, symbol):
        position = self._positions.get(str(symbol).strip().upper())
        if position is None:
            return None
        return position.get("side", "LONG")

    def account_info(self):
        position_value = sum(self._signed_value(position) for position in self._positions.values())
        return {
            "cash": round(self._cash, 2),
            "buying_power": round(self._cash, 2),
            "equity": round(self._cash + position_value, 2),
            "position_value": round(position_value, 2),
            "realized_pnl": round(self._realized_pnl, 2),
            "positions_count": len(self._positions),
            "trading_mode": "PAPER",
        }

    def positions(self):
        return {
            symbol: dict(position)
            for symbol, position in self._positions.items()
        }

    def current_exposure(self):
        return sum(
            abs(position["quantity"] * position["last_price"])
            for position in self._positions.values()
        )

    def daily_pnl(self, now=None):
        """Equity change versus the UTC day's opening equity.

        With a database, the reference is the last snapshot before today, or
        the first snapshot from today when the book started today.
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must include a timezone.")
        day_start = now.astimezone(timezone.utc).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        reference = None
        if self._database is not None and hasattr(self._database, "equity_before"):
            reference = self._database.equity_before(day_start.isoformat())
            if reference is None and hasattr(self._database, "earliest_equity_from"):
                reference = self._database.earliest_equity_from(day_start.isoformat())
        if reference is None:
            day_key = day_start.date().isoformat()
            if self._pnl_day != day_key:
                self._day_open_equity = self.account_info()["equity"]
                self._pnl_day = day_key
            reference = self._day_open_equity
        return self.account_info()["equity"] - float(reference)

    def update_market_price(self, symbol, price):
        symbol = str(symbol).strip().upper()
        if not isfinite(price) or price <= 0:
            raise ValueError("Market price must be a positive finite number.")
        if symbol in self._positions:
            previous_price = self._positions[symbol]["last_price"]
            self._positions[symbol]["last_price"] = float(price)
            if self._database is not None:
                try:
                    self._database.record_account_snapshot(self.account_info())
                except Exception:
                    self._positions[symbol]["last_price"] = previous_price
                    raise

    def trade_history(self):
        return [dict(order) for order in self._history]

    def validate_order(self, symbol, side, quantity, price, client_order_id):
        symbol = str(symbol).strip().upper()
        side = str(side).strip().upper()
        if not symbol:
            raise ValueError("Instrument symbol is required.")
        if side not in {"BUY", "SELL"}:
            raise ValueError("Paper orders support BUY or SELL only.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("Order quantity must be a positive whole number.")
        if not isfinite(price) or price <= 0:
            raise ValueError("Order price must be a positive finite number.")
        if not str(client_order_id).strip():
            raise ValueError("A client order ID is required for duplicate prevention.")
        held = self._held_side(symbol)
        if side == "SELL" and held == "LONG":
            if quantity > self._positions[symbol]["quantity"]:
                raise ValueError("SELL quantity exceeds the available long position.")
        if side == "SELL" and held == "SHORT":
            raise ValueError("SELL does not add to an open short. Buy covers it.")
        if side == "BUY" and held == "SHORT":
            if quantity > self._positions[symbol]["quantity"]:
                raise ValueError("BUY quantity exceeds the open short and does not open a long.")
        if side == "BUY" and held != "SHORT" and quantity * price > self._cash:
            raise ValueError("BUY order exceeds available paper buying power.")
        return {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": float(price),
            "client_order_id": str(client_order_id).strip(),
        }

    def _maximum_quantity(self, side, symbol, price, stop_loss, limits):
        if (
            isinstance(stop_loss, bool)
            or not isinstance(stop_loss, (int, float))
            or not isfinite(stop_loss)
            or stop_loss <= 0
        ):
            raise ValueError("stop_loss is required to enforce per-trade risk.")
        if side == "BUY" and not stop_loss < price:
            raise ValueError("BUY orders require stop_loss below the entry price.")
        if side == "SELL" and not stop_loss > price:
            raise ValueError("SELL orders require stop_loss above the entry price.")
        held = self._held_side(symbol)
        reducing = (side == "SELL" and held == "LONG") or (side == "BUY" and held == "SHORT")
        equity = self.account_info()["equity"]
        quantity = calculate_position_size(
            equity,
            price,
            stop_loss,
            limits,
            current_exposure=0.0 if reducing else self.current_exposure(),
        )
        if reducing:
            quantity = min(quantity, self._positions[symbol]["quantity"])
        return quantity

    def _resolve_effect(self, order):
        named = order.get("effect")
        if named in {"OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT", "CLOSE_SHORT"}:
            return named
        held = self._held_side(order["symbol"])
        if order["side"] == "BUY" and held == "SHORT":
            return "CLOSE_SHORT"
        if order["side"] == "BUY":
            return "OPEN_LONG"
        if order["side"] == "SELL" and held == "SHORT":
            raise ValueError("SELL does not add to an open short. Buy covers it.")
        if order["side"] == "SELL" and held == "LONG":
            return "CLOSE_LONG"
        if order["side"] == "SELL":
            return "OPEN_SHORT"
        raise ValueError("Paper orders support BUY or SELL only.")

    def _apply_position_change(self, order):
        total = order["quantity"] * order["price"]
        symbol = order["symbol"]
        effect = self._resolve_effect(order)
        order["effect"] = effect
        realized_delta = 0.0
        if effect == "OPEN_LONG":
            self._cash -= total
            current = self._positions.get(symbol)
            if current is None:
                self._positions[symbol] = {
                    "quantity": order["quantity"],
                    "average_price": order["price"],
                    "last_price": order["price"],
                    "side": "LONG",
                }
            else:
                if current.get("side", "LONG") != "LONG":
                    raise ValueError("BUY does not add to an open short.")
                new_quantity = current["quantity"] + order["quantity"]
                current["average_price"] = (
                    current["quantity"] * current["average_price"]
                    + total
                ) / new_quantity
                current["quantity"] = new_quantity
                current["last_price"] = order["price"]
                current["side"] = "LONG"
        elif effect == "OPEN_SHORT":
            if symbol in self._positions:
                raise ValueError("SELL does not open a short while a position is open.")
            self._cash += total
            self._positions[symbol] = {
                "quantity": order["quantity"],
                "average_price": order["price"],
                "last_price": order["price"],
                "side": "SHORT",
            }
        elif effect == "CLOSE_LONG":
            current = self._positions[symbol]
            if current.get("side", "LONG") != "LONG":
                raise ValueError("SELL closes a long only.")
            self._cash += total
            realized_delta = (order["price"] - current["average_price"]) * order["quantity"]
            self._realized_pnl += realized_delta
            current["quantity"] -= order["quantity"]
            if current["quantity"] == 0:
                del self._positions[symbol]
            else:
                current["last_price"] = order["price"]
        elif effect == "CLOSE_SHORT":
            current = self._positions[symbol]
            if current.get("side") != "SHORT":
                raise ValueError("BUY covers a short only.")
            self._cash -= total
            realized_delta = (current["average_price"] - order["price"]) * order["quantity"]
            self._realized_pnl += realized_delta
            current["quantity"] -= order["quantity"]
            if current["quantity"] == 0:
                del self._positions[symbol]
            else:
                current["last_price"] = order["price"]
        else:
            raise ValueError("Paper fill effect is not recognized.")
        return realized_delta

    def _position_plan_after_fill(self, order, stop_loss, profit_target):
        """Remember the latest buy's stop and target. A flat sell clears them."""
        symbol = order["symbol"]
        effect = order.get("effect")
        if effect in {"OPEN_LONG", "OPEN_SHORT"}:
            opening_short = effect == "OPEN_SHORT"
            prices_ok = (
                not isinstance(stop_loss, bool)
                and not isinstance(profit_target, bool)
                and isinstance(stop_loss, (int, float))
                and isinstance(profit_target, (int, float))
                and isfinite(stop_loss)
                and isfinite(profit_target)
            )
            if opening_short:
                prices_ok = prices_ok and profit_target < order["price"] < stop_loss
            else:
                prices_ok = prices_ok and stop_loss < order["price"] < profit_target
            if not prices_ok:
                return None, None
            position = self._positions[symbol]
            position["stop_loss"] = float(stop_loss)
            position["profit_target"] = float(profit_target)
            return {
                "symbol": symbol,
                "stop_loss": position["stop_loss"],
                "profit_target": position["profit_target"],
                "quantity": position["quantity"],
                "side": "SHORT" if opening_short else "LONG",
            }, None
        if symbol not in self._positions:
            return None, symbol
        position = self._positions[symbol]
        if "stop_loss" not in position or "profit_target" not in position:
            return None, None
        return {
            "symbol": symbol,
            "stop_loss": position["stop_loss"],
            "profit_target": position["profit_target"],
            "quantity": position["quantity"],
            "side": position.get("side", "LONG"),
        }, None

    def submit_order(
        self,
        symbol,
        side,
        quantity,
        price,
        client_order_id,
        approved=False,
        stop_loss=None,
        profit_target=None,
        limits=None,
    ):
        """Fill a paper order. Approval does not waive risk limits."""
        if not approved:
            raise PermissionError("Order requires explicit human approval.")

        limits = limits or RiskLimits()
        order_id = str(client_order_id).strip()
        if order_id in self._orders:
            existing = self._orders[order_id]
            if (
                existing["symbol"] != str(symbol).strip().upper()
                or existing["side"] != str(side).strip().upper()
                or existing["quantity"] != quantity
                or existing["price"] != price
            ):
                raise ValueError("Client order ID was already used for a different order.")
            return dict(existing)

        order = self.validate_order(
            symbol,
            side,
            quantity,
            price,
            order_id,
        )
        if not validate_daily_loss(
            self.account_info()["equity"],
            self.daily_pnl(),
            limits,
        ):
            raise PermissionError("Daily loss limit reached; trading is halted.")
        order["effect"] = self._resolve_effect(order)
        max_quantity = self._maximum_quantity(
            order["side"],
            order["symbol"],
            order["price"],
            stop_loss,
            limits,
        )
        if order["quantity"] > max_quantity:
            raise ValueError("Requested quantity exceeds the configured risk limit.")

        previous_state = (
            self._cash,
            deepcopy(self._positions),
            self._realized_pnl,
        )
        realized_delta = self._apply_position_change(order)
        position_plan, clear_symbol = self._position_plan_after_fill(
            order,
            stop_loss,
            profit_target,
        )
        filled = {
            **order,
            "status": "FILLED",
            "order_id": order_id,
            "realized_pnl": realized_delta,
        }
        if self._database is not None:
            plan_kwargs = {}
            if position_plan is not None:
                plan_kwargs["position_plan"] = position_plan
            if clear_symbol is not None:
                plan_kwargs["clear_position_symbol"] = clear_symbol
            try:
                self._database.record_paper_fill(
                    filled,
                    realized_delta,
                    self.account_info(),
                    **plan_kwargs,
                )
            except Exception:
                self._cash, self._positions, self._realized_pnl = previous_state
                raise
        self._orders[order_id] = filled
        self._history.append(filled)
        return dict(filled)


def get_broker_status():
    return "PAPER BROKER MODULE ONLINE; LIVE TRADING DISABLED"
