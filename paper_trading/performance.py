from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .ledger import PaperLedger
from .snapshots import TradingCalendar


def calculate_performance(
    ledger: PaperLedger, account_id: str, start: date, end: date,
    calendar_path: str | Path | None = None,
) -> dict[str, Any]:
    if end < start:
        raise ValueError("--to must not be earlier than --from")
    with ledger.connection() as con:
        curve = con.execute(
            "SELECT * FROM equity_curve WHERE account_id=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (account_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        trades = con.execute(
            "SELECT f.realized_pnl FROM fills f JOIN orders o ON o.order_id=f.order_id "
            "WHERE o.side='SELL' AND date(f.created_at) BETWEEN ? AND ? ORDER BY f.created_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    missing_dates: list[str] = []
    calendar_available = bool(calendar_path and Path(calendar_path).exists())
    if calendar_available:
        missing_dates = [
            item.isoformat() for item in TradingCalendar(calendar_path).missing_snapshot_dates(
                ledger, account_id, start, end,
            )
        ]
    if len(curve) < 2:
        return {
            "status": "INSUFFICIENT_DATA",
            "snapshot_count": len(curve),
            "calendar_available": calendar_available,
            "snapshot_gap_count": len(missing_dates),
            "snapshot_gap_dates": missing_dates,
            "metrics_incomplete": True,
            "message": "绩效计算至少需要两个交易日的 equity_curve 收盘快照",
        }

    equities = [Decimal(row["equity"]) for row in curve]
    cumulative = equities[-1] / equities[0] - Decimal("1")
    calendar_days = max(1, (date.fromisoformat(curve[-1]["trade_date"]) - date.fromisoformat(curve[0]["trade_date"])).days)
    annualized = Decimal(str((float(equities[-1] / equities[0]) ** (365 / calendar_days)) - 1))
    peak = equities[0]
    max_drawdown = Decimal("0")
    for value in equities:
        peak = max(peak, value)
        drawdown = value / peak - Decimal("1")
        max_drawdown = min(max_drawdown, drawdown)

    realized = [Decimal(row["realized_pnl"]) for row in trades]
    wins = [value for value in realized if value > 0]
    losses = [value for value in realized if value < 0]
    win_rate = Decimal(len(wins)) / Decimal(len(realized)) if realized else None
    profit_loss_ratio = (
        (sum(wins, Decimal("0")) / len(wins)) / abs(sum(losses, Decimal("0")) / len(losses))
        if wins and losses else None
    )
    benchmark_first = Decimal(curve[0]["benchmark_close"])
    benchmark_last = Decimal(curve[-1]["benchmark_close"])
    benchmark_return = benchmark_last / benchmark_first - Decimal("1")
    return {
        "status": "OK" if not missing_dates else "INCOMPLETE",
        "from": curve[0]["trade_date"],
        "to": curve[-1]["trade_date"],
        "snapshot_count": len(curve),
        "cumulative_return": cumulative,
        "annualized_return": annualized,
        "max_drawdown": max_drawdown,
        "trade_count": len(realized),
        "win_rate": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
        "benchmark_return": benchmark_return,
        "excess_return": cumulative - benchmark_return,
        "calendar_available": calendar_available,
        "snapshot_gap_count": len(missing_dates),
        "snapshot_gap_dates": missing_dates,
        "metrics_incomplete": bool(missing_dates),
    }
