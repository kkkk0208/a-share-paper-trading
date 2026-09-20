from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json

import pandas as pd

from paper_trading.models import money
from paper_trading.fees import calculate_fees

from .config import BacktestConfig
from .strategy import moving_average_target


@dataclass
class Position:
    quantity: int
    acquired_date: date
    entry_price: Decimal
    remaining_entry_fees: Decimal


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    warnings: tuple[str, ...] = ()


def _fees(config: BacktestConfig, side: str, notional: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    return calculate_fees(config.fees, side, notional)


def _limit_up_rate(symbol: str) -> Decimal:
    code, exchange = symbol.split(".", 1)
    if exchange == "BJ" or code.startswith(("4", "8")):
        return Decimal("0.30")
    if code.startswith(("300", "301", "688", "689")):
        return Decimal("0.20")
    return Decimal("0.10")


def _limit_up_price(symbol: str, previous_close: Decimal) -> Decimal:
    return (previous_close * (Decimal("1") + _limit_up_rate(symbol))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


def _missing_run_warning(symbol: str, frame: pd.DataFrame, calendar: pd.Index, threshold: int) -> str | None:
    missing = set(calendar) - set(frame.index)
    longest = current = 0
    for timestamp in calendar:
        current = current + 1 if timestamp in missing else 0
        longest = max(longest, current)
    if longest >= threshold:
        return f"{symbol} has a {longest}-trading-day history gap; moving averages span a suspension/data gap"
    return None


def _empty_trades() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "symbol", "signal_date", "execution_date", "side", "quantity", "raw_open",
        "fill_price", "notional", "commission", "stamp_duty", "transfer_fee",
        "total_fees", "realized_pnl",
    ])


