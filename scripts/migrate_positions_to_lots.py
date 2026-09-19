from __future__ import annotations

import argparse

from paper_trading.ledger import PaperLedger


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy positions into historical FIFO lots")
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    ledger = PaperLedger(args.db)
    print(f"positions_to_lots migration is applied; database={ledger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
