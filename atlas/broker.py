from copy import deepcopy
from math import isfinite


class PaperBroker:
    """In-memory, long-only paper broker. It has no live-broker connection."""

    def __init__(self, initial_cash=100_000.0, database=None):
        if not isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("Initial paper cash must be a positive finite amount.")
        self._cash = float(initial_cash)
        self._positions = {}
        self._orders = {}
        self._history = []
        self._realized_pnl = 0.0
        self._database = database
        if self._database is not None:
            self._database.record_initial_account(self.account_info())

    def account_info(self):
        position_value = sum(
            position["quantity"] * position["last_price"]
            for position in self._positions.values()
        )
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
        if side == "SELL":
            position = self._positions.get(symbol)
            if position is None or quantity > position["quantity"]:
                raise ValueError("SELL quantity exceeds the available long position.")
        if side == "BUY" and quantity * price > self._cash:
            raise ValueError("BUY order exceeds available paper buying power.")
        return {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": float(price),
            "client_order_id": str(client_order_id).strip(),
        }

    def submit_order(
        self,
        symbol,
        side,
        quantity,
        price,
        client_order_id,
        approved=False,
    ):
        if not approved:
            raise PermissionError("Order requires explicit human approval.")

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
        previous_state = (
            self._cash,
            deepcopy(self._positions),
            self._realized_pnl,
        )
        total = order["quantity"] * order["price"]
        symbol = order["symbol"]
        realized_delta = 0.0
        if order["side"] == "BUY":
            self._cash -= total
            current = self._positions.get(symbol)
            if current is None:
                self._positions[symbol] = {
                    "quantity": order["quantity"],
                    "average_price": order["price"],
                    "last_price": order["price"],
                }
            else:
                new_quantity = current["quantity"] + order["quantity"]
                current["average_price"] = (
                    current["quantity"] * current["average_price"]
                    + total
                ) / new_quantity
                current["quantity"] = new_quantity
                current["last_price"] = order["price"]
        else:
            current = self._positions[symbol]
            self._cash += total
            realized_delta = (
                order["price"] - current["average_price"]
            ) * order["quantity"]
            self._realized_pnl += realized_delta
            current["quantity"] -= order["quantity"]
            if current["quantity"] == 0:
                del self._positions[symbol]
            else:
                current["last_price"] = order["price"]

        filled = {
            **order,
            "status": "FILLED",
            "order_id": order_id,
            "realized_pnl": realized_delta,
        }
        if self._database is not None:
            try:
                self._database.record_paper_fill(
                    filled,
                    realized_delta,
                    self.account_info(),
                )
            except Exception:
                self._cash, self._positions, self._realized_pnl = previous_state
                raise
        self._orders[order_id] = filled
        self._history.append(filled)
        return dict(filled)


def get_broker_status():
    return "PAPER BROKER MODULE ONLINE; LIVE TRADING DISABLED"
