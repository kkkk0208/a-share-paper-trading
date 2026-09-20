from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .models import money


DEFAULT_FEE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "fees.json"


@dataclass(frozen=True)
class FeeConfig:
    commission_rate: Decimal
    commission_min: Decimal
    stamp_duty_rate: Decimal
    stamp_duty_side: str
    transfer_fee_rate: Decimal
    transfer_fee_side: str

    def __post_init__(self) -> None:
        for name in ("commission_rate", "stamp_duty_rate", "transfer_fee_rate"):
            object.__setattr__(self, name, Decimal(str(getattr(self, name))))
        object.__setattr__(self, "commission_min", money(self.commission_min))
        object.__setattr__(self, "stamp_duty_side", self.stamp_duty_side.upper())
        object.__setattr__(self, "transfer_fee_side", self.transfer_fee_side.upper())
        if min(self.commission_rate, self.stamp_duty_rate, self.transfer_fee_rate) < 0:
            raise ValueError("fee rates cannot be negative")
        if self.commission_min < 0:
            raise ValueError("commission_min cannot be negative")
        if self.stamp_duty_side not in {"BUY", "SELL", "BOTH", "NONE"}:
            raise ValueError("invalid stamp_duty_side")
        if self.transfer_fee_side not in {"BUY", "SELL", "BOTH", "NONE"}:
            raise ValueError("invalid transfer_fee_side")

    def to_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in self.__dict__.items()}


def load_fee_config(path: str | Path | None = None) -> FeeConfig:
    source = Path(path) if path else DEFAULT_FEE_CONFIG_PATH
    return FeeConfig(**json.loads(source.read_text(encoding="utf-8")))


def calculate_fees(config: FeeConfig, side: str, notional: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    normalized_side = side.upper()
    amount = money(notional)
    commission = max(config.commission_min, money(amount * config.commission_rate))
    stamp = money(amount * config.stamp_duty_rate) if config.stamp_duty_side in {normalized_side, "BOTH"} else money(0)
    transfer = money(amount * config.transfer_fee_rate) if config.transfer_fee_side in {normalized_side, "BOTH"} else money(0)
    return commission, stamp, transfer
