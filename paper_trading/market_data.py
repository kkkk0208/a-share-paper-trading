from __future__ import annotations

import time as _time
import uuid
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from .broker import PaperBroker
from .models import MarketQuote
from .quote_quality import Phase, session_phase
from .quote_sources import BatchQuoteSource, SinaSource, TencentSource
from .quote_store import SqliteLimitCache, latest_stored_quote


SHANGHAI = ZoneInfo("Asia/Shanghai")


class QuoteProvider(ABC):
    """Normalizes external quote sources into one fail-closed snapshot model."""

    @abstractmethod
    def latest_quote(self, symbol: str) -> MarketQuote:
        raise NotImplementedError


class SourceQuoteProvider(QuoteProvider):
    def __init__(self, source: BatchQuoteSource) -> None:
        self.source = source

    def latest_quote(self, symbol: str) -> MarketQuote:
        batch = self.source.fetch([symbol])
        parsed = batch.quotes.get(symbol)
        if parsed is None:
            reason = batch.errors.get(symbol, "unknown error")
            raise RuntimeError(f"{self.source.name} quote unavailable for {symbol}: {reason}")
        return parsed.to_market_quote(snapshot_id=str(uuid.uuid4()))


class TencentQuoteProvider(SourceQuoteProvider):
    def __init__(self, source: BatchQuoteSource | None = None) -> None:
        super().__init__(source or TencentSource())


class SinaQuoteProvider(SourceQuoteProvider):
    def __init__(self, source: BatchQuoteSource | None = None) -> None:
        super().__init__(source or SinaSource())


class FailoverQuoteProvider(QuoteProvider):
    """Primary first. After `failure_threshold` consecutive errors use the secondary,
    and try the primary again every `recovery_seconds` so the switch is not permanent."""

    def __init__(
        self, primary: QuoteProvider, secondary: QuoteProvider, failure_threshold: int = 3,
        *, recovery_seconds: float = 60.0, clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        self.primary = primary
        self.secondary = secondary
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self.primary_failures = 0
        self.using_secondary = False
        self._secondary_since: float | None = None

    def latest_quote(self, symbol: str) -> MarketQuote:
        primary_error: Exception | None = None
        if self.using_secondary and self._clock() - (self._secondary_since or 0.0) >= self.recovery_seconds:
            try:
                quote = self.primary.latest_quote(symbol)
            except Exception as exc:
                primary_error = exc
                self._secondary_since = self._clock()
            else:
                self.using_secondary = False
                self.primary_failures = 0
                self._secondary_since = None
                return quote
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
                self._secondary_since = self._clock()
        try:
            return self.secondary.latest_quote(symbol)
        except Exception as secondary_error:
            detail = f"primary={primary_error or 'disabled'}; secondary={secondary_error}"
            raise RuntimeError(f"all quote providers failed; fail closed: {detail}") from secondary_error


class LimitPriceCache(Protocol):
    def get(self, symbol: str, trade_date: date) -> tuple[Decimal, Decimal] | None: ...

    def put(self, symbol: str, trade_date: date, limit_up: Decimal, limit_down: Decimal, source: str) -> bool: ...


class MemoryLimitCache:
    def __init__(self) -> None:
        self._values: dict[tuple[str, date], tuple[Decimal, Decimal]] = {}

    def get(self, symbol: str, trade_date: date) -> tuple[Decimal, Decimal] | None:
        return self._values.get((symbol, trade_date))

    def put(self, symbol: str, trade_date: date, limit_up: Decimal, limit_down: Decimal, source: str) -> bool:
        key = (symbol, trade_date)
        if key in self._values:
            return False
        self._values[key] = (limit_up, limit_down)
        return True


class LimitFillingProvider(QuoteProvider):
    """Price limits do not change during a trading day. Remember them from a source that
    reports them (Tencent) and reuse them for one that does not (Sina). Nothing is
    estimated: without a same-day value the quote keeps None and risk still fails closed."""

    def __init__(self, inner: QuoteProvider, cache: LimitPriceCache) -> None:
        self.inner = inner
        self.cache = cache

    def latest_quote(self, symbol: str) -> MarketQuote:
        quote = self.inner.latest_quote(symbol)
        trade_date = quote.timestamp.astimezone(SHANGHAI).date()
        if quote.limit_up is not None and quote.limit_down is not None:
            if session_phase(quote.timestamp) not in {Phase.CLOSED, Phase.PRE_MARKET}:
                self.cache.put(symbol, trade_date, quote.limit_up, quote.limit_down, quote.source)
            return quote
        cached = self.cache.get(symbol, trade_date)
        if cached is None:
            return quote
        return replace(quote, limit_up=cached[0], limit_down=cached[1], source=f"{quote.source}+cached_limits")


_DEFAULT_PROVIDERS: dict[tuple[int, str | None], QuoteProvider] = {}


class ResilientPaperQuoteBroker(PaperBroker):
    """External snapshots plus local matching; it cannot route real orders."""

    def __init__(
        self, provider: QuoteProvider | None = None, failure_threshold: int = 3,
        limit_cache: LimitPriceCache | None = None,
    ) -> None:
        if provider is None:
            key = (failure_threshold, getattr(limit_cache, "cache_key", None))
            provider = _DEFAULT_PROVIDERS.get(key)
            if provider is None:
                failover = FailoverQuoteProvider(TencentQuoteProvider(), SinaQuoteProvider(), failure_threshold)
                provider = LimitFillingProvider(failover, limit_cache or MemoryLimitCache())
                _DEFAULT_PROVIDERS[key] = provider
        self.provider = provider

    def latest_quote(self, symbol: str) -> MarketQuote:
        return self.provider.latest_quote(symbol)

    def adapter_name(self) -> str:
        return "deterministic"


TencentPaperQuoteBroker = ResilientPaperQuoteBroker


class StoreFirstQuoteBroker(PaperBroker):
    """Use the newest tick the collector stored when it is fresh; otherwise fetch live.

    With the collector running, trading reads the same data that is being recorded,
    and source failover state lives in one long-running process.
    """

    def __init__(
        self, db_path: str | Path, live: PaperBroker, max_age_seconds: float = 6.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.live = live
        self.max_age_seconds = max_age_seconds
        self._clock = clock or (lambda: datetime.now().astimezone())

    def latest_quote(self, symbol: str) -> MarketQuote:
        stored = latest_stored_quote(self.db_path, symbol)
        if stored is not None:
            age = (self._clock() - stored.timestamp).total_seconds()
            if -2 <= age <= self.max_age_seconds:
                return stored
        return self.live.latest_quote(symbol)

    def adapter_name(self) -> str:
        return "deterministic"


def paper_quote_broker(db_path: str | Path, failure_threshold: int = 3) -> PaperBroker:
    """Default broker for the CLI and dashboard: collector ticks first, live Tencent/Sina second."""
    live = ResilientPaperQuoteBroker(failure_threshold=failure_threshold, limit_cache=SqliteLimitCache(db_path))
    return StoreFirstQuoteBroker(db_path, live)
