from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN

from .config import PaperConfig
from .fees import calculate_fees
from .models import FillResult, MarketQuote, OrderSide, SignalIntent, money


class PaperMatcher:
    version = "matching-v1"

    def __init__(self, config: PaperConfig) -> None:
        self.config = config

    def match(
        self, intent: SignalIntent, quote: MarketQuote, remaining: int,
        approved_at: datetime, ticks_after_approval: int, now: datetime,
    ) -> FillResult | None:
        if quote.timestamp <= approved_at:
            return None
        if ticks_after_approval < self.config.matching.min_ticks_delay:
            return None
        age = (now - quote.timestamp).total_seconds()
        if age < -2 or age > self.config.matching.max_quote_age_seconds:
            return None
        if not quote.tradable or remaining <= 0:
            return None

        if intent.side == OrderSide.BUY:
            if quote.ask > intent.limit_price or quote.ask_size <= 0:
                return None
            if Decimal(quote.ask_size) < Decimal(intent.quantity) * self.config.matching.min_depth_ratio:
                return None
            raw_price = quote.ask * (Decimal("1") + self.config.slippage_bps / Decimal("10000"))
            fill_price = min(intent.limit_price, money(raw_price))
            available = quote.ask_size
        else:
            if quote.bid < intent.limit_price or quote.bid_size <= 0:
                return None
            if Decimal(quote.bid_size) < Decimal(intent.quantity) * self.config.matching.min_depth_ratio:
                return None
            raw_price = quote.bid * (Decimal("1") - self.config.slippage_bps / Decimal("10000"))
            fill_price = max(intent.limit_price, money(raw_price))
            available = quote.bid_size

        participation = int(
            (Decimal(available) * self.config.max_participation_rate).to_integral_value(rounding=ROUND_DOWN)
        )
        quantity = min(remaining, max(0, participation))
        if quantity <= 0:
            return None

        notional = money(fill_price * quantity)
        commission, stamp_tax, transfer_fee = calculate_fees(self.config.fees, intent.side.value, notional)
        return FillResult(
            quantity=quantity,
            price=fill_price,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            quote_snapshot_id=quote.snapshot_id,
            quote_timestamp=quote.timestamp,
            order_book={
                "bids": [[str(price), size] for price, size in quote.bid_levels],
                "asks": [[str(price), size] for price, size in quote.ask_levels],
            },
        )
