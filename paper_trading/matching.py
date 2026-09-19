from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from .config import PaperConfig
from .models import FillResult, MarketQuote, OrderSide, SignalIntent, money


class PaperMatcher:
    version = "matching-v1"

    def __init__(self, config: PaperConfig) -> None:
        self.config = config

    def match(self, intent: SignalIntent, quote: MarketQuote, remaining: int) -> FillResult | None:
        if quote.timestamp <= intent.created_at:
            return None
        if not quote.tradable or remaining <= 0:
            return None

        if intent.side == OrderSide.BUY:
            if quote.ask > intent.limit_price or quote.ask_size <= 0:
                return None
            raw_price = quote.ask * (Decimal("1") + self.config.slippage_bps / Decimal("10000"))
            fill_price = min(intent.limit_price, money(raw_price))
            available = quote.ask_size
        else:
            if quote.bid < intent.limit_price or quote.bid_size <= 0:
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
        commission = max(
            self.config.minimum_commission,
            money(notional * self.config.commission_bps / Decimal("10000")),
        )
        stamp_tax = (
            money(notional * self.config.sell_stamp_tax_bps / Decimal("10000"))
            if intent.side == OrderSide.SELL else money(0)
        )
        transfer_fee = money(notional * self.config.transfer_fee_bps / Decimal("10000"))
        return FillResult(
            quantity=quantity,
            price=fill_price,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            quote_snapshot_id=quote.snapshot_id,
        )
