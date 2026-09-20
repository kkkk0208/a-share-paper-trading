from __future__ import annotations

import math
from typing import Any

import pandas as pd

from .config import SplitConfig


def _finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def calculate_metrics(curve: pd.DataFrame, trades: pd.DataFrame) -> dict[str, Any]:
    if curve.empty:
        raise ValueError("cannot calculate metrics for an empty equity curve")
    equity = curve["equity"].astype(float)
    benchmark = curve["benchmark_equity"].astype(float)
    daily = equity.pct_change().dropna()
    cumulative = equity.iloc[-1] / equity.iloc[0] - 1
    benchmark_return = benchmark.iloc[-1] / benchmark.iloc[0] - 1
    years = max((len(equity) - 1) / 252, 0)
    annualized = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 and equity.iloc[0] > 0 else None
    drawdown = equity / equity.cummax() - 1
    volatility = daily.std(ddof=1) * math.sqrt(252) if len(daily) >= 2 else None
    sharpe = daily.mean() / daily.std(ddof=1) * math.sqrt(252) if len(daily) >= 2 and daily.std(ddof=1) > 0 else None
    sells = trades[trades["side"] == "SELL"] if not trades.empty else trades
    wins = sells[sells["realized_pnl"] > 0] if not sells.empty else sells
    gross_profit = float(wins["realized_pnl"].sum()) if not wins.empty else 0.0
    losses = sells[sells["realized_pnl"] < 0] if not sells.empty else sells
    gross_loss = abs(float(losses["realized_pnl"].sum())) if not losses.empty else 0.0
    turnover = float(trades["notional"].sum()) / float(equity.mean()) if not trades.empty and equity.mean() else 0.0
    return {
        "start": curve.index[0].date().isoformat(),
        "end": curve.index[-1].date().isoformat(),
        "trading_days": int(len(curve)),
        "cumulative_return": _finite(cumulative),
        "annualized_return": _finite(annualized) if annualized is not None else None,
        "max_drawdown": _finite(float(drawdown.min())),
        "annualized_volatility": _finite(volatility) if volatility is not None else None,
        "sharpe_ratio_rf0": _finite(sharpe) if sharpe is not None else None,
        "completed_trades": int(len(sells)),
        "win_rate": _finite(len(wins) / len(sells)) if len(sells) else None,
        "profit_factor": _finite(gross_profit / gross_loss) if gross_loss > 0 else None,
        "benchmark_return": _finite(benchmark_return),
        "excess_return": _finite(cumulative - benchmark_return),
        "turnover": _finite(turnover),
    }


def chronological_slices(curve: pd.DataFrame, splits: SplitConfig) -> dict[str, pd.DataFrame]:
    count = len(curve)
    train_end = max(1, int(count * float(splits.train)))
    validation_end = max(train_end + 1, int(count * float(splits.train + splits.validation)))
    validation_end = min(validation_end, count)
    return {
        "train": curve.iloc[:train_end],
        "validation": curve.iloc[train_end:validation_end],
        "test": curve.iloc[validation_end:],
    }


def segmented_metrics(curve: pd.DataFrame, trades: pd.DataFrame, splits: SplitConfig) -> dict[str, Any]:
    result: dict[str, Any] = {"full": calculate_metrics(curve, trades)}
    for name, segment in chronological_slices(curve, splits).items():
        if segment.empty:
            result[name] = None
            continue
        start, end = segment.index[0].date().isoformat(), segment.index[-1].date().isoformat()
        segment_trades = trades[
            (trades["execution_date"] >= start) & (trades["execution_date"] <= end)
        ] if not trades.empty else trades
        result[name] = calculate_metrics(segment, segment_trades)
    return result
