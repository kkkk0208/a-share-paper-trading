from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

import pandas as pd

from paper_trading.fees import FeeConfig, calculate_fees

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


def _position_list(row: pd.Series | None) -> list[dict[str, Any]]:
    if row is None:
        return []
    raw = row.get("open_positions", "[]")
    return json.loads(raw) if isinstance(raw, str) else []


def _attributed_pnl(
    trades: pd.DataFrame, base_row: pd.Series | None, end_row: pd.Series,
) -> dict[str, Any]:
    carry_positions = _position_list(base_row)
    carry: dict[str, dict[str, float]] = {}
    for position in carry_positions:
        quantity = int(position["quantity"])
        carry[position["symbol"]] = {
            "quantity": float(quantity),
            "unrealized_per_share": float(position["unrealized_pnl"]) / quantity if quantity else 0.0,
        }
    carry_in = sum(float(position["unrealized_pnl"]) for position in carry_positions)
    lifecycle_realized = 0.0
    attributed_realized = 0.0
    components: list[float] = []
    sells = trades[trades["side"] == "SELL"].sort_values("execution_date") if not trades.empty else trades
    for _, trade in sells.iterrows():
        realized = float(trade["realized_pnl"])
        lifecycle_realized += realized
        state = carry.get(str(trade["symbol"]))
        released = 0.0
        if state and state["quantity"] > 0:
            released_quantity = min(float(trade["quantity"]), state["quantity"])
            released = released_quantity * state["unrealized_per_share"]
            state["quantity"] -= released_quantity
        period_realized = realized - released
        attributed_realized += period_realized
        components.append(period_realized)

    remaining_carry_by_symbol = {
        symbol: state["quantity"] * state["unrealized_per_share"]
        for symbol, state in carry.items()
    }
    carry_remaining = sum(remaining_carry_by_symbol.values())
    unrealized_end = float(end_row.get("unrealized_pnl", 0.0))
    attributed_unrealized = 0.0
    for position in _position_list(end_row):
        contribution = float(position["unrealized_pnl"]) - remaining_carry_by_symbol.get(position["symbol"], 0.0)
        attributed_unrealized += contribution
        components.append(contribution)
    return {
        "carry_in": carry_in,
        "carry_released": carry_in - carry_remaining,
        "carry_remaining": carry_remaining,
        "lifecycle_realized": lifecycle_realized,
        "period_realized": attributed_realized,
        "unrealized_end": unrealized_end,
        "period_unrealized": attributed_unrealized,
        "components": components,
    }


def _expected_fee_rate(trades: pd.DataFrame, config: FeeConfig | None) -> float | None:
    if config is None or trades.empty:
        return None
    total_notional = float(trades["notional"].sum())
    if total_notional <= 0:
        return None
    expected = Decimal("0")
    for _, trade in trades.iterrows():
        side = str(trade["side"])
        notional = Decimal(str(trade["notional"]))
        expected += sum(calculate_fees(config, side, notional), Decimal("0"))
    return float(expected) / total_notional


