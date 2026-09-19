from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import money


ALLOWED_PAPER_ADAPTERS = {"deterministic", "vnpy_paper"}


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


@dataclass(frozen=True)
class MatchingConfig:
    min_depth_ratio: Decimal
    min_ticks_delay: int
    max_quote_age_seconds: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "min_depth_ratio", Decimal(str(self.min_depth_ratio)))
        if self.min_depth_ratio <= 0 or self.min_ticks_delay < 0 or self.max_quote_age_seconds <= 0:
            raise ValueError("invalid matching constraints")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "min_depth_ratio": str(self.min_depth_ratio),
            "min_ticks_delay": self.min_ticks_delay,
            "max_quote_age_seconds": self.max_quote_age_seconds,
        }


@dataclass(frozen=True)
class PaperConfig:
    account_id: str
    currency: str
    initial_cash: Decimal
    paper_mode: bool
    adapter: str
    whitelist: tuple[str, ...]
    max_order_value: Decimal
    max_symbol_value: Decimal
    max_total_exposure: Decimal
    min_cash_buffer: Decimal
    max_daily_loss: Decimal
    approval_ttl_seconds: int
    intent_ttl_minutes: int
    lot_size: int
    max_participation_rate: Decimal
    slippage_bps: Decimal
    fees: FeeConfig
    matching: MatchingConfig
    quote_failover_failures: int
    ruleset_version: str

    def __post_init__(self) -> None:
        for name in (
            "initial_cash", "max_order_value", "max_symbol_value", "max_total_exposure",
            "min_cash_buffer", "max_daily_loss",
        ):
            object.__setattr__(self, name, money(getattr(self, name)))
        for name in (
            "max_participation_rate", "slippage_bps",
        ):
            object.__setattr__(self, name, Decimal(str(getattr(self, name))))
        if isinstance(self.fees, dict):
            object.__setattr__(self, "fees", FeeConfig(**self.fees))
        if isinstance(self.matching, dict):
            object.__setattr__(self, "matching", MatchingConfig(**self.matching))
        object.__setattr__(self, "whitelist", tuple(self.whitelist))
        if not self.paper_mode:
            raise ValueError("paper_mode must remain true")
        if self.adapter not in ALLOWED_PAPER_ADAPTERS:
            raise ValueError(f"adapter must be one of {sorted(ALLOWED_PAPER_ADAPTERS)}")
        if self.initial_cash <= 0 or self.lot_size <= 0:
            raise ValueError("initial_cash and lot_size must be positive")
        if not self.whitelist:
            raise ValueError("whitelist cannot be empty")
        if self.approval_ttl_seconds <= 0 or self.intent_ttl_minutes <= 0:
            raise ValueError("approval and intent TTL must be positive")
        if self.quote_failover_failures <= 0:
            raise ValueError("quote_failover_failures must be positive")
        if not (Decimal("0") < self.max_participation_rate <= Decimal("1")):
            raise ValueError("max_participation_rate must be in (0, 1]")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        result = dict(self.__dict__)
        for key, value in list(result.items()):
            if isinstance(value, Decimal):
                result[key] = str(value)
            elif isinstance(value, tuple):
                result[key] = list(value)
            elif isinstance(value, FeeConfig):
                result[key] = value.to_dict()
            elif isinstance(value, MatchingConfig):
                result[key] = value.to_dict()
        return result


def load_config(path: str | Path) -> PaperConfig:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"paper trading config does not exist: {source}")
    data = json.loads(source.read_text(encoding="utf-8"))
    data.setdefault("intent_ttl_minutes", 30)
    data.setdefault("quote_failover_failures", 3)
    if "fees" not in data:
        data["fees"] = {
            "commission_rate": str(Decimal(str(data.pop("commission_bps"))) / Decimal("10000")),
            "commission_min": data.pop("minimum_commission"),
            "stamp_duty_rate": str(Decimal(str(data.pop("sell_stamp_tax_bps"))) / Decimal("10000")),
            "stamp_duty_side": "SELL",
            "transfer_fee_rate": str(Decimal(str(data.pop("transfer_fee_bps"))) / Decimal("10000")),
            "transfer_fee_side": "BOTH",
        }
    if "matching" not in data:
        data["matching"] = {
            "min_depth_ratio": "3.0",
            "min_ticks_delay": 1,
            "max_quote_age_seconds": data.pop("max_quote_age_seconds"),
        }
    required = set(PaperConfig.__dataclass_fields__)
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"missing config fields: {', '.join(missing)}")
    return PaperConfig(**{key: data[key] for key in required})
