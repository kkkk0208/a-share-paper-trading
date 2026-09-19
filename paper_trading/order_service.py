from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .broker import PaperBroker
from .config import PaperConfig
from .ledger import PaperLedger
from .matching import PaperMatcher
from .models import MarketQuote, OrderStatus, SignalIntent
from .risk_engine import RiskEngine


class OrderService:
    def __init__(
        self, config: PaperConfig, ledger: PaperLedger, broker: PaperBroker,
        runtime_dir: str | Path,
    ) -> None:
        if not getattr(broker, "paper_only", False):
            raise ValueError("broker does not declare paper_only")
        if broker.adapter_name() != config.adapter:
            raise ValueError("broker adapter does not match config")
        self.config = config
        self.ledger = ledger
        self.broker = broker
        self.runtime_dir = Path(runtime_dir)
        self.risk = RiskEngine(config, ledger, runtime_dir)
        self.matcher = PaperMatcher(config)

    def propose(self, intent: SignalIntent, quote: MarketQuote, now: datetime) -> dict[str, Any]:
        self.ledger.add_intent(intent, now)
        snapshot_id = self.ledger.save_quote(quote, now)
        if not quote.snapshot_id:
            quote = MarketQuote(**(quote.to_dict() | {"snapshot_id": snapshot_id}))
        decision = self.risk.evaluate(intent, quote, now)
        self.ledger.save_risk_decision(decision)
        return {
            "intent_id": intent.intent_id,
            "status": self.ledger.intent_status(intent.intent_id),
            "decision": decision.decision,
            "rejection_reasons": decision.rejection_reasons,
        }

    def approve(self, intent_id: str, actor: str, note: str, now: datetime) -> dict[str, Any]:
        if not actor.strip():
            raise ValueError("approval actor is required")
        self.ledger.record_approval(intent_id, True, actor.strip(), note.strip(), self.config, now)
        return {"intent_id": intent_id, "status": self.ledger.intent_status(intent_id)}

    def reject(self, intent_id: str, actor: str, note: str, now: datetime) -> dict[str, Any]:
        self.ledger.record_approval(intent_id, False, actor.strip(), note.strip(), self.config, now)
        return {"intent_id": intent_id, "status": self.ledger.intent_status(intent_id)}

    def execute(self, intent_id: str, now: datetime) -> dict[str, Any]:
        intent = self.ledger.get_intent(intent_id)
        existing = self.ledger.order_for_intent(intent_id)
        if existing and existing["status"] in {
            OrderStatus.FILLED.value, OrderStatus.CANCELED.value, OrderStatus.REJECTED.value,
        }:
            return {
                "intent_id": intent_id,
                "order_id": existing["order_id"],
                "status": existing["status"],
                "filled_quantity": existing["filled_quantity"],
            }
        quote = self.broker.latest_quote(intent.symbol)
        snapshot_id = self.ledger.save_quote(quote, now)
        if not quote.snapshot_id:
            quote = MarketQuote(**(quote.to_dict() | {"snapshot_id": snapshot_id}))

        decision = self.risk.evaluate(intent, quote, now)
        self.ledger.save_risk_decision(decision)
        if decision.decision == "REJECTED":
            return {"intent_id": intent_id, "status": OrderStatus.RISK_REJECTED.value,
                    "rejection_reasons": decision.rejection_reasons}
        if not self.ledger.valid_approval(intent_id, self.config, now):
            return {"intent_id": intent_id, "status": OrderStatus.APPROVAL_PENDING.value,
                    "rejection_reasons": ["缺少有效人工批准，或批准时使用的风控配置已变化"]}

        order = self.ledger.create_order(intent, now)
        remaining = order["quantity"] - order["filled_quantity"]
        fill = self.matcher.match(intent, quote, remaining)
        if fill is None:
            return {"intent_id": intent_id, "order_id": order["order_id"],
                    "status": order["status"], "filled_quantity": order["filled_quantity"]}
        updated = self.ledger.apply_fill(self.config, order["order_id"], fill, now)
        return {"intent_id": intent_id, "order_id": updated["order_id"],
                "status": updated["status"], "filled_quantity": updated["filled_quantity"]}
