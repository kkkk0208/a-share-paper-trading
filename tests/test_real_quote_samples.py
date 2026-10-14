"""Checks the parsers against real responses captured with

    python -m paper_trading.collector capture --symbols 002195.SZ 002815.SZ 000300.SH --label continuous

Each capture is a .txt (raw response, UTF-8) plus a .json description in
tests/fixtures/quotes/real/. With no captures yet, these tests are skipped.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path

from paper_trading.quote_sources import parse_sina, parse_tencent


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "quotes" / "real"
PARSERS = {"tencent": parse_tencent, "sina": parse_sina}


def captures() -> list[dict]:
    result = []
    for path in sorted(FIXTURES.glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("error") or not meta.get("file"):
            continue
        meta["text"] = (FIXTURES / meta["file"]).read_text(encoding="utf-8")
        meta["captured"] = datetime.fromisoformat(meta["captured_at"])
        result.append(meta)
    return result


class RealQuoteSampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.samples = captures()
        if not self.samples:
            self.skipTest("no real captures yet; run `python -m paper_trading.collector capture` during trading hours")

    def test_every_requested_symbol_parses_or_has_an_explicit_reason(self):
        for sample in self.samples:
            batch = PARSERS[sample["source"]](sample["text"], sample["symbols"], sample["captured"])
            with self.subTest(file=sample["file"]):
                self.assertEqual(set(batch.quotes) | set(batch.errors), set(sample["symbols"]))
                self.assertTrue(batch.quotes, batch.errors)

    def test_basic_invariants_hold(self):
        for sample in self.samples:
            batch = PARSERS[sample["source"]](sample["text"], sample["symbols"], sample["captured"])
            for symbol, quote in batch.quotes.items():
                with self.subTest(file=sample["file"], symbol=symbol):
                    self.assertGreater(quote.last, 0)
                    bids = [price for price, _ in quote.bid_levels]
                    asks = [price for price, _ in quote.ask_levels]
                    self.assertEqual(bids, sorted(bids, reverse=True))
                    self.assertEqual(asks, sorted(asks))
                    if sample["source"] == "tencent" and not quote.index:
                        self.assertIsNotNone(quote.limit_up, "Tencent should report price limits for stocks")
                        self.assertIsNotNone(quote.limit_down)
                    if quote.limit_up is not None and quote.limit_down is not None:
                        self.assertLess(quote.limit_down, quote.limit_up)
                        self.assertTrue(quote.limit_down <= quote.last <= quote.limit_up)
                    if sample["phase"] == "continuous" and bids and asks:
                        self.assertLess(bids[0], asks[0])
                    if sample["phase"] == "continuous" and (bids or asks):
                        age = abs((sample["captured"] - quote.timestamp).total_seconds())
                        self.assertLess(age, 120, "quote time should be close to capture time while trading")

    def test_paired_captures_agree_between_sources(self):
        by_stem: dict[str, dict[str, dict]] = {}
        for sample in self.samples:
            stem = sample["file"].rsplit("_", 1)[0]
            by_stem.setdefault(stem, {})[sample["source"]] = sample
        pairs = [group for group in by_stem.values() if {"tencent", "sina"} <= set(group)]
        if not pairs:
            self.skipTest("no Tencent/Sina pairs captured at the same time")
        for group in pairs:
            tencent = parse_tencent(group["tencent"]["text"], group["tencent"]["symbols"], group["tencent"]["captured"])
            sina = parse_sina(group["sina"]["text"], group["sina"]["symbols"], group["sina"]["captured"])
            for symbol in set(tencent.quotes) & set(sina.quotes):
                a, b = tencent.quotes[symbol], sina.quotes[symbol]
                if abs((a.timestamp - b.timestamp).total_seconds()) > 10:
                    continue
                with self.subTest(stem=group["tencent"]["file"], symbol=symbol):
                    self.assertLess(abs(a.last - b.last) / a.last, 0.01)

    def test_named_edge_case_captures_keep_the_expected_market_shape(self):
        for sample in self.samples:
            batch = PARSERS[sample["source"]](sample["text"], sample["symbols"], sample["captured"])
            if sample.get("label") == "limit_up" and sample["source"] == "tencent":
                quote = next(iter(batch.quotes.values()))
                self.assertTrue(quote.bid_levels)
                self.assertFalse(quote.ask_levels)
                self.assertEqual(quote.last, quote.limit_up)
            if sample.get("label") == "suspended":
                quote = next(iter(batch.quotes.values()))
                self.assertFalse(quote.tradable)
                self.assertFalse(quote.bid_levels or quote.ask_levels)
                if sample["source"] == "sina":
                    self.assertEqual(quote.status, "suspended")


if __name__ == "__main__":
    unittest.main()
