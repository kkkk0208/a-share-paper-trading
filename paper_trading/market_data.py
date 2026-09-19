from __future__ import annotations

import re
import urllib.request
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from tencent_quote import fetch_raw, normalize_symbol, parse

from .broker import PaperBroker
from .models import MarketQuote, money


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _decimal(value: str | None, field: str, source: str) -> Decimal:
    if value in (None, ""):
        raise ValueError(f"{source} quote missing {field}")
    return money(value)


class QuoteProvider(ABC):
    """Normalizes external quote sources into one fail-closed snapshot model."""

    @abstractmethod
    def latest_quote(self, symbol: str) -> MarketQuote:
        raise NotImplementedError


class TencentQuoteProvider(QuoteProvider):
    def latest_quote(self, symbol: str) -> MarketQuote:
        code = symbol.split(".", 1)[0]
        rows = parse(fetch_raw([normalize_symbol(code)]))
        if len(rows) != 1:
            raise RuntimeError(f"expected one Tencent quote for {symbol}, got {len(rows)}")
        row = rows[0]
        fields = row.raw_fields
        if not row.timestamp:
            raise ValueError("Tencent quote missing timestamp")
        timestamp = datetime.strptime(row.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        last = _decimal(row.last, "last", "Tencent")
        bid = _decimal(fields.get("field_9"), "bid1", "Tencent")
        ask = _decimal(fields.get("field_19"), "ask1", "Tencent")
        bid_levels = tuple(
            (_decimal(fields.get(f"field_{9 + level * 2}"), f"bid{level + 1}", "Tencent"),
             int(Decimal(fields.get(f"field_{10 + level * 2}") or "0") * 100))
            for level in range(5) if fields.get(f"field_{9 + level * 2}")
        )
        ask_levels = tuple(
            (_decimal(fields.get(f"field_{19 + level * 2}"), f"ask{level + 1}", "Tencent"),
             int(Decimal(fields.get(f"field_{20 + level * 2}") or "0") * 100))
            for level in range(5) if fields.get(f"field_{19 + level * 2}")
        )
        return MarketQuote(
            symbol=symbol, timestamp=timestamp, last=last, bid=bid, ask=ask,
            bid_size=bid_levels[0][1], ask_size=ask_levels[0][1],
            source="tencent_level1_snapshot", tradable=bool(bid > 0 and ask > 0),
            limit_up=_decimal(fields.get("limit_up"), "limit_up", "Tencent") if fields.get("limit_up") else None,
            limit_down=_decimal(fields.get("limit_down"), "limit_down", "Tencent") if fields.get("limit_down") else None,
            snapshot_id=str(uuid.uuid4()), bid_levels=bid_levels, ask_levels=ask_levels,
        )


class SinaQuoteProvider(QuoteProvider):
    endpoint = "https://hq.sinajs.cn/list={symbol}"

    def latest_quote(self, symbol: str) -> MarketQuote:
        code = normalize_symbol(symbol.split(".", 1)[0])
        request = urllib.request.Request(
            self.endpoint.format(symbol=code),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            text = response.read().decode("gbk", errors="replace")
        match = re.search(r'="(.*)"', text)
        if not match:
            raise RuntimeError(f"Sina quote missing payload for {symbol}")
        fields = match.group(1).split(",")
        if len(fields) < 33:
            raise RuntimeError(f"Sina quote has {len(fields)} fields for {symbol}")
        timestamp = datetime.strptime(f"{fields[30]} {fields[31]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
        bid_levels = tuple(
            (_decimal(fields[11 + level * 2], f"bid{level + 1}", "Sina"), int(fields[10 + level * 2] or 0))
            for level in range(5) if fields[11 + level * 2]
        )
        ask_levels = tuple(
            (_decimal(fields[21 + level * 2], f"ask{level + 1}", "Sina"), int(fields[20 + level * 2] or 0))
            for level in range(5) if fields[21 + level * 2]
        )
        bid = _decimal(fields[6], "bid1", "Sina")
        ask = _decimal(fields[7], "ask1", "Sina")
        status = fields[32].strip()
        return MarketQuote(
            symbol=symbol, timestamp=timestamp, last=_decimal(fields[3], "last", "Sina"),
            bid=bid, ask=ask, bid_size=bid_levels[0][1], ask_size=ask_levels[0][1],
            source="sina_level1_snapshot", tradable=status == "00" and bid > 0 and ask > 0,
            limit_up=None, limit_down=None, snapshot_id=str(uuid.uuid4()),
            bid_levels=bid_levels, ask_levels=ask_levels,
        )


class FailoverQuoteProvider(QuoteProvider):
    def __init__(self, primary: QuoteProvider, secondary: QuoteProvider, failure_threshold: int = 3) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        self.primary = primary
        self.secondary = secondary
        self.failure_threshold = failure_threshold
        self.primary_failures = 0
        self.using_secondary = False

    def latest_quote(self, symbol: str) -> MarketQuote:
        primary_error: Exception | None = None
        if not self.using_secondary:
            try:
                quote = self.primary.latest_quote(symbol)
                self.primary_failures = 0
                return quote
            except Exception as exc:
                primary_error = exc
                self.primary_failures += 1
                if self.primary_failures < self.failure_threshold:
                    raise RuntimeError(
                        f"primary quote failed ({self.primary_failures}/{self.failure_threshold}); fail closed"
                    ) from exc
                self.using_secondary = True
        try:
            return self.secondary.latest_quote(symbol)
        except Exception as secondary_error:
            detail = f"primary={primary_error or 'disabled'}; secondary={secondary_error}"
            raise RuntimeError(f"all quote providers failed; fail closed: {detail}") from secondary_error


_DEFAULT_PROVIDERS: dict[int, FailoverQuoteProvider] = {}


class ResilientPaperQuoteBroker(PaperBroker):
    """External snapshots plus local matching; it cannot route real orders."""

    def __init__(self, provider: QuoteProvider | None = None, failure_threshold: int = 3) -> None:
        if provider is None:
            provider = _DEFAULT_PROVIDERS.setdefault(
                failure_threshold,
                FailoverQuoteProvider(TencentQuoteProvider(), SinaQuoteProvider(), failure_threshold),
            )
        self.provider = provider

    def latest_quote(self, symbol: str) -> MarketQuote:
        return self.provider.latest_quote(symbol)

    def adapter_name(self) -> str:
        return "deterministic"


TencentPaperQuoteBroker = ResilientPaperQuoteBroker
