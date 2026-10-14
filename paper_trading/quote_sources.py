"""Batch Level 1 quote sources: Tencent (primary) and Sina (backup).

Both are unofficial public endpoints: no SLA, they may rate-limit or change format.
The parsers are pure functions over the response text, so they can be tested with
captured responses (tests/fixtures/quotes/real/, see `collector capture`).

Field positions follow the commonly documented layouts. They must be checked
against real captures; see docs/quote-collector.md.
"""

from __future__ import annotations

import re
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from .models import MarketQuote
from .symbols import from_vendor_code, is_index, split_symbol, to_vendor_code


SHANGHAI = ZoneInfo("Asia/Shanghai")

TENCENT_URL = "https://qt.gtimg.cn/q={codes}"
SINA_URL = "https://hq.sinajs.cn/list={codes}"
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn/"}
HTTP_TIMEOUT_SECONDS = 5.0

# Tencent "~" fields (0-based)
TENCENT_CODE, TENCENT_LAST, TENCENT_PREV_CLOSE, TENCENT_OPEN, TENCENT_VOLUME_LOTS = 2, 3, 4, 5, 6
TENCENT_BID_START, TENCENT_ASK_START = 9, 19      # price, lots, price, lots, ... five levels
TENCENT_TIMESTAMP, TENCENT_HIGH, TENCENT_LOW, TENCENT_AMOUNT_WAN = 30, 33, 34, 37
TENCENT_STATUS = 40
TENCENT_LIMIT_UP, TENCENT_LIMIT_DOWN = 47, 48

# Sina "," fields (0-based)
SINA_OPEN, SINA_PREV_CLOSE, SINA_LAST, SINA_HIGH, SINA_LOW = 1, 2, 3, 4, 5
SINA_VOLUME_SHARES, SINA_AMOUNT_YUAN = 8, 9
SINA_BID_START, SINA_ASK_START = 10, 20           # shares, price, shares, price, ... five levels
SINA_DATE, SINA_TIME, SINA_STATUS = 30, 31, 32
SINA_STATUS_NORMAL = "00"

FetchText = Callable[[str], str]


class QuoteSourceError(RuntimeError):
    """The whole request failed: network error, HTTP error or an unreadable payload."""


def http_get_gbk(url: str, headers: dict[str, str] | None = None, timeout: float = HTTP_TIMEOUT_SECONDS) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("gbk", errors="replace")


@dataclass(frozen=True)
class ParsedQuote:
    symbol: str
    source: str
    timestamp: datetime
    last: Decimal
    prev_close: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume_shares: int | None
    amount_yuan: Decimal | None
    bid_levels: tuple[tuple[Decimal, int], ...]
    ask_levels: tuple[tuple[Decimal, int], ...]
    limit_up: Decimal | None
    limit_down: Decimal | None
    status: str  # normal | suspended | unknown
    raw: str
    limit_note: str = ""  # set when limits come from the same-day cache, not from this source

    @property
    def index(self) -> bool:
        return is_index(self.symbol)

    @property
    def tradable(self) -> bool:
        """At least one side of the book is visible and the source does not report a halt.

        One empty side is normal at limit-up or limit-down, so it does not make the
        security untradable. Which side can trade is decided by MarketQuote.has_bid/has_ask.
        """
        return not self.index and self.status != "suspended" and bool(self.bid_levels or self.ask_levels)

    def with_limits(self, limit_up: Decimal, limit_down: Decimal, note: str) -> "ParsedQuote":
        return replace(self, limit_up=limit_up, limit_down=limit_down, limit_note=note)

    def to_market_quote(self, snapshot_id: str = "") -> MarketQuote:
        bid, bid_size = self.bid_levels[0] if self.bid_levels else (Decimal("0"), 0)
        ask, ask_size = self.ask_levels[0] if self.ask_levels else (Decimal("0"), 0)
        return MarketQuote(
            symbol=self.symbol, timestamp=self.timestamp, last=self.last,
            bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
            source=f"{self.source}_level1_snapshot" + (f"+{self.limit_note}" if self.limit_note else ""),
            tradable=self.tradable,
            limit_up=self.limit_up, limit_down=self.limit_down, snapshot_id=snapshot_id,
            bid_levels=self.bid_levels, ask_levels=self.ask_levels,
        )


