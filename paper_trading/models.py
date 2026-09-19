from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


MONEY_QUANT = Decimal("0.01")


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANT)


def parse_time(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    PROPOSED = "PROPOSED"
    RISK_REJECTED = "RISK_REJECTED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    USER_REJECTED = "USER_REJECTED"
    EXPIRED = "EXPIRED"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"


@dataclass(frozen=True)
class SignalIntent:
    intent_id: str
    created_at: datetime
    strategy_id: str
    symbol: str
    side: OrderSide
    quantity: int
    limit_price: Decimal
    valid_until: datetime
    reason_codes: tuple[str, ...]
    evidence_snapshot_id: str
    requires_user_approval: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", parse_time(self.created_at))
        object.__setattr__(self, "valid_until", parse_time(self.valid_until))
        object.__setattr__(self, "limit_price", money(self.limit_price))
        object.__setattr__(self, "side", OrderSide(self.side))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        if not self.intent_id or not self.strategy_id or not self.evidence_snapshot_id:
            raise ValueError("intent_id, strategy_id and evidence_snapshot_id are required")
        if not self.symbol.endswith((".SZ", ".SH", ".BJ")):
            raise ValueError("symbol must use exchange suffix, e.g. 002195.SZ")
        if self.quantity <= 0 or self.limit_price <= 0:
            raise ValueError("quantity and limit_price must be positive")
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until must be later than created_at")
        if not self.reason_codes:
            raise ValueError("at least one reason code is required")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["created_at"] = self.created_at.isoformat()
        result["valid_until"] = self.valid_until.isoformat()
        result["side"] = self.side.value
        result["limit_price"] = str(self.limit_price)
        result["reason_codes"] = list(self.reason_codes)
        return result

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "SignalIntent":
        return cls(**row)


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    timestamp: datetime
    last: Decimal
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    source: str
    tradable: bool = True
    limit_up: Decimal | None = None
    limit_down: Decimal | None = None
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", parse_time(self.timestamp))
        for name in ("last", "bid", "ask"):
            object.__setattr__(self, name, money(getattr(self, name)))
        for name in ("limit_up", "limit_down"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, money(value))
        if min(self.last, self.bid, self.ask) <= 0:
            raise ValueError("last, bid and ask must be positive")
        if self.bid_size < 0 or self.ask_size < 0:
            raise ValueError("quote sizes cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["timestamp"] = self.timestamp.isoformat()
        for name in ("last", "bid", "ask", "limit_up", "limit_down"):
            value = result[name]
            result[name] = None if value is None else str(value)
        return result


@dataclass(frozen=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RiskDecision:
    intent_id: str
    decision: str
    checked_at: datetime
    ruleset_version: str
    checks: tuple[RiskCheck, ...] = field(default_factory=tuple)

    @property
    def rejection_reasons(self) -> list[str]:
        return [check.detail for check in self.checks if not check.passed]


@dataclass(frozen=True)
class FillResult:
    quantity: int
    price: Decimal
    commission: Decimal
    stamp_tax: Decimal
    transfer_fee: Decimal
    quote_snapshot_id: str

    @property
    def fees(self) -> Decimal:
        return money(self.commission + self.stamp_tax + self.transfer_fee)
