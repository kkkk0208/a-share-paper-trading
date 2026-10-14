"""Storage for the quote collector, inside the paper-trading SQLite file.

Accepted ticks go to the existing market_snapshots table, so T+1 availability,
valuations and the post-approval tick check see them without extra wiring.
Everything else (rejected snapshots, source errors, cross-checks) goes to
quote_events. Same-day price limits go to daily_limits.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import fields as dataclass_fields
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from .ledger import PaperLedger
from .models import MarketQuote, money


QUOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS quote_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    symbol TEXT,
    source TEXT,
    quote_timestamp TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quote_events_created ON quote_events(created_at);
CREATE INDEX IF NOT EXISTS idx_quote_events_symbol ON quote_events(symbol, created_at);

CREATE TABLE IF NOT EXISTS daily_limits (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    limit_up TEXT NOT NULL,
    limit_down TEXT NOT NULL,
    source TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS collector_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at TEXT NOT NULL,
    status_json TEXT NOT NULL
);
"""

MARKET_QUOTE_FIELDS = frozenset(item.name for item in dataclass_fields(MarketQuote))


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    return con


def ensure_quote_schema(path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(target)) as con:
        con.executescript(QUOTE_SCHEMA)


def tick_snapshot_id(quote: MarketQuote, source: str) -> str:
    """Same content gives the same id, so a repeated snapshot is stored only once."""
    key = "|".join(str(part) for part in (
        source, quote.symbol, quote.timestamp.isoformat(), quote.last, quote.bid, quote.ask,
        quote.bid_size, quote.ask_size,
    ))
    return "tick-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


def quote_from_payload(payload: dict[str, Any]) -> MarketQuote:
    return MarketQuote(**{key: value for key, value in payload.items() if key in MARKET_QUOTE_FIELDS})


def _day_range(day: date) -> tuple[str, str]:
    return f"{day.isoformat()}T00:00:00", f"{(day + timedelta(days=1)).isoformat()}T00:00:00"


def latest_stored_quote(db_path: str | Path, symbol: str) -> MarketQuote | None:
    path = Path(db_path)
    if not path.exists():
        return None
    with closing(_connect(path)) as con:
        try:
            row = con.execute(
                "SELECT payload_json FROM market_snapshots WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return None if row is None else quote_from_payload(json.loads(row["payload_json"]))


class SqliteLimitCache:
    """Price limits are fixed for a trading day. The first value seen for a day wins."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.cache_key = str(self.path.resolve())
        ensure_quote_schema(self.path)

    def get(self, symbol: str, trade_date: date) -> tuple[Decimal, Decimal] | None:
        with closing(_connect(self.path)) as con:
            row = con.execute(
                "SELECT limit_up,limit_down FROM daily_limits WHERE symbol=? AND trade_date=?",
                (symbol, trade_date.isoformat()),
            ).fetchone()
        return None if row is None else (money(row["limit_up"]), money(row["limit_down"]))

    def put(
        self, symbol: str, trade_date: date, limit_up: Decimal, limit_down: Decimal,
        source: str, captured_at: datetime | None = None,
    ) -> bool:
        when = (captured_at or datetime.now().astimezone()).isoformat()
        with closing(_connect(self.path)) as con, con:
            cursor = con.execute(
                "INSERT OR IGNORE INTO daily_limits(symbol,trade_date,limit_up,limit_down,source,captured_at) "
                "VALUES(?,?,?,?,?,?)",
                (symbol, trade_date.isoformat(), str(money(limit_up)), str(money(limit_down)), source, when),
            )
            return cursor.rowcount == 1


class QuoteStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.ledger = PaperLedger(self.path)
        ensure_quote_schema(self.path)
        self.limits = SqliteLimitCache(self.path)

    def save_tick(self, quote: MarketQuote, extra: dict[str, Any], now: datetime) -> str:
        return self.ledger.save_quote(quote, now, extra=extra)

    def record_event(
        self, event_type: str, severity: str, *, now: datetime, symbol: str | None = None,
        source: str | None = None, quote_timestamp: datetime | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.ledger.transaction() as con:
            con.execute(
                "INSERT INTO quote_events(event_type,severity,symbol,source,quote_timestamp,detail_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (event_type, severity, symbol, source,
                 quote_timestamp.isoformat() if quote_timestamp else None,
                 json.dumps(detail or {}, ensure_ascii=False, sort_keys=True, default=str), now.isoformat()),
            )

    def latest_ticks(self, symbols: Sequence[str]) -> dict[str, MarketQuote]:
        result: dict[str, MarketQuote] = {}
        for symbol in symbols:
            quote = latest_stored_quote(self.path, symbol)
            if quote is not None:
                result[symbol] = quote
        return result

    def update_status(self, now: datetime, status: dict[str, Any]) -> None:
        with self.ledger.transaction() as con:
            con.execute(
                "INSERT INTO collector_status(id,updated_at,status_json) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,status_json=excluded.status_json",
                (now.isoformat(), json.dumps(status, ensure_ascii=False, sort_keys=True, default=str)),
            )

    def status(self) -> dict[str, Any] | None:
        with self.ledger.connection() as con:
            row = con.execute("SELECT updated_at,status_json FROM collector_status WHERE id=1").fetchone()
        return None if row is None else {"updated_at": row["updated_at"], **json.loads(row["status_json"])}

    def ticks_for_day(self, symbol: str, day: date) -> list[dict[str, Any]]:
        start, end = _day_range(day)
        with self.ledger.connection() as con:
            rows = con.execute(
                "SELECT timestamp,created_at,payload_json FROM market_snapshots "
                "WHERE symbol=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
                (symbol, start, end),
            ).fetchall()
        return [
            {"timestamp": row["timestamp"], "created_at": row["created_at"], "payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    def events_for_day(self, day: date) -> list[dict[str, Any]]:
        with self.ledger.connection() as con:
            rows = con.execute(
                "SELECT * FROM quote_events WHERE substr(created_at,1,10)=? ORDER BY id", (day.isoformat(),),
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]


def collector_status(db_path: str | Path) -> dict[str, Any] | None:
    """Read-only helper for the dashboard. Returns None when the collector never ran."""
    path = Path(db_path)
    if not path.exists():
        return None
    with closing(_connect(path)) as con:
        try:
            row = con.execute("SELECT updated_at,status_json FROM collector_status WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            return None
    return None if row is None else {"updated_at": row["updated_at"], **json.loads(row["status_json"])}
