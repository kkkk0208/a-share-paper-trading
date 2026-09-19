from __future__ import annotations

from abc import ABC, abstractmethod

from .models import MarketQuote, SignalIntent


class PaperBroker(ABC):
    """Paper-only broker boundary. Implementations must never expose live routing."""

    paper_only = True

    @abstractmethod
    def latest_quote(self, symbol: str) -> MarketQuote:
        raise NotImplementedError

    @abstractmethod
    def adapter_name(self) -> str:
        raise NotImplementedError


class StaticQuoteBroker(PaperBroker):
    def __init__(self, quote: MarketQuote, name: str = "deterministic") -> None:
        self.quote = quote
        self.name = name

    def latest_quote(self, symbol: str) -> MarketQuote:
        if symbol != self.quote.symbol:
            raise KeyError(symbol)
        return self.quote

    def adapter_name(self) -> str:
        return self.name
