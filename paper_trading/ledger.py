from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .config import PaperConfig
from .models import FillResult, MarketQuote, OrderSide, OrderStatus, RiskDecision, SignalIntent, money


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    initial_cash TEXT NOT NULL,
    cash TEXT NOT NULL,
    frozen_cash TEXT NOT NULL DEFAULT '0.00',
    realized_pnl TEXT NOT NULL DEFAULT '0.00',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    available_quantity INTEGER NOT NULL,
    t0_quantity INTEGER NOT NULL,
    t0_date TEXT,
    average_cost TEXT NOT NULL,
    realized_pnl TEXT NOT NULL DEFAULT '0.00',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS position_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    cost_price REAL NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES intents(intent_id)
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES intents(intent_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    approved INTEGER NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(intent_id, approved, created_at),
    FOREIGN KEY (intent_id) REFERENCES intents(intent_id)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    intent_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    limit_price TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES intents(intent_id)
);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price TEXT NOT NULL,
    commission TEXT NOT NULL,
    stamp_tax TEXT NOT NULL,
    transfer_fee TEXT NOT NULL,
    quote_snapshot_id TEXT NOT NULL,
    quote_timestamp TEXT,
    order_book_json TEXT,
    realized_pnl TEXT NOT NULL DEFAULT '0.00',
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS equity_curve (
    account_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    equity TEXT NOT NULL,
    benchmark_close TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, trade_date),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    cash_dividend TEXT NOT NULL DEFAULT '0',
    stock_dividend TEXT NOT NULL DEFAULT '0',
    split_ratio TEXT NOT NULL DEFAULT '1',
    applied_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, ex_date)
);