@dataclass(frozen=True)
class SourceBatch:
    source: str
    fetched_at: datetime
    quotes: dict[str, ParsedQuote]
    errors: dict[str, str]
    raw_text: str


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    text = value.strip()
    if text in {"", "-", "--"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _positive(value: str | None) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def _field(fields: Sequence[str], position: int) -> str | None:
    return fields[position] if position < len(fields) else None


def _levels(pairs: Iterable[tuple[str | None, str | None]], multiplier: int) -> tuple[tuple[Decimal, int], ...]:
    """Keep only levels with a positive price and size. Zeros mean an empty level."""
    levels: list[tuple[Decimal, int]] = []
    for price_text, size_text in pairs:
        price, size = _positive(price_text), _decimal(size_text)
        if price is None or size is None or size <= 0:
            continue
        levels.append((price, int(size * multiplier)))
    return tuple(levels)


def _finish(source: str, symbols: Sequence[str], quotes: dict[str, ParsedQuote],
            errors: dict[str, str], fetched_at: datetime, text: str, missing_reason: str) -> SourceBatch:
    for symbol in symbols:
        if symbol not in quotes and symbol not in errors:
            errors[symbol] = missing_reason
    return SourceBatch(source, fetched_at, quotes, errors, text)


_TENCENT_LINE = re.compile(r'v_(?P<key>[a-z]{2}\d{6}|pv_none_match)="(?P<body>[^"]*)"')


def parse_tencent(text: str, symbols: Sequence[str], fetched_at: datetime) -> SourceBatch:
    wanted = {to_vendor_code(symbol): symbol for symbol in symbols}
    quotes: dict[str, ParsedQuote] = {}
    errors: dict[str, str] = {}
    for match in _TENCENT_LINE.finditer(text):
        symbol = wanted.get(match["key"])
        if symbol is None:
            continue
        if not match["body"]:
            errors[symbol] = "empty payload"
            continue
        try:
            quotes[symbol] = _parse_tencent_fields(symbol, match["body"].split("~"), match.group(0))
        except ValueError as exc:
            errors[symbol] = str(exc)
    missing = "source reports no match (unknown symbol?)" if "pv_none_match" in text else "not returned by source"
    return _finish("tencent", symbols, quotes, errors, fetched_at, text, missing)


def _parse_tencent_fields(symbol: str, fields: list[str], raw: str) -> ParsedQuote:
    code, _ = split_symbol(symbol)
    if len(fields) <= TENCENT_LOW:
        raise ValueError(f"tencent payload has only {len(fields)} fields")
    if fields[TENCENT_CODE].strip() != code:
        raise ValueError(f"tencent code mismatch: {fields[TENCENT_CODE]!r}")
    last = _positive(fields[TENCENT_LAST])
    if last is None:
        raise ValueError("tencent last price is missing or zero")
    stamp = fields[TENCENT_TIMESTAMP].strip()
    if not re.fullmatch(r"\d{14}", stamp):
        raise ValueError(f"tencent timestamp is malformed: {stamp!r}")
    index = is_index(symbol)
    volume = _decimal(_field(fields, TENCENT_VOLUME_LOTS))
    amount = _decimal(_field(fields, TENCENT_AMOUNT_WAN))
    return ParsedQuote(
        symbol=symbol, source="tencent",
        timestamp=datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI),
        last=last,
        prev_close=_positive(_field(fields, TENCENT_PREV_CLOSE)),
        open=_positive(_field(fields, TENCENT_OPEN)),
        high=_positive(_field(fields, TENCENT_HIGH)),
        low=_positive(_field(fields, TENCENT_LOW)),
        # Book and volume are reported in lots of 100 shares.
        volume_shares=None if index or volume is None else int(volume * 100),
        amount_yuan=None if amount is None else amount * 10000,
        bid_levels=() if index else _levels(
            ((_field(fields, TENCENT_BID_START + 2 * i), _field(fields, TENCENT_BID_START + 2 * i + 1))
             for i in range(5)), 100),
        ask_levels=() if index else _levels(
            ((_field(fields, TENCENT_ASK_START + 2 * i), _field(fields, TENCENT_ASK_START + 2 * i + 1))
             for i in range(5)), 100),
        limit_up=None if index else _positive(_field(fields, TENCENT_LIMIT_UP)),
        limit_down=None if index else _positive(_field(fields, TENCENT_LIMIT_DOWN)),
        status="suspended" if (_field(fields, TENCENT_STATUS) or "").strip() == "S" else "unknown",
        raw=raw,
    )


_SINA_LINE = re.compile(r'hq_str_(?P<key>[a-z]{2}\d{6})="(?P<body>[^"]*)"')


def parse_sina(text: str, symbols: Sequence[str], fetched_at: datetime) -> SourceBatch:
    wanted = {to_vendor_code(symbol): symbol for symbol in symbols}
    quotes: dict[str, ParsedQuote] = {}
    errors: dict[str, str] = {}
    for match in _SINA_LINE.finditer(text):
        symbol = wanted.get(match["key"])
        if symbol is None:
            continue
        if not match["body"]:
            errors[symbol] = "empty payload (unknown symbol?)"
            continue
        try:
            quotes[symbol] = _parse_sina_fields(symbol, match["body"].split(","), match.group(0))
        except ValueError as exc:
            errors[symbol] = str(exc)
    return _finish("sina", symbols, quotes, errors, fetched_at, text, "not returned by source")


