"""Exchange trading calendar for live trading decisions.

This file lists trading days, including the rest of the current year, so "today"
is known before the market opens. It is kept separate from data/trading_calendar.csv,
which holds benchmark closes from history and therefore never contains today.

Days after the last listed trading day are unknown, and unknown fails closed.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable


SYNC_COMMAND = "python -m paper_trading.collector sync-calendar"


class ExchangeCalendar:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache: tuple[float, tuple[date, ...]] | None = None

    def _dates(self) -> tuple[date, ...]:
        if not self.path.exists():
            return ()
        mtime = self.path.stat().st_mtime
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        with self.path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            columns = reader.fieldnames or []
            # "date" keeps older benchmark-style files readable.
            column = next((name for name in ("trade_date", "date") if name in columns), None)
            if column is None:
                raise ValueError(f"{self.path.name} needs a trade_date column")
            values = sorted({
                date.fromisoformat(str(row[column]).strip()[:10]) for row in reader if str(row.get(column) or "").strip()
            })
        self._cache = (mtime, tuple(values))
        return self._cache[1]

    @property
    def available(self) -> bool:
        try:
            return bool(self._dates())
        except ValueError:
            return False

    @property
    def first_date(self) -> date | None:
        dates = self._dates()
        return dates[0] if dates else None

    @property
    def last_date(self) -> date | None:
        dates = self._dates()
        return dates[-1] if dates else None

    def status(self, day: date) -> str:
        """missing | before_coverage | after_coverage | trading | closed"""
        dates = self._dates()
        if not dates:
            return "missing"
        if day < dates[0]:
            return "before_coverage"
        if day > dates[-1]:
            return "after_coverage"
        return "trading" if day in set(dates) else "closed"

    def is_trading_day(self, day: date) -> bool:
        return self.status(day) == "trading"

    def check(self, day: date) -> tuple[bool, str]:
        try:
            status = self.status(day)
        except (OSError, ValueError) as exc:
            return False, f"交易日历无法读取：{exc}"
        if status == "trading":
            return True, f"{day} 是交易日"
        if status == "missing":
            return False, f"交易日历 {self.path.name} 不存在或为空，请先运行：{SYNC_COMMAND}"
        if status == "after_coverage":
            return False, f"交易日历只覆盖到 {self.last_date}，{day} 超出范围，请重新运行：{SYNC_COMMAND}"
        if status == "before_coverage":
            return False, f"{day} 早于交易日历起点 {self.first_date}"
        return False, f"{day} 不是交易日"


def write_trade_dates(path: str | Path, days: Iterable[date]) -> dict[str, Any]:
    values = sorted(set(days))
    if not values:
        raise ValueError("refusing to write an empty trading calendar")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as sink:
        writer = csv.writer(sink)
        writer.writerow(["trade_date"])
        writer.writerows([[value.isoformat()] for value in values])
    temp.replace(target)
    return {"path": str(target), "count": len(values), "first": values[0].isoformat(), "last": values[-1].isoformat()}


def _akshare_trade_dates() -> list[date]:
    import akshare as ak  # optional dependency, see requirements-backtesting.txt

    frame = ak.tool_trade_date_hist_sina()
    column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
    return [date.fromisoformat(str(value)[:10]) for value in frame[column].tolist()]


def sync_trade_dates(path: str | Path, loader: Callable[[], Iterable[date]] | None = None) -> dict[str, Any]:
    """Download Sina's trading calendar through AKShare (it lists the rest of the current year)."""
    return write_trade_dates(path, (loader or _akshare_trade_dates)())
