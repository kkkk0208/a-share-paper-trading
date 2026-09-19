from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from tencent_quote import fetch_raw, normalize_symbol, parse

from .broker import PaperBroker
from .models import MarketQuote, money


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _decimal(value: str | None, field: str) -> Decimal:
    if value in (None, ""):
        raise ValueError(f"Tencent quote missing {field}")
    return money(value)


def _timestamp(value: str | None) -> datetime:
    if not value:
        raise ValueError("Tencent quote missing timestamp")
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)


class TencentPaperQuoteBroker(PaperBroker):
    """Real quote snapshots plus local matching. It cannot send broker orders."""

    def latest_quote(self, symbol: str) -> MarketQuote:
        code = symbol.split(".", 1)[0]
        raw = fetch_raw([normalize_symbol(code)])
        rows = parse(raw)
        if len(rows) != 1:
            raise RuntimeError(f"expected one Tencent quote for {symbol}, got {len(rows)}")
        row = rows[0]
        fields = row.raw_fields
        last = _decimal(row.last, "last")
        bid = _decimal(fields.get("field_9"), "bid1")
        ask = _decimal(fields.get("field_19"), "ask1")
        # Tencent level-one quantities are lots; convert to shares.
        bid_size = int(Decimal(fields.get("field_10") or "0") * 100)
        ask_size = int(Decimal(fields.get("field_20") or "0") * 100)
        return MarketQuote(
            symbol=symbol,
            timestamp=_timestamp(row.timestamp),
            last=last,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            source="tencent_level1_snapshot",
            tradable=bool(bid > 0 and ask > 0),
            limit_up=_decimal(fields.get("limit_up"), "limit_up") if fields.get("limit_up") else None,
            limit_down=_decimal(fields.get("limit_down"), "limit_down") if fields.get("limit_down") else None,
            snapshot_id=str(uuid.uuid4()),
        )

    def adapter_name(self) -> str:
        # Execution is still the deterministic local paper adapter.
        return "deterministic"
