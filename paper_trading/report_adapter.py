from __future__ import annotations

from decimal import Decimal
from typing import Any

from .ledger import PaperLedger


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def paper_account_summary(ledger: PaperLedger, account_id: str) -> dict[str, Any]:
    """Read-only projection for HTML/PDF reporting."""
    return _json_safe(ledger.status(account_id))
