from __future__ import annotations

from decimal import Decimal

from .ledger import PaperLedger
from .models import money


def reconcile(ledger: PaperLedger, account_id: str) -> list[str]:
    errors: list[str] = []
    account = ledger.account(account_id)
    with ledger.connection() as con:
        last_cash = con.execute(
            "SELECT balance_after FROM cash_ledger WHERE account_id=? ORDER BY id DESC LIMIT 1", (account_id,)
        ).fetchone()
        if not last_cash or money(last_cash["balance_after"]) != account["cash"]:
            errors.append("cash ledger balance does not match account cash")
        for position in ledger.positions(account_id):
            last_position = con.execute(
                "SELECT quantity_after,available_after FROM position_ledger WHERE account_id=? AND symbol=? ORDER BY id DESC LIMIT 1",
                (account_id, position["symbol"]),
            ).fetchone()
            if not last_position:
                errors.append(f"{position['symbol']} has no position ledger entry")
                continue
            if int(last_position["quantity_after"]) != position["quantity"]:
                errors.append(f"{position['symbol']} quantity mismatch")
            lot_quantity = con.execute(
                "SELECT COALESCE(SUM(quantity),0) quantity FROM position_lots WHERE account_id=? AND symbol=?",
                (account_id, position["symbol"]),
            ).fetchone()["quantity"]
            if int(lot_quantity) != position["quantity"]:
                errors.append(f"{position['symbol']} lot quantity mismatch")
        cash_sum = con.execute(
            "SELECT amount FROM cash_ledger WHERE account_id=? ORDER BY id", (account_id,)
        ).fetchall()
        calculated = money(sum((Decimal(row["amount"]) for row in cash_sum), Decimal("0")))
        if calculated != account["cash"]:
            errors.append("cash ledger entries do not sum to account cash")
    return errors
