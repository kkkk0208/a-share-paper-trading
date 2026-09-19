from __future__ import annotations

from typing import Any

from ..broker import PaperBroker
from ..models import MarketQuote


class VnpyPaperAdapter(PaperBroker):
    """Safety boundary for vnpy_paperaccount.

    The package is optional. This adapter intentionally refuses to start until a
    caller provides an initialized PaperAccount engine and a quote converter.
    No live vn.py gateway is accepted here.
    """

    def __init__(self, paper_engine: Any, quote_provider: Any) -> None:
        try:
            import vnpy_paperaccount  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("vnpy_paperaccount is not installed in the paper environment") from exc
        if paper_engine is None or quote_provider is None:
            raise ValueError("paper engine and quote provider are required")
        engine_name = type(paper_engine).__module__.lower() + "." + type(paper_engine).__name__.lower()
        if "paper" not in engine_name:
            raise ValueError("refusing a vn.py engine that is not explicitly a paper engine")
        self.paper_engine = paper_engine
        self.quote_provider = quote_provider

    def latest_quote(self, symbol: str) -> MarketQuote:
        quote = self.quote_provider(symbol)
        if not isinstance(quote, MarketQuote):
            raise TypeError("quote_provider must return MarketQuote")
        return quote

    def adapter_name(self) -> str:
        return "vnpy_paper"
