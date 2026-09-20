from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from paper_trading.fees import FeeConfig, load_fee_config


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    short_window: int
    long_window: int

    def __post_init__(self) -> None:
        if self.name != "ma_cross_v1":
            raise ValueError("only the explicit example strategy ma_cross_v1 is currently available")
        if not 1 <= self.short_window < self.long_window:
            raise ValueError("strategy windows must satisfy 1 <= short_window < long_window")


@dataclass(frozen=True)
class ExecutionConfig:
    lot_size: int
    slippage_bps: Decimal
    max_position_weight: Decimal
    max_volume_participation: Decimal
    suspension_warning_days: int = 5

    def __post_init__(self) -> None:
        object.__setattr__(self, "slippage_bps", Decimal(str(self.slippage_bps)))
        object.__setattr__(self, "max_position_weight", Decimal(str(self.max_position_weight)))
        object.__setattr__(self, "max_volume_participation", Decimal(str(self.max_volume_participation)))
        if self.lot_size <= 0 or self.slippage_bps < 0:
            raise ValueError("invalid lot_size or slippage")
        if not Decimal("0") < self.max_position_weight <= Decimal("1"):
            raise ValueError("max_position_weight must be in (0, 1]")
        if not Decimal("0") < self.max_volume_participation <= Decimal("1"):
            raise ValueError("max_volume_participation must be in (0, 1]")
        if self.suspension_warning_days < 1:
            raise ValueError("suspension_warning_days must be positive")


@dataclass(frozen=True)
class SplitConfig:
    train: Decimal
    validation: Decimal
    test: Decimal

    def __post_init__(self) -> None:
        for name in ("train", "validation", "test"):
            object.__setattr__(self, name, Decimal(str(getattr(self, name))))
        if self.train <= 0 or self.validation <= 0 or self.test <= 0:
            raise ValueError("all chronological split weights must be positive")
        if abs(self.train + self.validation + self.test - Decimal("1")) > Decimal("0.000001"):
            raise ValueError("chronological split weights must sum to 1")


@dataclass(frozen=True)
class BacktestConfig:
    symbols: tuple[str, ...]
    benchmark_symbol: str
    start_date: date
    end_date: date
    initial_cash: Decimal
    price_adjustment: str
    strategy: StrategyConfig
    execution: ExecutionConfig
    fees: FeeConfig
    splits: SplitConfig

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "start_date", date.fromisoformat(self.start_date) if isinstance(self.start_date, str) else self.start_date)
        object.__setattr__(self, "end_date", date.fromisoformat(self.end_date) if isinstance(self.end_date, str) else self.end_date)
        object.__setattr__(self, "initial_cash", Decimal(str(self.initial_cash)))
        for name, cls in (("strategy", StrategyConfig), ("execution", ExecutionConfig), ("fees", FeeConfig), ("splits", SplitConfig)):
            value = getattr(self, name)
            if isinstance(value, dict):
                object.__setattr__(self, name, cls(**value))
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be non-empty and unique")
        if any(not symbol.endswith((".SZ", ".SH", ".BJ")) for symbol in self.symbols):
            raise ValueError("symbols must include exchange suffix")
        if self.end_date <= self.start_date or self.initial_cash <= 0:
            raise ValueError("invalid date range or initial cash")
        if self.price_adjustment not in {"qfq", "hfq", "none"}:
            raise ValueError("price_adjustment must be qfq, hfq or none")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols), "benchmark_symbol": self.benchmark_symbol,
            "start_date": self.start_date.isoformat(), "end_date": self.end_date.isoformat(),
            "initial_cash": str(self.initial_cash), "price_adjustment": self.price_adjustment,
            "strategy": self.strategy.__dict__,
            "execution": {
                key: value if isinstance(value, int) else str(value)
                for key, value in self.execution.__dict__.items()
            },
            "fees": self.fees.to_dict(),
            "splits": {key: str(value) for key, value in self.splits.__dict__.items()},
        }


def load_backtest_config(path: str | Path) -> BacktestConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    shared_fees = load_fee_config()
    if "fees" in data and FeeConfig(**data["fees"]) != shared_fees:
        raise ValueError("backtest fee values differ from shared config/fees.json")
    data["fees"] = shared_fees.to_dict()
    return BacktestConfig(**data)
