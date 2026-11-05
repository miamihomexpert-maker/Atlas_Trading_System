from datetime import datetime, timezone
from math import isfinite
import json
from pathlib import Path
import secrets
import sqlite3
import threading


class AtlasDatabase:
    """Local SQLite persistence for paper fills, equity, and model versions."""

    def __init__(self, path="database/atlas.sqlite3"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self):
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_orders (
                    order_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    equity REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    equity REAL NOT NULL,
                    realized_pnl REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_versions (
                    version TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    model_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS atlas_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_approvals (
                    token TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    signal_json TEXT NOT NULL,
                    response_at TEXT,
                    order_id TEXT
                );
                """
            )
            approval_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(trade_approvals)"
                ).fetchall()
            }
            if "order_id" not in approval_columns:
                self._connection.execute(
                    "ALTER TABLE trade_approvals ADD COLUMN order_id TEXT"
                )

    def close(self):
        with self._lock:
            self._connection.close()

    def record_initial_account(self, account):
        with self._lock, self._connection:
            if self._connection.execute(
                "SELECT 1 FROM equity_snapshots LIMIT 1"
            ).fetchone():
                return
            self._connection.execute(
                """
                INSERT INTO equity_snapshots(timestamp, equity, realized_pnl)
                VALUES (?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    float(account["equity"]),
                    float(account["realized_pnl"]),
                ),
            )

    def record_account_snapshot(self, account):
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO equity_snapshots(timestamp, equity, realized_pnl)
                VALUES (?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    float(account["equity"]),
                    float(account["realized_pnl"]),
                ),
            )

    def record_paper_fill(self, order, realized_pnl, account):
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO paper_orders(
                    order_id, timestamp, symbol, side, quantity, price,
                    realized_pnl, equity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order["order_id"],
                    datetime.now(timezone.utc).isoformat(),
                    order["symbol"],
                    order["side"],
                    order["quantity"],
                    order["price"],
                    float(realized_pnl),
                    float(account["equity"]),
                ),
            )
            self._connection.execute(
                """
                INSERT INTO equity_snapshots(timestamp, equity, realized_pnl)
                VALUES (?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    float(account["equity"]),
                    float(account["realized_pnl"]),
                ),
            )

    def list_paper_orders(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM paper_orders ORDER BY timestamp, order_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def replay_paper_orders(self):
        """Return fills in insertion order so a restart can rebuild the book."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM paper_orders ORDER BY rowid"
            ).fetchall()
        return [dict(row) for row in rows]

    def has_paper_state(self):
        with self._lock:
            snapshot = self._connection.execute(
                "SELECT 1 FROM equity_snapshots LIMIT 1"
            ).fetchone()
            if snapshot:
                return True
            order = self._connection.execute(
                "SELECT 1 FROM paper_orders LIMIT 1"
            ).fetchone()
        return order is not None

    def first_equity_snapshot(self):
        with self._lock:
            row = self._connection.execute(
                """
                SELECT equity, realized_pnl
                FROM equity_snapshots
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def latest_equity_snapshot(self):
        with self._lock:
            row = self._connection.execute(
                """
                SELECT equity, realized_pnl
                FROM equity_snapshots
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def equity_before(self, timestamp):
        with self._lock:
            row = self._connection.execute(
                """
                SELECT equity FROM equity_snapshots
                WHERE timestamp < ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(timestamp),),
            ).fetchone()
        return float(row["equity"]) if row else None

    def earliest_equity_from(self, timestamp):
        with self._lock:
            row = self._connection.execute(
                """
                SELECT equity FROM equity_snapshots
                WHERE timestamp >= ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (str(timestamp),),
            ).fetchone()
        return float(row["equity"]) if row else None

    def performance_report(self):
        orders = self.list_paper_orders()
        closed_fills = [
            row for row in orders
            if row["side"] == "SELL"
        ]
        profits = [
            row["realized_pnl"]
            for row in closed_fills
            if row["realized_pnl"] > 0
        ]
        losses = [
            row["realized_pnl"]
            for row in closed_fills
            if row["realized_pnl"] < 0
        ]
        with self._lock:
            snapshots = self._connection.execute(
                "SELECT equity FROM equity_snapshots ORDER BY id"
            ).fetchall()

        peak = 0.0
        maximum_drawdown = 0.0
        initial_equity = float(snapshots[0]["equity"]) if snapshots else None
        latest_equity = float(snapshots[-1]["equity"]) if snapshots else None
        for row in snapshots:
            equity = float(row["equity"])
            peak = max(peak, equity)
            if peak > 0:
                maximum_drawdown = max(
                    maximum_drawdown,
                    (peak - equity) / peak,
                )
        return {
            "fill_count": len(orders),
            "closed_fill_count": len(closed_fills),
            "win_rate": (
                len(profits) / len(closed_fills)
                if closed_fills
                else 0.0
            ),
            "average_profit": (
                sum(profits) / len(profits)
                if profits
                else 0.0
            ),
            "average_loss": (
                sum(losses) / len(losses)
                if losses
                else 0.0
            ),
            "profit_factor": (
                sum(profits) / abs(sum(losses))
                if losses
                else None
            ),
            "net_realized_pnl": sum(row["realized_pnl"] for row in closed_fills),
            "initial_equity": initial_equity,
            "latest_equity": latest_equity,
            "net_return_pct": (
                (latest_equity / initial_equity - 1) * 100
                if initial_equity
                else None
            ),
            "maximum_drawdown": maximum_drawdown,
            "trading_costs": None,
            "costs_note": "PaperBroker does not model transaction costs.",
        }

    def save_model_candidate(self, version, metrics, model):
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO model_versions(
                    version, created_at, status, metrics_json, model_json
                ) VALUES (?, ?, 'candidate', ?, ?)
                """,
                (
                    version,
                    created_at,
                    json.dumps(metrics, sort_keys=True, allow_nan=False),
                    json.dumps(model, sort_keys=True, allow_nan=False),
                ),
            )
        return {
            "version": version,
            "created_at": created_at,
            "status": "candidate",
        }

    def get_model_version(self, version):
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM model_versions WHERE version = ?",
                (version,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        result["model"] = json.loads(result.pop("model_json"))
        return result

    def active_model(self):
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM atlas_metadata WHERE key = 'active_model'"
            ).fetchone()
        return self.get_model_version(row["value"]) if row else None

    def promote_model(self, version, minimum_samples=50, minimum_accuracy=0.52,
                      maximum_drawdown=0.20):
        if (
            isinstance(minimum_samples, bool)
            or not isinstance(minimum_samples, int)
            or minimum_samples <= 0
        ):
            raise ValueError("minimum_samples must be a positive integer.")
        if not isfinite(minimum_accuracy) or not 0 <= minimum_accuracy <= 1:
            raise ValueError("minimum_accuracy must be between 0 and 1.")
        if not isfinite(maximum_drawdown) or not 0 <= maximum_drawdown <= 1:
            raise ValueError("maximum_drawdown must be between 0 and 1.")
        candidate = self.get_model_version(version)
        if candidate is None or candidate["status"] != "candidate":
            raise ValueError("Only a stored candidate model can be evaluated.")
        metrics = candidate["metrics"]
        required = (
            metrics.get("sample_count", 0) >= minimum_samples
            and metrics.get("accuracy", 0.0) >= minimum_accuracy
            and metrics.get("maximum_drawdown", 1.0) <= maximum_drawdown
            and metrics.get("net_return_pct", float("-inf")) > 0
        )
        incumbent = self.active_model()
        if incumbent is not None:
            incumbent_metrics = incumbent["metrics"]
            candidate_comparison = metrics.get("candidate_comparison", {})
            incumbent_comparison = metrics.get("incumbent_comparison", {})
            same_window = (
                incumbent_comparison.get("version") == incumbent["version"]
                and candidate_comparison.get("sample_count", 0) >= minimum_samples
                and candidate_comparison.get("accuracy", 0.0) >= minimum_accuracy
                and candidate_comparison.get("maximum_drawdown", 1.0)
                <= maximum_drawdown
                and candidate_comparison.get("net_return_pct", float("-inf"))
                > incumbent_comparison.get("net_return_pct", float("inf"))
                and candidate_comparison.get("maximum_drawdown", 1.0)
                <= incumbent_comparison.get("maximum_drawdown", 0.0)
            )
            required = required and same_window
        if not required:
            return {
                "promoted": False,
                "reason": "Candidate failed predefined out-of-sample performance or risk gates.",
                "active_version": incumbent["version"] if incumbent else None,
            }

        with self._lock, self._connection:
            if incumbent is not None:
                self._connection.execute(
                    "UPDATE model_versions SET status = 'archived' WHERE version = ?",
                    (incumbent["version"],),
                )
            self._connection.execute(
                "UPDATE model_versions SET status = 'active' WHERE version = ?",
                (version,),
            )
            self._connection.execute(
                """
                INSERT INTO atlas_metadata(key, value)
                VALUES ('active_model', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (version,),
            )
        return {
            "promoted": True,
            "reason": "Candidate passed out-of-sample performance and risk gates.",
            "active_version": version,
        }

    def rollback_model(self, version):
        previous = self.get_model_version(version)
        if previous is None or previous["status"] not in {"archived", "active"}:
            raise ValueError("Rollback target must be an archived model version.")
        incumbent = self.active_model()
        if incumbent is not None and incumbent["version"] == version:
            return version
        with self._lock, self._connection:
            if incumbent is not None:
                self._connection.execute(
                    "UPDATE model_versions SET status = 'archived' WHERE version = ?",
                    (incumbent["version"],),
                )
            self._connection.execute(
                "UPDATE model_versions SET status = 'active' WHERE version = ?",
                (version,),
            )
            self._connection.execute(
                """
                INSERT INTO atlas_metadata(key, value)
                VALUES ('active_model', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (version,),
            )
        return version

    def create_trade_approval(self, signal, chat_id, expires_minutes=15):
        if not isinstance(signal, dict) or not signal:
            raise ValueError("A trade signal is required for approval.")
        if not str(chat_id).strip():
            raise ValueError("An authorized notification chat ID is required.")
        if (
            isinstance(expires_minutes, bool)
            or not isinstance(expires_minutes, int)
            or expires_minutes <= 0
        ):
            raise ValueError("Approval expiry must be a positive number of minutes.")
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        token = secrets.token_urlsafe(24)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO trade_approvals(
                    token, chat_id, created_at, expires_at, status, signal_json
                ) VALUES (?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    token,
                    str(chat_id).strip(),
                    now.isoformat(),
                    (now + timedelta(minutes=expires_minutes)).isoformat(),
                    json.dumps(signal, sort_keys=True, allow_nan=False),
                ),
            )
        return token

    def cancel_trade_approval(self, token):
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE trade_approvals SET status = 'CANCELLED', response_at = ?
                WHERE token = ? AND status = 'PENDING'
                """,
                (datetime.now(timezone.utc).isoformat(), token),
            )

    def get_trade_approval(self, token, chat_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM trade_approvals WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        if str(chat_id) != row["chat_id"]:
            return {"status": "UNAUTHORIZED"}
        return {
            "status": row["status"],
            "expires_at": row["expires_at"],
            "signal": json.loads(row["signal_json"]),
        }

    def process_trade_approval(self, token, chat_id, decision):
        decision = str(decision).strip().upper()
        if decision not in {"APPROVE", "REJECT"}:
            raise ValueError("Approval decision must be APPROVE or REJECT.")
        now = datetime.now(timezone.utc)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM trade_approvals WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return {"status": "NOT_FOUND"}
            if str(chat_id) != row["chat_id"]:
                return {"status": "UNAUTHORIZED"}
            if row["status"] != "PENDING":
                return {"status": row["status"]}
            if datetime.fromisoformat(row["expires_at"]) <= now:
                self._connection.execute(
                    """
                    UPDATE trade_approvals SET status = 'EXPIRED', response_at = ?
                    WHERE token = ?
                    """,
                    (now.isoformat(), token),
                )
                return {"status": "EXPIRED"}
            status = "APPROVED" if decision == "APPROVE" else "REJECTED"
            self._connection.execute(
                """
                UPDATE trade_approvals SET status = ?, response_at = ?
                WHERE token = ? AND status = 'PENDING'
                """,
                (status, now.isoformat(), token),
            )
            signal = json.loads(row["signal_json"])
        return {"status": status, "signal": signal}

    def claim_trade_approval(self, token, chat_id, order_id, signal):
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM trade_approvals WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return {"status": "NOT_FOUND"}
            if str(chat_id) != row["chat_id"]:
                return {"status": "UNAUTHORIZED"}
            approved_signal = json.loads(row["signal_json"])
            if approved_signal != signal:
                return {"status": "SIGNAL_MISMATCH"}
            if row["status"] == "APPROVED":
                if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
                    self._connection.execute(
                        """
                        UPDATE trade_approvals SET status = 'EXPIRED', response_at = ?
                        WHERE token = ? AND status = 'APPROVED'
                        """,
                        (datetime.now(timezone.utc).isoformat(), token),
                    )
                    return {"status": "EXPIRED"}
                updated = self._connection.execute(
                    """
                    UPDATE trade_approvals SET status = 'EXECUTING', order_id = ?
                    WHERE token = ? AND status = 'APPROVED'
                    """,
                    (str(order_id), token),
                ).rowcount
                if updated == 1:
                    return {"status": "EXECUTING", "signal": approved_signal}
                row = self._connection.execute(
                    "SELECT * FROM trade_approvals WHERE token = ?",
                    (token,),
                ).fetchone()
            if row["status"] == "EXECUTING" and row["order_id"] == str(order_id):
                return {"status": "EXECUTING", "signal": approved_signal}
            return {"status": row["status"]}

    def complete_trade_approval(self, token, order_id):
        with self._lock, self._connection:
            updated = self._connection.execute(
                """
                UPDATE trade_approvals SET status = 'EXECUTED',
                    response_at = ?
                WHERE token = ? AND status = 'EXECUTING' AND order_id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), token, str(order_id)),
            ).rowcount
            if updated != 1:
                raise ValueError("Approval execution state does not match the order.")

    def get_metadata(self, key, default=None):
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM atlas_metadata WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else default

    def set_metadata(self, key, value):
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO atlas_metadata(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(key), str(value)),
            )


def get_database_status():
    return "LOCAL SQLITE PERSISTENCE AVAILABLE; NOT INITIALIZED UNTIL REQUESTED"