def _parse_sina_fields(symbol: str, fields: list[str], raw: str) -> ParsedQuote:
    if len(fields) <= SINA_TIME:
        raise ValueError(f"sina payload has only {len(fields)} fields")
    status_code = (_field(fields, SINA_STATUS) or "").strip()
    status = "unknown" if not status_code else ("normal" if status_code == SINA_STATUS_NORMAL else "suspended")
    last = _positive(fields[SINA_LAST])
    if last is None and status == "suspended":
        # Suspended Sina rows use zero for the live price but retain the last
        # reference price in prev_close.  Keep the row auditable/non-tradable.
        last = _positive(fields[SINA_PREV_CLOSE])
    if last is None:
        raise ValueError("sina last price is missing or zero (not traded yet?)")
    try:
        timestamp = datetime.strptime(
            f"{fields[SINA_DATE].strip()} {fields[SINA_TIME].strip()}", "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        raise ValueError(f"sina timestamp is malformed: {fields[SINA_DATE]!r} {fields[SINA_TIME]!r}") from exc
    index = is_index(symbol)
    volume = _decimal(fields[SINA_VOLUME_SHARES])
    return ParsedQuote(
        symbol=symbol, source="sina", timestamp=timestamp, last=last,
        prev_close=_positive(fields[SINA_PREV_CLOSE]),
        open=_positive(fields[SINA_OPEN]),
        high=_positive(fields[SINA_HIGH]),
        low=_positive(fields[SINA_LOW]),
        volume_shares=None if index or volume is None else int(volume),
        amount_yuan=_decimal(fields[SINA_AMOUNT_YUAN]),
        # Book sizes are reported in shares, size first then price.
        bid_levels=() if index else _levels(
            ((_field(fields, SINA_BID_START + 2 * i + 1), _field(fields, SINA_BID_START + 2 * i))
             for i in range(5)), 1),
        ask_levels=() if index else _levels(
            ((_field(fields, SINA_ASK_START + 2 * i + 1), _field(fields, SINA_ASK_START + 2 * i))
             for i in range(5)), 1),
        limit_up=None, limit_down=None,  # Sina does not publish price limits
        status=status,
        raw=raw,
    )


class BatchQuoteSource(ABC):
    name = "source"
    line_marker = ""  # every valid response contains this text

    def __init__(self, fetch_text: FetchText | None = None) -> None:
        self._fetch_text = fetch_text

    @abstractmethod
    def request_url(self, symbols: Sequence[str]) -> str:
        raise NotImplementedError

    @abstractmethod
    def parse(self, text: str, symbols: Sequence[str], fetched_at: datetime) -> SourceBatch:
        raise NotImplementedError

    def download(self, url: str) -> str:
        return http_get_gbk(url)

    def fetch_text(self, symbols: Sequence[str]) -> str:
        url = self.request_url(symbols)
        try:
            return self._fetch_text(url) if self._fetch_text else self.download(url)
        except Exception as exc:
            raise QuoteSourceError(f"{self.name} request failed: {exc}") from exc

    def fetch(self, symbols: Sequence[str], now: datetime | None = None) -> SourceBatch:
        fetched_at = now or datetime.now().astimezone()
        if not symbols:
            return SourceBatch(self.name, fetched_at, {}, {}, "")
        text = self.fetch_text(symbols)
        if self.line_marker not in text:
            raise QuoteSourceError(f"{self.name} returned an unreadable payload: {text[:120]!r}")
        return self.parse(text, symbols, fetched_at)


class TencentSource(BatchQuoteSource):
    name = "tencent"
    line_marker = "v_"

    def request_url(self, symbols: Sequence[str]) -> str:
        return TENCENT_URL.format(codes=",".join(to_vendor_code(symbol) for symbol in symbols))

    def download(self, url: str) -> str:
        """Prefer TLS, but tolerate Tencent's endpoint closing TLS connections.

        Some networks can reach qt.gtimg.cn only over plain HTTP.  The payload is
        still treated as untrusted market data and must pass the normal parser,
        freshness and cross-source quality checks before it can drive an order.
        """
        try:
            return http_get_gbk(url)
        except Exception as https_error:
            http_url = url.replace("https://", "http://", 1)
            try:
                return http_get_gbk(http_url)
            except Exception as http_error:
                raise QuoteSourceError(
                    f"HTTPS failed ({https_error}); HTTP fallback failed ({http_error})"
                ) from http_error

    def parse(self, text: str, symbols: Sequence[str], fetched_at: datetime) -> SourceBatch:
        return parse_tencent(text, symbols, fetched_at)


class SinaSource(BatchQuoteSource):
    name = "sina"
    line_marker = "hq_str_"

    def request_url(self, symbols: Sequence[str]) -> str:
        return SINA_URL.format(codes=",".join(to_vendor_code(symbol) for symbol in symbols))

    def download(self, url: str) -> str:
        return http_get_gbk(url, SINA_HEADERS)

    def parse(self, text: str, symbols: Sequence[str], fetched_at: datetime) -> SourceBatch:
        return parse_sina(text, symbols, fetched_at)


__all__ = [
    "BatchQuoteSource", "ParsedQuote", "QuoteSourceError", "SinaSource", "SourceBatch", "TencentSource",
    "from_vendor_code", "parse_sina", "parse_tencent",
]