CREATE TABLE IF NOT EXISTS cash_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    balance_after TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS position_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    quantity_delta INTEGER NOT NULL,
    quantity_after INTEGER NOT NULL,
    available_after INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    last TEXT NOT NULL,
    bid TEXT NOT NULL,
    ask TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_intent ON risk_decisions(intent_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_intent_events ON intent_events(intent_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_approval_intent ON approvals(intent_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol ON market_snapshots(symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_lots_symbol ON position_lots(symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_lots_account_symbol ON position_lots(account_id, symbol, trade_date, id);
"""


def utc_now() -> datetime:
    return datetime.now().astimezone()


class PaperLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        return con

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _create_schema(self) -> None:
        with self.connection() as con:
            con.executescript(SCHEMA)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(fills)").fetchall()}
            if "quote_timestamp" not in columns:
                con.execute("ALTER TABLE fills ADD COLUMN quote_timestamp TEXT")
            if "order_book_json" not in columns:
                con.execute("ALTER TABLE fills ADD COLUMN order_book_json TEXT")
            if "realized_pnl" not in columns:
                con.execute("ALTER TABLE fills ADD COLUMN realized_pnl TEXT NOT NULL DEFAULT '0.00'")
        self.migrate_positions_to_lots()

    def migrate_positions_to_lots(self, now: datetime | None = None) -> int:
        """One-time migration: every legacy position becomes one historical lot."""
        when = now or utc_now()
        migration = "positions_to_lots_v1"
        with self.transaction() as con:
            if con.execute("SELECT 1 FROM schema_migrations WHERE name=?", (migration,)).fetchone():
                return 0
            rows = con.execute("SELECT * FROM positions WHERE quantity>0 ORDER BY account_id,symbol").fetchall()
            migrated = 0
            for row in rows:
                exists = con.execute(
                    "SELECT 1 FROM position_lots WHERE account_id=? AND symbol=? LIMIT 1",
                    (row["account_id"], row["symbol"]),
                ).fetchone()
                if exists:
                    continue
                con.execute(
                    "INSERT INTO position_lots(account_id,symbol,trade_date,quantity,cost_price,created_at) VALUES(?,?,?,?,?,?)",
                    (row["account_id"], row["symbol"], "1970-01-01", int(row["quantity"]),
                     float(row["average_cost"]), when.isoformat()),
                )
                migrated += 1
            con.execute(
                "INSERT INTO schema_migrations(name,applied_at) VALUES(?,?)",
                (migration, when.isoformat()),
            )
        return migrated

    def initialize_account(self, config: PaperConfig, now: datetime | None = None) -> None:
        timestamp = (now or utc_now()).isoformat()
        with self.transaction() as con:
            existing = con.execute("SELECT * FROM accounts WHERE account_id=?", (config.account_id,)).fetchone()
            if existing:
                if money(existing["initial_cash"]) != config.initial_cash:
                    raise ValueError("existing account initial_cash does not match config")
                return
            con.execute(
                "INSERT INTO accounts(account_id,currency,initial_cash,cash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (config.account_id, config.currency, str(config.initial_cash), str(config.initial_cash), timestamp, timestamp),
            )
            con.execute(
                "INSERT INTO cash_ledger(account_id,event_type,reference_id,amount,balance_after,created_at) VALUES(?,?,?,?,?,?)",
                (config.account_id, "INITIAL_CAPITAL", config.account_id, str(config.initial_cash), str(config.initial_cash), timestamp),
            )

    def account(self, account_id: str) -> dict[str, Any]:
        with self.connection() as con:
            row = con.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if not row:
            raise KeyError(f"account not initialized: {account_id}")
        result = dict(row)
        for name in ("initial_cash", "cash", "frozen_cash", "realized_pnl"):
            result[name] = money(result[name])
        return result

    def latest_trade_date(self, symbol: str) -> date | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT timestamp FROM market_snapshots WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return None if not row else datetime.fromisoformat(row["timestamp"]).date()

    def position(
        self, account_id: str, symbol: str, current_trade_date: date | None = None,
    ) -> dict[str, Any]:
        trade_date = current_trade_date or self.latest_trade_date(symbol)
        with self.connection() as con:
            rows = con.execute(
                "SELECT trade_date,quantity,cost_price FROM position_lots "
                "WHERE account_id=? AND symbol=? AND quantity>0 ORDER BY trade_date,id",
                (account_id, symbol),
            ).fetchall()
        quantity = sum(int(row["quantity"]) for row in rows)
        available = (
            sum(int(row["quantity"]) for row in rows if date.fromisoformat(row["trade_date"]) < trade_date)
            if trade_date else 0
        )
        total_cost = sum(Decimal(str(row["cost_price"])) * int(row["quantity"]) for row in rows)
        average_cost = money(total_cost / quantity) if quantity else money(0)
        return {
            "account_id": account_id,
            "symbol": symbol,
            "quantity": quantity,
            "available_quantity": available,
            "t0_quantity": quantity - available,
            "average_cost": average_cost,
            "realized_pnl": money(0),
            "t0_date": None,
        }

    def positions(self, account_id: str) -> list[dict[str, Any]]:
        with self.connection() as con:
            rows = con.execute(
                "SELECT DISTINCT symbol FROM position_lots WHERE account_id=? AND quantity>0 ORDER BY symbol",
                (account_id,),
            ).fetchall()
        return [self.position(account_id, row["symbol"]) for row in rows]

    def add_intent(self, intent: SignalIntent, now: datetime | None = None) -> str:
        timestamp = (now or utc_now()).isoformat()
        payload = json.dumps(intent.to_dict(), ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            existing = con.execute("SELECT payload_json,status FROM intents WHERE intent_id=?", (intent.intent_id,)).fetchone()
            if existing:
                if existing["payload_json"] != payload:
                    raise ValueError("intent_id already exists with different payload")
                return existing["status"]
            con.execute(
                "INSERT INTO intents(intent_id,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                (intent.intent_id, payload, OrderStatus.PROPOSED.value, intent.created_at.isoformat(), timestamp),
            )
            con.execute(
                "INSERT INTO intent_events(intent_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (intent.intent_id, "CREATED", "{}", timestamp),
            )
        return OrderStatus.PROPOSED.value

    def get_intent(self, intent_id: str) -> SignalIntent:
        with self.connection() as con:
            row = con.execute("SELECT payload_json FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        if not row:
            raise KeyError(intent_id)
        return SignalIntent.from_dict(json.loads(row["payload_json"]))

    def intent_status(self, intent_id: str) -> str:
        with self.connection() as con:
            row = con.execute("SELECT status FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        if not row:
            raise KeyError(intent_id)
        return row["status"]

    def _set_intent_status(self, con: sqlite3.Connection, intent_id: str, status: str, now: datetime) -> None:
        con.execute("UPDATE intents SET status=?,updated_at=? WHERE intent_id=?", (status, now.isoformat(), intent_id))

    def expire_intent_if_due(self, intent_id: str, config: PaperConfig, now: datetime) -> bool:
        intent = self.get_intent(intent_id)
        deadline = min(intent.valid_until, intent.created_at + timedelta(minutes=config.intent_ttl_minutes))
        terminal = {
            OrderStatus.FILLED.value, OrderStatus.RISK_REJECTED.value, OrderStatus.USER_REJECTED.value,
            OrderStatus.REJECTED.value, OrderStatus.CANCELED.value, OrderStatus.EXPIRED.value,
        }
        current = self.intent_status(intent_id)
        if current in terminal or now <= deadline:
            return current == OrderStatus.EXPIRED.value
        with self.transaction() as con:
            self._set_intent_status(con, intent_id, OrderStatus.EXPIRED.value, now)
            con.execute(
                "INSERT INTO intent_events(intent_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (intent_id, OrderStatus.EXPIRED.value,
                 json.dumps({"deadline": deadline.isoformat()}, sort_keys=True), now.isoformat()),
            )
            con.execute(
                "UPDATE orders SET status=?,updated_at=? WHERE intent_id=? AND status NOT IN (?,?)",
                (OrderStatus.CANCELED.value, now.isoformat(), intent_id,
                 OrderStatus.FILLED.value, OrderStatus.CANCELED.value),
            )
        return True

    def cancel_intent(self, intent_id: str, reason: str, now: datetime, actor: str = "user") -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("cancel reason is required")
        current = self.intent_status(intent_id)
        if current == OrderStatus.FILLED.value:
            raise ValueError("filled intent cannot be canceled")
        with self.transaction() as con:
            self._set_intent_status(con, intent_id, OrderStatus.CANCELED.value, now)
            con.execute(
                "UPDATE orders SET status=?,updated_at=? WHERE intent_id=? AND status<>?",
                (OrderStatus.CANCELED.value, now.isoformat(), intent_id, OrderStatus.FILLED.value),
            )
            con.execute(
                "INSERT INTO intent_events(intent_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (intent_id, OrderStatus.CANCELED.value,
                 json.dumps({"actor": actor, "reason": reason.strip()}, ensure_ascii=False, sort_keys=True),
                 now.isoformat()),
            )
        return {"intent_id": intent_id, "status": OrderStatus.CANCELED.value, "reason": reason.strip()}

    def list_intents(self, status: str | None = None) -> list[dict[str, Any]]:
        groups = {
            "pending": [OrderStatus.PROPOSED.value, OrderStatus.APPROVAL_PENDING.value,
                        OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value],
            "approved": [OrderStatus.APPROVED.value],
            "executed": [OrderStatus.FILLED.value],
            "rejected": [OrderStatus.RISK_REJECTED.value, OrderStatus.USER_REJECTED.value,
                         OrderStatus.REJECTED.value, OrderStatus.CANCELED.value],
            "expired": [OrderStatus.EXPIRED.value],
        }
        with self.connection() as con:
            if status:
                values = groups[status]
                placeholders = ",".join("?" for _ in values)
                rows = con.execute(
                    f"SELECT * FROM intents WHERE status IN ({placeholders}) ORDER BY updated_at DESC", values
                ).fetchall()
            else:
                rows = con.execute("SELECT * FROM intents ORDER BY updated_at DESC").fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def save_risk_decision(self, decision: RiskDecision) -> None:
        checks = [check.__dict__ for check in decision.checks]
        status = OrderStatus.RISK_REJECTED.value if decision.decision == "REJECTED" else OrderStatus.APPROVAL_PENDING.value
        with self.transaction() as con:
            con.execute(
                "INSERT INTO risk_decisions(intent_id,decision,ruleset_version,checked_at,checks_json) VALUES(?,?,?,?,?)",
                (decision.intent_id, decision.decision, decision.ruleset_version, decision.checked_at.isoformat(),
                 json.dumps(checks, ensure_ascii=False, sort_keys=True)),
            )
            self._set_intent_status(con, decision.intent_id, status, decision.checked_at)

    def latest_risk(self, intent_id: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM risk_decisions WHERE intent_id=? ORDER BY id DESC LIMIT 1", (intent_id,)
            ).fetchone()
        return None if not row else dict(row)

    def record_approval(
        self, intent_id: str, approved: bool, actor: str, note: str,
        config: PaperConfig, now: datetime | None = None,
    ) -> None:
        when = now or utc_now()
        intent = self.get_intent(intent_id)
        latest_risk = self.latest_risk(intent_id)
        if not latest_risk or latest_risk["decision"] == "REJECTED":
            raise ValueError("intent has no passing risk decision")
        expires = min(intent.valid_until, when + timedelta(seconds=config.approval_ttl_seconds))
        status = OrderStatus.APPROVED.value if approved else OrderStatus.USER_REJECTED.value
        with self.transaction() as con:
            con.execute(
                "INSERT INTO approvals(intent_id,approved,actor,note,config_fingerprint,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (intent_id, int(approved), actor, note, config.fingerprint, when.isoformat(), expires.isoformat()),
            )
            self._set_intent_status(con, intent_id, status, when)

    def valid_approval(self, intent_id: str, config: PaperConfig, now: datetime) -> bool:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM approvals WHERE intent_id=? ORDER BY id DESC LIMIT 1", (intent_id,)
            ).fetchone()
        return bool(
            row and row["approved"] and row["config_fingerprint"] == config.fingerprint
            and datetime.fromisoformat(row["expires_at"]) >= now
        )

    def latest_approval(self, intent_id: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM approvals WHERE intent_id=? AND approved=1 ORDER BY id DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
        return None if not row else dict(row)

    def quote_count_after(self, symbol: str, after: datetime, through: datetime) -> int:
        with self.connection() as con:
            row = con.execute(
                "SELECT COUNT(DISTINCT snapshot_id) count FROM market_snapshots "
                "WHERE symbol=? AND timestamp>? AND timestamp<=?",
                (symbol, after.isoformat(), through.isoformat()),
            ).fetchone()
        return int(row["count"])

    def save_quote(self, quote: MarketQuote, now: datetime | None = None) -> str:
        snapshot_id = quote.snapshot_id or str(uuid.uuid4())
        payload = quote.to_dict() | {"snapshot_id": snapshot_id}
        with self.transaction() as con:
            con.execute(
                "INSERT OR IGNORE INTO market_snapshots(snapshot_id,symbol,timestamp,last,bid,ask,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (snapshot_id, quote.symbol, quote.timestamp.isoformat(), str(quote.last), str(quote.bid), str(quote.ask),
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), (now or utc_now()).isoformat()),
            )
        return snapshot_id

    def latest_prices(self) -> dict[str, Decimal]:
        query = """
        SELECT m.symbol,m.last FROM market_snapshots m
        JOIN (SELECT symbol,MAX(timestamp) ts FROM market_snapshots GROUP BY symbol) x
          ON x.symbol=m.symbol AND x.ts=m.timestamp
        """
        with self.connection() as con:
            rows = con.execute(query).fetchall()
        return {row["symbol"]: money(row["last"]) for row in rows}

    def total_exposure(self, account_id: str, prices: dict[str, Decimal] | None = None) -> Decimal:
        current = prices or self.latest_prices()
        total = money(0)
        for position in self.positions(account_id):
            price = current.get(position["symbol"], position["average_cost"])
            total += money(price * position["quantity"])
        return money(total)

    def create_order(self, intent: SignalIntent, now: datetime) -> dict[str, Any]:
        client_order_id = f"paper-{intent.intent_id}"
        order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, client_order_id))
        with self.transaction() as con:
            existing = con.execute("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()
            if existing:
                return dict(existing)
            con.execute(
                "INSERT INTO orders(order_id,client_order_id,intent_id,status,symbol,side,quantity,limit_price,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (order_id, client_order_id, intent.intent_id, OrderStatus.SUBMITTED.value, intent.symbol,
                 intent.side.value, intent.quantity, str(intent.limit_price), now.isoformat(), now.isoformat()),
            )
            con.execute(
                "INSERT INTO order_events(order_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (order_id, OrderStatus.SUBMITTED.value, "{}", now.isoformat()),
            )
            self._set_intent_status(con, intent.intent_id, OrderStatus.SUBMITTED.value, now)
        return self.order(order_id)

    def order(self, order_id: str) -> dict[str, Any]:
        with self.connection() as con:
            row = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            raise KeyError(order_id)
        result = dict(row)
        result["limit_price"] = money(result["limit_price"])
        return result

    def order_for_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute("SELECT order_id FROM orders WHERE intent_id=?", (intent_id,)).fetchone()
        return None if not row else self.order(row["order_id"])

    def apply_fill(
        self, config: PaperConfig, order_id: str, fill: FillResult, now: datetime,
        fill_id: str | None = None,
    ) -> dict[str, Any]:
        unique_fill_id = fill_id or str(uuid.uuid4())
        with self.transaction() as con:
            if con.execute("SELECT 1 FROM fills WHERE fill_id=?", (unique_fill_id,)).fetchone():
                return dict(con.execute("SELECT * FROM fills WHERE fill_id=?", (unique_fill_id,)).fetchone())
            order = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not order:
                raise KeyError(order_id)
            remaining = order["quantity"] - order["filled_quantity"]
            if fill.quantity <= 0 or fill.quantity > remaining:
                raise ValueError("fill quantity exceeds remaining order quantity")
            account = con.execute("SELECT * FROM accounts WHERE account_id=?", (config.account_id,)).fetchone()
            if not account:
                raise KeyError(config.account_id)
            quote_row = con.execute(
                "SELECT timestamp FROM market_snapshots WHERE snapshot_id=?", (fill.quote_snapshot_id,)
            ).fetchone()
            if not quote_row:
                raise ValueError("fill quote snapshot is missing")
            current_trade_date = datetime.fromisoformat(quote_row["timestamp"]).date()
            lots = con.execute(
                "SELECT * FROM position_lots WHERE account_id=? AND symbol=? AND quantity>0 ORDER BY trade_date,id",
                (config.account_id, order["symbol"]),
            ).fetchall()
            old_qty = sum(int(row["quantity"]) for row in lots)
            old_available = sum(
                int(row["quantity"]) for row in lots
                if date.fromisoformat(row["trade_date"]) < current_trade_date
            )
            old_cost_total = sum(Decimal(str(row["cost_price"])) * int(row["quantity"]) for row in lots)
            old_cost = money(old_cost_total / old_qty) if old_qty else money(0)
            cash = money(account["cash"])
            notional = money(fill.price * fill.quantity)
            fees = fill.fees

            if OrderSide(order["side"]) == OrderSide.BUY:
                cash_delta = -money(notional + fees)
                if cash + cash_delta < 0:
                    raise ValueError("insufficient cash at fill time")
                new_qty = old_qty + fill.quantity
                new_available = old_available
                total_cost = old_cost * old_qty + notional + fees
                new_cost = money(total_cost / new_qty)
                realized_delta = money(0)
                lot_cost = (notional + fees) / fill.quantity
                con.execute(
                    "INSERT INTO position_lots(account_id,symbol,trade_date,quantity,cost_price,created_at) VALUES(?,?,?,?,?,?)",
                    (config.account_id, order["symbol"], current_trade_date.isoformat(), fill.quantity,
                     str(lot_cost), now.isoformat()),
                )
            else:
                if fill.quantity > old_available:
                    raise ValueError("insufficient T+1 available position at fill time")
                cash_delta = money(notional - fees)
                new_qty = old_qty - fill.quantity
                new_available = old_available - fill.quantity
                remaining_to_sell = fill.quantity
                realized_before_fees = Decimal("0")
                for lot in lots:
                    if remaining_to_sell <= 0:
                        break
                    if date.fromisoformat(lot["trade_date"]) >= current_trade_date:
                        continue
                    take = min(remaining_to_sell, int(lot["quantity"]))
                    realized_before_fees += (fill.price - Decimal(str(lot["cost_price"]))) * take
                    left = int(lot["quantity"]) - take
                    if left:
                        con.execute("UPDATE position_lots SET quantity=? WHERE id=?", (left, lot["id"]))
                    else:
                        con.execute("DELETE FROM position_lots WHERE id=?", (lot["id"],))
                    remaining_to_sell -= take
                if remaining_to_sell:
                    raise ValueError("FIFO lot deduction could not satisfy sell quantity")
                realized_delta = money(realized_before_fees - fees)
                remaining_lots = con.execute(
                    "SELECT quantity,cost_price FROM position_lots WHERE account_id=? AND symbol=? AND quantity>0",
                    (config.account_id, order["symbol"]),
                ).fetchall()
                remaining_cost = sum(
                    Decimal(str(row["cost_price"])) * int(row["quantity"]) for row in remaining_lots
                )
                new_cost = money(remaining_cost / new_qty) if new_qty else money(0)

            new_cash = money(cash + cash_delta)
            new_filled = order["filled_quantity"] + fill.quantity
            new_status = OrderStatus.FILLED.value if new_filled == order["quantity"] else OrderStatus.PARTIALLY_FILLED.value
            con.execute(
                "INSERT INTO fills(fill_id,order_id,quantity,price,commission,stamp_tax,transfer_fee,quote_snapshot_id,quote_timestamp,order_book_json,realized_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (unique_fill_id, order_id, fill.quantity, str(fill.price), str(fill.commission), str(fill.stamp_tax),
                 str(fill.transfer_fee), fill.quote_snapshot_id, fill.quote_timestamp.isoformat(),
                 json.dumps(fill.order_book, ensure_ascii=False, sort_keys=True), str(realized_delta), now.isoformat()),
            )
            con.execute(
                "UPDATE accounts SET cash=?,realized_pnl=?,updated_at=? WHERE account_id=?",
                (str(new_cash), str(money(Decimal(account["realized_pnl"]) + realized_delta)), now.isoformat(), config.account_id),
            )
            con.execute(
                "UPDATE orders SET filled_quantity=?,status=?,updated_at=? WHERE order_id=?",
                (new_filled, new_status, now.isoformat(), order_id),
            )
            self._set_intent_status(con, order["intent_id"], new_status, now)
            con.execute(
                "INSERT INTO cash_ledger(account_id,event_type,reference_id,amount,balance_after,created_at) VALUES(?,?,?,?,?,?)",
                (config.account_id, "FILL", unique_fill_id, str(cash_delta), str(new_cash), now.isoformat()),
            )
            con.execute(
                "INSERT INTO position_ledger(account_id,symbol,reference_id,quantity_delta,quantity_after,available_after,created_at) VALUES(?,?,?,?,?,?,?)",
                (config.account_id, order["symbol"], unique_fill_id,
                 fill.quantity if order["side"] == OrderSide.BUY.value else -fill.quantity,
                 new_qty, new_available, now.isoformat()),
            )
            con.execute(
                "INSERT INTO order_events(order_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (order_id, new_status, json.dumps({"fill_id": unique_fill_id}, sort_keys=True), now.isoformat()),
            )
        return self.order(order_id)

    def settle_t1(self, account_id: str, trade_date: date, now: datetime | None = None) -> int:
        # Deprecated compatibility shim. Availability is derived from lot.trade_date
        # and the latest market quote date, so settlement no longer mutates state.
        return 0

    def status(self, account_id: str) -> dict[str, Any]:
        account = self.account(account_id)
        prices = self.latest_prices()
        positions = self.positions(account_id)
        market_value = money(0)
        unrealized = money(0)
        for row in positions:
            last = prices.get(row["symbol"], row["average_cost"])
            row["last"] = last
            row["market_value"] = money(last * row["quantity"])
            row["unrealized_pnl"] = money((last - row["average_cost"]) * row["quantity"])
            market_value += row["market_value"]
            unrealized += row["unrealized_pnl"]
        return {
            "account_id": account_id,
            "cash": account["cash"],
            "market_value": money(market_value),
            "equity": money(account["cash"] + market_value),
            "realized_pnl": account["realized_pnl"],
            "unrealized_pnl": money(unrealized),
            "positions": positions,
        }

    def record_equity_snapshot(
        self, account_id: str, trade_date: date, benchmark_close: Decimal,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        snapshot = self.status(account_id)
        when = now or utc_now()
        with self.transaction() as con:
            con.execute(
                "INSERT INTO equity_curve(account_id,trade_date,equity,benchmark_close,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(account_id,trade_date) DO UPDATE SET equity=excluded.equity,"
                "benchmark_close=excluded.benchmark_close,created_at=excluded.created_at",
                (account_id, trade_date.isoformat(), str(snapshot["equity"]), str(money(benchmark_close)), when.isoformat()),
            )
        return {"trade_date": trade_date, "equity": snapshot["equity"], "benchmark_close": money(benchmark_close)}

    def add_corporate_action(
        self, symbol: str, ex_date: date, cash_dividend: Decimal,
        stock_dividend: Decimal, split_ratio: Decimal, now: datetime | None = None,
    ) -> dict[str, Any]:
        cash = Decimal(str(cash_dividend))
        stock = Decimal(str(stock_dividend))
        split = Decimal(str(split_ratio))
        if cash < 0 or stock < 0 or split <= 0:
            raise ValueError("corporate action values must be non-negative and split_ratio positive")
        when = now or utc_now()
        with self.transaction() as con:
            con.execute(
                "INSERT INTO corporate_actions(symbol,ex_date,cash_dividend,stock_dividend,split_ratio,created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(symbol,ex_date) DO UPDATE SET "
                "cash_dividend=excluded.cash_dividend,stock_dividend=excluded.stock_dividend,"
                "split_ratio=excluded.split_ratio,created_at=excluded.created_at "
                "WHERE corporate_actions.applied_at IS NULL",
                (symbol, ex_date.isoformat(), str(cash), str(stock), str(split), when.isoformat()),
            )
        return {"symbol": symbol, "ex_date": ex_date, "cash_dividend": cash,
                "stock_dividend": stock, "split_ratio": split}

    def apply_corporate_actions(
        self, account_id: str, through_date: date, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        when = now or utc_now()
        applied: list[dict[str, Any]] = []
        with self.transaction() as con:
            actions = con.execute(
                "SELECT * FROM corporate_actions WHERE applied_at IS NULL AND ex_date<=? ORDER BY ex_date,id",
                (through_date.isoformat(),),
            ).fetchall()
            for action in actions:
                lots = con.execute(
                    "SELECT * FROM position_lots WHERE account_id=? AND symbol=? AND trade_date<? AND quantity>0 ORDER BY id",
                    (account_id, action["symbol"], action["ex_date"]),
                ).fetchall()
                cash_dividend = Decimal(action["cash_dividend"])
                stock_dividend = Decimal(action["stock_dividend"])
                split_ratio = Decimal(action["split_ratio"])
                factor = split_ratio * (Decimal("1") + stock_dividend)
                cash_delta = Decimal("0")
                old_total = 0
                new_total = 0
                for lot in lots:
                    old_quantity = int(lot["quantity"])
                    new_quantity = int((Decimal(old_quantity) * factor).to_integral_value(rounding="ROUND_FLOOR"))
                    new_cost = max(Decimal("0"), (Decimal(str(lot["cost_price"])) - cash_dividend) / factor)
                    con.execute(
                        "UPDATE position_lots SET quantity=?,cost_price=? WHERE id=?",
                        (new_quantity, str(new_cost), lot["id"]),
                    )
                    old_total += old_quantity
                    new_total += new_quantity
                    cash_delta += cash_dividend * old_quantity
                if cash_delta:
                    account = con.execute("SELECT cash FROM accounts WHERE account_id=?", (account_id,)).fetchone()
                    if not account:
                        raise KeyError(account_id)
                    new_cash = money(Decimal(account["cash"]) + cash_delta)
                    con.execute(
                        "UPDATE accounts SET cash=?,updated_at=? WHERE account_id=?",
                        (str(new_cash), when.isoformat(), account_id),
                    )
                    con.execute(
                        "INSERT INTO cash_ledger(account_id,event_type,reference_id,amount,balance_after,created_at) VALUES(?,?,?,?,?,?)",
                        (account_id, "CASH_DIVIDEND", f"CA-{action['id']}", str(money(cash_delta)),
                         str(new_cash), when.isoformat()),
                    )
                con.execute("UPDATE corporate_actions SET applied_at=? WHERE id=?", (when.isoformat(), action["id"]))
                applied.append({
                    "id": action["id"], "symbol": action["symbol"], "ex_date": action["ex_date"],
                    "lots_adjusted": len(lots), "quantity_before": old_total,
                    "quantity_after": new_total, "cash_dividend": money(cash_delta),
                })
        return applied
