from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .ledger import PaperLedger


@dataclass(frozen=True)
class TradingDay:
    trade_date: date
    benchmark_close: Decimal


class TradingCalendar:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def available(self) -> bool:
        return self.path.exists()

    def days(self, start: date, end: date) -> list[TradingDay]:
        if not self.path.exists():
            return []
        result: list[TradingDay] = []
        with self.path.open("r", encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                current = date.fromisoformat(str(row["date"])[:10])
                if start <= current <= end:
                    result.append(TradingDay(current, Decimal(str(row["close"]))))
        return sorted(result, key=lambda item: item.trade_date)

    def is_trading_day(self, value: date) -> bool:
        return any(item.trade_date == value for item in self.days(value, value))

    def missing_snapshot_dates(
        self, ledger: PaperLedger, account_id: str, start: date, end: date,
    ) -> list[date]:
        expected = [item.trade_date for item in self.days(start, end)]
        existing = ledger.snapshot_dates(account_id, start, end)
        return [item for item in expected if item not in existing]


class HistoricalCloseStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def close(self, symbol: str, trade_date: date) -> Decimal | None:
        path = self.root / f"{symbol.replace('.', '_')}.csv"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                if str(row.get("date", ""))[:10] == trade_date.isoformat():
                    return Decimal(str(row["close"]))
        return None


class SnapshotCatchup:
    def __init__(
        self, ledger: PaperLedger, account_id: str, calendar_path: str | Path,
        history_dir: str | Path,
    ) -> None:
        self.ledger = ledger
        self.account_id = account_id
        self.calendar = TradingCalendar(calendar_path)
        self.history = HistoricalCloseStore(history_dir)

    def catch_up(
        self, through: date | None, now: datetime,
        current_prices: dict[str, Decimal] | None = None,
    ) -> dict[str, Any]:
        if through is None or not self.calendar.available:
            return {
                "calendar_available": self.calendar.available,
                "filled": [],
                "unresolved": [],
            }
        start = self.ledger.account_created_date(self.account_id)
        days = self.calendar.days(start, through)
        existing = self.ledger.snapshot_dates(self.account_id, start, through)
        prices_now = current_prices or {}
        filled: list[str] = []
        unresolved: list[dict[str, str]] = []
        symbols = [row["symbol"] for row in self.ledger.positions(self.account_id)]
        for day in days:
            if day.trade_date in existing:
                continue
            if self.ledger.historical_state_changed_after(self.account_id, day.trade_date):
                unresolved.append({
                    "date": day.trade_date.isoformat(),
                    "reason": "该日之后已有成交或已应用公司行动，当前账本无法可靠倒推",
                })
                continue
            quote_prices = self.ledger.close_prices_from_quotes(symbols, day.trade_date)
            prices: dict[str, Decimal] = {}
            missing: list[str] = []
            for symbol in symbols:
                value = prices_now.get(symbol) if day.trade_date == through else None
                value = value or quote_prices.get(symbol) or self.history.close(symbol, day.trade_date)
                if value is None:
                    missing.append(symbol)
                else:
                    prices[symbol] = value
            if missing:
                unresolved.append({
                    "date": day.trade_date.isoformat(),
                    "reason": f"缺少收盘价: {', '.join(missing)}",
                })
                continue
            self.ledger.record_equity_snapshot_with_prices(
                self.account_id, day.trade_date, day.benchmark_close, prices, now,
            )
            filled.append(day.trade_date.isoformat())
        return {
            "calendar_available": True,
            "filled": filled,
            "unresolved": unresolved,
        }
