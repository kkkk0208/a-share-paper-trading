"""Symbol helpers. The exchange suffix is always kept, so 000300.SH can never be
mistaken for a Shenzhen code."""

from __future__ import annotations

import re

_SYMBOL = re.compile(r"^(?P<code>\d{6})\.(?P<exchange>SH|SZ|BJ)$")
_VENDOR = re.compile(r"^(?P<prefix>sh|sz|bj)(?P<code>\d{6})$")


def split_symbol(symbol: str) -> tuple[str, str]:
    match = _SYMBOL.match(symbol.strip().upper())
    if not match:
        raise ValueError(f"symbol must look like 002195.SZ or 000300.SH, got {symbol!r}")
    return match["code"], match["exchange"]


def to_vendor_code(symbol: str) -> str:
    """002195.SZ -> sz002195 (format used by both Tencent and Sina)."""
    code, exchange = split_symbol(symbol)
    return f"{exchange.lower()}{code}"


def from_vendor_code(vendor_code: str) -> str:
    match = _VENDOR.match(vendor_code.strip().lower())
    if not match:
        raise ValueError(f"unknown vendor code {vendor_code!r}")
    return f"{match['code']}.{match['prefix'].upper()}"


def is_index(symbol: str) -> bool:
    """Index codes have no order book and no price limits."""
    code, exchange = split_symbol(symbol)
    return (
        (exchange == "SH" and code.startswith("000"))
        or (exchange == "SZ" and code.startswith("399"))
        or (exchange == "BJ" and code.startswith("899"))
    )