def run_backtest(
    config: BacktestConfig,
    histories: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame,
) -> BacktestResult:
    """Run a long-only, close-signal/next-open execution backtest.

    Historical frames must already be normalized and indexed by trading date.  The
    benchmark calendar is authoritative, so a signal can never execute on the same
    bar that produced it.
    """
    missing = sorted(set(config.symbols) - set(histories))
    if missing:
        raise ValueError(f"missing histories: {', '.join(missing)}")

    start, end = pd.Timestamp(config.start_date), pd.Timestamp(config.end_date)
    benchmark = benchmark.loc[start:end].copy()
    if benchmark.empty:
        raise ValueError("benchmark has no rows in configured date range")
    frames: dict[str, pd.DataFrame] = {}
    warnings: list[str] = []
    for symbol in config.symbols:
        frame = histories[symbol].loc[start:end].copy()
        if frame.empty:
            raise ValueError(f"{symbol} has no rows in configured date range")
        frame["target"] = moving_average_target(frame, config.strategy)
        frames[symbol] = frame
        warning = _missing_run_warning(
            symbol, frame, benchmark.index, config.execution.suspension_warning_days,
        )
        if warning:
            warnings.append(warning)

    cash = money(config.initial_cash)
    positions: dict[str, Position] = {}
    pending: dict[str, tuple[int, date]] = {}
    trade_rows: list[dict[str, object]] = []
    curve_rows: list[dict[str, object]] = []
    last_close: dict[str, Decimal] = {}
    benchmark_base = Decimal(str(benchmark.iloc[0]["close"]))
    buy_hold_bases = {
        symbol: Decimal(str(frame.iloc[0]["close"])) for symbol, frame in frames.items()
    }
    buy_hold_marks = dict(buy_hold_bases)
    slip = config.execution.slippage_bps / Decimal("10000")

    for timestamp in benchmark.index:
        trade_date = timestamp.date()

        # Mark at today's open before sizing. Missing bars retain their last close.
        opening_equity = cash
        for symbol, position in positions.items():
            frame = frames[symbol]
            mark = Decimal(str(frame.at[timestamp, "open"])) if timestamp in frame.index else last_close.get(symbol, position.entry_price)
            opening_equity += money(mark * position.quantity)

        # Exit orders run first, which makes released cash available to entries.
        for desired_side in (0, 1):
            for symbol in config.symbols:
                if symbol not in pending or pending[symbol][0] != desired_side:
                    continue
                frame = frames[symbol]
                if timestamp not in frame.index:
                    continue
                target, signal_date = pending[symbol]
                raw_open = Decimal(str(frame.at[timestamp, "open"]))
                daily_volume = int(frame.at[timestamp, "volume"])
                volume_cap = int(
                    (Decimal(daily_volume) * config.execution.max_volume_participation)
                    // config.execution.lot_size * config.execution.lot_size
                )
                position = positions.get(symbol)

                if target == 0 and position is not None:
                    if trade_date <= position.acquired_date or volume_cap <= 0:
                        continue
                    quantity = min(position.quantity, volume_cap)
                    fill_price = money(raw_open * (Decimal("1") - slip))
                    notional = money(fill_price * quantity)
                    commission, stamp, transfer = _fees(config, "SELL", notional)
                    total_fees = money(commission + stamp + transfer)
                    entry_fee_share = money(position.remaining_entry_fees * Decimal(quantity) / Decimal(position.quantity))
                    realized = money(
                        (fill_price - position.entry_price) * quantity - entry_fee_share - total_fees
                    )
                    cash = money(cash + notional - total_fees)
                    position.quantity -= quantity
                    position.remaining_entry_fees = money(position.remaining_entry_fees - entry_fee_share)
                    if position.quantity == 0:
                        del positions[symbol]
                        del pending[symbol]
                    trade_rows.append({
                        "symbol": symbol, "signal_date": signal_date.isoformat(),
                        "execution_date": trade_date.isoformat(), "side": "SELL",
                        "quantity": quantity, "raw_open": float(raw_open),
                        "fill_price": float(fill_price), "notional": float(notional),
                        "commission": float(commission), "stamp_duty": float(stamp),
                        "transfer_fee": float(transfer), "total_fees": float(total_fees),
                        "realized_pnl": float(realized),
                    })

                if target == 1 and position is None and volume_cap >= config.execution.lot_size:
                    row_number = frame.index.get_loc(timestamp)
                    if row_number > 0:
                        previous_close = Decimal(str(frame.iloc[row_number - 1]["close"]))
                        if raw_open >= _limit_up_price(symbol, previous_close):
                            continue
                    target_value = money(opening_equity * config.execution.max_position_weight)
                    fill_price = money(raw_open * (Decimal("1") + slip))
                    max_qty = int(target_value // fill_price)
                    quantity = min(max_qty, volume_cap)
                    quantity = quantity // config.execution.lot_size * config.execution.lot_size
                    while quantity >= config.execution.lot_size:
                        notional = money(fill_price * quantity)
                        commission, stamp, transfer = _fees(config, "BUY", notional)
                        total_fees = money(commission + stamp + transfer)
                        if notional + total_fees <= cash:
                            break
                        quantity -= config.execution.lot_size
                    if quantity < config.execution.lot_size:
                        continue
                    cash = money(cash - notional - total_fees)
                    positions[symbol] = Position(quantity, trade_date, fill_price, total_fees)
                    del pending[symbol]
                    trade_rows.append({
                        "symbol": symbol, "signal_date": signal_date.isoformat(),
                        "execution_date": trade_date.isoformat(), "side": "BUY",
                        "quantity": quantity, "raw_open": float(raw_open),
                        "fill_price": float(fill_price), "notional": float(notional),
                        "commission": float(commission), "stamp_duty": float(stamp),
                        "transfer_fee": float(transfer), "total_fees": float(total_fees),
                        "realized_pnl": None,
                    })

        market_value = Decimal("0")
        unrealized_pnl = Decimal("0")
        open_positions: list[dict[str, object]] = []
        for symbol, position in positions.items():
            frame = frames[symbol]
            if timestamp in frame.index:
                last_close[symbol] = Decimal(str(frame.at[timestamp, "close"]))
            mark = last_close.get(symbol, position.entry_price)
            position_value = money(mark * position.quantity)
            position_unrealized = money(
                (mark - position.entry_price) * position.quantity - position.remaining_entry_fees
            )
            market_value += position_value
            unrealized_pnl += position_unrealized
            open_positions.append({
                "symbol": symbol,
                "quantity": position.quantity,
                "cost_price": float(position.entry_price),
                "close": float(mark),
                "market_value": float(position_value),
                "unrealized_pnl": float(position_unrealized),
            })
        equity = money(cash + market_value)
        benchmark_close = Decimal(str(benchmark.at[timestamp, "close"]))
        benchmark_equity = money(config.initial_cash * benchmark_close / benchmark_base)
        buy_hold_equity = Decimal("0")
        equal_allocation = config.initial_cash / Decimal(len(config.symbols))
        for symbol in config.symbols:
            if timestamp in frames[symbol].index:
                buy_hold_marks[symbol] = Decimal(str(frames[symbol].at[timestamp, "close"]))
            buy_hold_equity += equal_allocation * buy_hold_marks[symbol] / buy_hold_bases[symbol]
        curve_rows.append({
            "date": trade_date.isoformat(), "cash": float(cash),
            "market_value": float(market_value), "equity": float(equity),
            "benchmark_equity": float(benchmark_equity),
            "buy_and_hold_equity": float(money(buy_hold_equity)),
            "unrealized_pnl": float(money(unrealized_pnl)),
            "open_positions": json.dumps(open_positions, ensure_ascii=False, sort_keys=True),
        })

        # Signals are observed only after the closing mark and become pending for a
        # later trading date. This ordering is the core look-ahead safeguard.
        for symbol in config.symbols:
            frame = frames[symbol]
            if timestamp not in frame.index:
                continue
            target = int(frame.at[timestamp, "target"])
            is_held = symbol in positions
            if target != int(is_held):
                pending[symbol] = (target, trade_date)
            elif symbol in pending:
                del pending[symbol]

    curve = pd.DataFrame(curve_rows).set_index("date")
    curve.index = pd.to_datetime(curve.index)
    trades = pd.DataFrame(trade_rows) if trade_rows else _empty_trades()
    return BacktestResult(equity_curve=curve, trades=trades, warnings=tuple(warnings))
