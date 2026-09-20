from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

import pandas as pd

from .config import SplitConfig


def _finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def _wilson_interval(wins: int, count: int, z: float = 1.959963984540054) -> list[float] | None:
    if count == 0:
        return None
    proportion = wins / count
    denominator = 1 + z * z / count
    center = (proportion + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / count + z * z / (4 * count * count)) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def calculate_metrics(
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    risk_free_rate_annual: Decimal | float = 0,
    base_row: pd.Series | None = None,
) -> dict[str, Any]:
    if curve.empty:
        raise ValueError("cannot calculate metrics for an empty equity curve")
    equity = curve["equity"].astype(float)
    benchmark = curve["benchmark_equity"].astype(float)
    buy_hold = curve["buy_and_hold_equity"].astype(float)
    base_equity = float(base_row["equity"]) if base_row is not None else float(equity.iloc[0])
    base_benchmark = float(base_row["benchmark_equity"]) if base_row is not None else float(benchmark.iloc[0])
    base_buy_hold = float(base_row["buy_and_hold_equity"]) if base_row is not None else float(buy_hold.iloc[0])
    return_count = len(curve) if base_row is not None else max(0, len(curve) - 1)

    full_equity = pd.concat([
        pd.Series([base_equity], index=[curve.index[0] - pd.Timedelta(nanoseconds=1)]), equity,
    ]) if base_row is not None else equity
    daily = full_equity.pct_change().dropna()
    cumulative = equity.iloc[-1] / base_equity - 1
    benchmark_return = benchmark.iloc[-1] / base_benchmark - 1
    buy_hold_return = buy_hold.iloc[-1] / base_buy_hold - 1
    buy_hold_excess = (1 + cumulative) / (1 + buy_hold_return) - 1
    relative_excess = (1 + cumulative) / (1 + benchmark_return) - 1
    years = return_count / 252
    sufficient_sample = return_count >= 60
    annualized = (equity.iloc[-1] / base_equity) ** (1 / years) - 1 if sufficient_sample and years > 0 else None
    benchmark_annualized = (benchmark.iloc[-1] / base_benchmark) ** (1 / years) - 1 if sufficient_sample and years > 0 else None
    drawdown = full_equity / full_equity.cummax() - 1
    volatility = daily.std(ddof=1) * math.sqrt(252) if sufficient_sample and len(daily) >= 2 else None
    risk_free = float(risk_free_rate_annual)
    daily_rf = (1 + risk_free) ** (1 / 252) - 1
    sharpe = (
        (daily.mean() - daily_rf) / daily.std(ddof=1) * math.sqrt(252)
        if sufficient_sample and len(daily) >= 2 and daily.std(ddof=1) > 0 else None
    )

    sells = trades[trades["side"] == "SELL"] if not trades.empty else trades
    wins = sells[sells["realized_pnl"] > 0] if not sells.empty else sells
    losses = sells[sells["realized_pnl"] < 0] if not sells.empty else sells
    gross_profit = float(wins["realized_pnl"].sum()) if not wins.empty else 0.0
    gross_loss = abs(float(losses["realized_pnl"].sum())) if not losses.empty else 0.0
    realized_pnl = float(sells["realized_pnl"].sum()) if not sells.empty else 0.0
    total_fees = float(trades["total_fees"].sum()) if not trades.empty else 0.0
    turnover = float(trades["notional"].sum()) / float(full_equity.mean()) if not trades.empty else 0.0
    unrealized_end = float(curve.iloc[-1].get("unrealized_pnl", 0.0))
    total_pnl = float(equity.iloc[-1] - base_equity)
    unrealized_share = unrealized_end / total_pnl if total_pnl > 0 and unrealized_end > 0 else None
    positions_raw = curve.iloc[-1].get("open_positions", "[]")
    open_positions = json.loads(positions_raw) if isinstance(positions_raw, str) else []
    warnings: list[str] = []
    if unrealized_share is not None and unrealized_share > 0.30:
        warnings.append(
            f"期末浮盈占本期总盈亏 {unrealized_share:.1%}，超过 30%；累计收益对未平仓敞口敏感"
        )
    if 0 < len(sells) < 30:
        warnings.append(f"本期仅有 {len(sells)} 笔平仓交易，胜率与盈亏比统计不稳定")
    if buy_hold_excess < 0:
        warnings.append(f"策略相对标的买入持有落后 {abs(buy_hold_excess):.1%}")

    return {
        "start": curve.index[0].date().isoformat(),
        "end": curve.index[-1].date().isoformat(),
        "return_base_date": (base_row.name if base_row is not None else curve.index[0]).date().isoformat(),
        "trading_days": int(len(curve)),
        "return_observations": int(return_count),
        "sample_sufficient_for_annualized_metrics": sufficient_sample,
        "cumulative_return": _finite(cumulative),
        "annualized_return": _finite(annualized) if annualized is not None else None,
        "max_drawdown": _finite(float(drawdown.min())),
        "annualized_volatility": _finite(volatility) if volatility is not None else None,
        "sharpe_ratio": _finite(sharpe) if sharpe is not None else None,
        "risk_free_rate_annual": risk_free,
        "completed_trades": int(len(sells)),
        "win_rate": _finite(len(wins) / len(sells)) if len(sells) else None,
        "win_rate_95pct_wilson": _wilson_interval(len(wins), len(sells)),
        "profit_factor": _finite(gross_profit / gross_loss) if gross_loss > 0 else None,
        "realized_pnl": realized_pnl,
        "unrealized_pnl_at_period_end": unrealized_end,
        "unrealized_pnl_share_of_period_total": _finite(unrealized_share) if unrealized_share is not None else None,
        "open_positions_at_period_end": open_positions,
        "total_fees_paid": total_fees,
        "fees_as_fraction_of_starting_equity": total_fees / base_equity if base_equity else None,
        "benchmark_return": _finite(benchmark_return),
        "benchmark_annualized_return": _finite(benchmark_annualized) if benchmark_annualized is not None else None,
        "buy_and_hold_return": _finite(buy_hold_return),
        "strategy_vs_buy_and_hold_return": _finite(buy_hold_excess),
        "buy_and_hold_definition": "single symbol: that symbol; multiple symbols: equal-weight close-to-close basket, rebalanced only at inception",
        "excess_return": _finite(relative_excess),
        "excess_return_method": "geometric relative return: (1+portfolio)/(1+benchmark)-1",
        "turnover": _finite(turnover),
        "turnover_definition": "double-sided traded notional divided by average daily equity",
        "warnings": warnings,
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


def segmented_metrics(
    curve: pd.DataFrame, trades: pd.DataFrame, splits: SplitConfig,
    risk_free_rate_annual: Decimal | float = 0,
) -> dict[str, Any]:
    result: dict[str, Any] = {"full": calculate_metrics(curve, trades, risk_free_rate_annual)}
    pieces = chronological_slices(curve, splits)
    previous_end: pd.Series | None = None
    for name, segment in pieces.items():
        if segment.empty:
            result[name] = None
            continue
        start, end = segment.index[0].date().isoformat(), segment.index[-1].date().isoformat()
        segment_trades = trades[
            (trades["execution_date"] >= start) & (trades["execution_date"] <= end)
        ] if not trades.empty else trades
        result[name] = calculate_metrics(segment, segment_trades, risk_free_rate_annual, previous_end)
        previous_end = segment.iloc[-1]

    segment_growth = math.prod(
        1 + result[name]["cumulative_return"] for name in ("train", "validation", "test")
        if result[name] is not None
    )
    full_growth = 1 + result["full"]["cumulative_return"]
    chain_error = abs(segment_growth / full_growth - 1)
    if chain_error > 0.001:
        raise ValueError(f"chronological segment chain differs from full return by {chain_error:.6%}")
    result["segmentation_check"] = {"chain_error": chain_error, "tolerance": 0.001, "passed": True}
    result["warnings"] = [
        f"{name}: {warning}"
        for name in ("full", "train", "validation", "test")
        if result.get(name)
        for warning in result[name]["warnings"]
    ]
    if result.get("test") and abs(result["full"]["max_drawdown"] - result["test"]["max_drawdown"]) < 1e-12:
        result["warnings"].append("全样本最大回撤发生在 test 段；样本外阶段包含历史最深回撤")
    return result