def calculate_metrics(
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    risk_free_rate_annual: Decimal | float = 0,
    base_row: pd.Series | None = None,
    fee_config: FeeConfig | None = None,
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
    attribution = _attributed_pnl(trades, base_row, curve.iloc[-1])
    realized_pnl = attribution["lifecycle_realized"]
    total_fees = float(trades["total_fees"].sum()) if not trades.empty else 0.0
    total_notional = float(trades["notional"].sum()) if not trades.empty else 0.0
    average_equity = float(full_equity.mean())
    turnover = total_notional / average_equity if total_notional and average_equity else 0.0
    unrealized_end = attribution["unrealized_end"]
    total_pnl = float(equity.iloc[-1] - base_equity)
    period_realized = attribution["period_realized"]
    period_unrealized = attribution["period_unrealized"]
    mixed_signs = period_realized * period_unrealized < 0
    unrealized_share = (
        period_unrealized / total_pnl
        if total_pnl > 0 and period_unrealized >= 0 and not mixed_signs else None
    )
    unrealized_dominates = bool(
        total_pnl and period_unrealized > 0 and abs(period_unrealized) > abs(total_pnl) * 0.30
    )
    positions_raw = curve.iloc[-1].get("open_positions", "[]")
    open_positions = json.loads(positions_raw) if isinstance(positions_raw, str) else []
    attribution_error = attribution["period_realized"] + attribution["period_unrealized"] - total_pnl
    if abs(attribution_error) > 0.01:
        raise ValueError(f"period PnL attribution differs from marked equity by {attribution_error:.2f}")
    positive_components = sorted((item for item in attribution["components"] if item > 0), reverse=True)
    top1_share = positive_components[0] / total_pnl if positive_components and total_pnl > 0 else None
    top3_share = sum(positive_components[:3]) / total_pnl if positive_components and total_pnl > 0 else None
    effective_fee_rate = total_fees / total_notional if total_notional else None
    expected_fee_rate = _expected_fee_rate(trades, fee_config)
    fee_rate_deviation = (
        effective_fee_rate / expected_fee_rate - 1
        if effective_fee_rate is not None and expected_fee_rate else None
    )
    annualized_fee_drag = total_fees / average_equity / years if years > 0 and average_equity else None
    warnings: list[str] = []
    if unrealized_dominates:
        warnings.append(
            "期末浮盈超过本期总盈亏的 30%；累计收益对未平仓敞口敏感"
        )
    if 0 < len(sells) < 30:
        warnings.append(f"本期仅有 {len(sells)} 笔平仓交易，胜率与盈亏比统计不稳定")
    if buy_hold_excess < 0:
        warnings.append(f"策略相对标的买入持有落后 {abs(buy_hold_excess):.1%}")
    if fee_rate_deviation is not None and abs(fee_rate_deviation) > 0.10:
        warnings.append(f"实际有效费率与配置理论费率偏差 {fee_rate_deviation:.1%}，超过 10%")

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
        "carry_in_unrealized_pnl": attribution["carry_in"],
        "carry_in_unrealized_released": attribution["carry_released"],
        "carry_in_unrealized_remaining_at_end": attribution["carry_remaining"],
        "lifecycle_realized_pnl_closed_in_period": realized_pnl,
        "realized_pnl_in_period": period_realized,
        "unrealized_pnl_at_period_end": unrealized_end,
        "unrealized_pnl_in_period": period_unrealized,
        "unrealized_pnl_share_of_period_total": _finite(unrealized_share) if unrealized_share is not None else None,
        "mixed_realized_unrealized_signs": mixed_signs,
        "unrealized_pnl_dominates_period": unrealized_dominates,
        "pnl_attribution_check": {
            "formula": "realized_pnl_in_period + unrealized_pnl_in_period == ending_equity - starting_equity",
            "difference": attribution_error,
            "tolerance": 0.01,
            "passed": True,
        },
        "open_positions_at_period_end": open_positions,
        "top1_trade_pnl_share": _finite(top1_share) if top1_share is not None else None,
        "top3_trade_pnl_share": _finite(top3_share) if top3_share is not None else None,
        "total_fees_paid": total_fees,
        "fees_as_fraction_of_starting_equity": total_fees / base_equity if base_equity else None,
        "total_traded_notional": total_notional,
        "average_daily_equity": average_equity,
        "effective_fee_rate": effective_fee_rate,
        "expected_fee_rate": expected_fee_rate,
        "fee_rate_deviation": fee_rate_deviation,
        "annualized_fee_drag": annualized_fee_drag,
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
    fee_config: FeeConfig | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "full": calculate_metrics(curve, trades, risk_free_rate_annual, fee_config=fee_config),
    }
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
        result[name] = calculate_metrics(
            segment, segment_trades, risk_free_rate_annual, previous_end, fee_config,
        )
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
    result["strategy_vs_buy_and_hold_by_segment"] = {
        name: result[name]["strategy_vs_buy_and_hold_return"]
        for name in ("train", "validation", "test") if result.get(name)
    }
    test_segment = pieces["test"]
    if not test_segment.empty:
        test_base = pieces["validation"].iloc[-1] if not pieces["validation"].empty else None
        base_equity = float(test_base["equity"]) if test_base is not None else float(test_segment.iloc[0]["equity"])
        sensitivity = test_segment.tail(60)["equity"].astype(float) / base_equity - 1
        min_date = sensitivity.idxmin().date().isoformat()
        max_date = sensitivity.idxmax().date().isoformat()
        result["ending_date_sensitivity_last_60"] = {
            "definition": "test cumulative return from the fixed test opening equity to each of the final 60 trading-day endpoints",
            "fixed_test_base_date": (test_base.name if test_base is not None else test_segment.index[0]).date().isoformat(),
            "observations": int(len(sensitivity)),
            "minimum": float(sensitivity.min()),
            "median": float(sensitivity.median()),
            "maximum": float(sensitivity.max()),
            "minimum_end_date": min_date,
            "maximum_end_date": max_date,
            "negative_endings": int((sensitivity < 0).sum()),
            "negative_share": float((sensitivity < 0).mean()),
        }
    else:
        result["ending_date_sensitivity_last_60"] = None
    result["warnings"] = [
        f"{name}: {warning}"
        for name in ("full", "train", "validation", "test")
        if result.get(name)
        for warning in result[name]["warnings"]
    ]
    if result.get("test") and abs(result["full"]["max_drawdown"] - result["test"]["max_drawdown"]) < 1e-12:
        result["warnings"].append("全样本最大回撤发生在 test 段；样本外阶段包含历史最深回撤")
    sensitivity = result.get("ending_date_sensitivity_last_60")
    if sensitivity and sensitivity["minimum"] < 0:
        result["warnings"].append(
            f"test 末 60 个交易日终点敏感性最低为 {sensitivity['minimum']:.1%}；期末收益结论不稳健"
        )
    return result
