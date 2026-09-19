from __future__ import annotations

import argparse
import json
import secrets
import sys
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


SITE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SITE_ROOT.parent
DIST_DIR = SITE_ROOT / "dist"
CONFIG_PATH = PROJECT_ROOT / "config" / "paper_trading.json"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "paper_trading.example.json"
RUNTIME_DIR = PROJECT_ROOT / "runtime" / "paper_trading"
DB_PATH = RUNTIME_DIR / "paper_trading.sqlite3"
sys.path.insert(0, str(PROJECT_ROOT))

from paper_trading.config import PaperConfig, load_config  # noqa: E402
from paper_trading.ledger import PaperLedger  # noqa: E402
from paper_trading.market_data import TencentPaperQuoteBroker  # noqa: E402
from paper_trading.models import OrderSide, SignalIntent  # noqa: E402
from paper_trading.order_service import OrderService  # noqa: E402
from paper_trading.reconciliation import reconcile  # noqa: E402
from paper_trading.report_adapter import paper_account_summary  # noqa: E402


def now_local() -> datetime:
    return datetime.now().astimezone()


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, Path)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dashboard_state(csrf_token: str) -> dict[str, Any]:
    base = {
        "csrfToken": csrf_token,
        "serverTime": now_local().isoformat(),
        "paperOnly": True,
        "killSwitch": (RUNTIME_DIR / "kill_switch").exists(),
    }
    if not CONFIG_PATH.exists():
        return base | {
            "setupRequired": True,
            "setupDefaults": read_json(EXAMPLE_CONFIG_PATH),
        }

    config = load_config(CONFIG_PATH)
    ledger = PaperLedger(DB_PATH)
    ledger.initialize_account(config)
    account = paper_account_summary(ledger, config.account_id)
    raw_account = ledger.account(config.account_id)
    with ledger.connection() as con:
        pending_rows = con.execute(
            """
            SELECT i.intent_id,i.status,i.payload_json,i.updated_at,r.checks_json
            FROM intents i
            LEFT JOIN risk_decisions r ON r.id=(
                SELECT MAX(r2.id) FROM risk_decisions r2 WHERE r2.intent_id=i.intent_id
            )
            WHERE i.status IN ('APPROVAL_PENDING','APPROVED','SUBMITTED','PARTIALLY_FILLED')
            ORDER BY i.updated_at DESC LIMIT 30
            """
        ).fetchall()
        order_rows = con.execute(
            "SELECT * FROM orders ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        fill_rows = con.execute(
            """
            SELECT f.*,o.symbol,o.side FROM fills f
            JOIN orders o ON o.order_id=f.order_id
            ORDER BY f.created_at DESC LIMIT 50
            """
        ).fetchall()
        latest_quote = con.execute(
            "SELECT symbol,timestamp,last,bid,ask FROM market_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

    pending: list[dict[str, Any]] = []
    for row in pending_rows:
        payload = json.loads(row["payload_json"])
        checks = json.loads(row["checks_json"] or "[]")
        pending.append({
            **payload,
            "status": row["status"],
            "updated_at": row["updated_at"],
            "risk_checks": checks,
        })

    return base | {
        "setupRequired": False,
        "account": account | {"initial_cash": str(raw_account["initial_cash"])},
        "config": config.to_dict() | {"fingerprint": config.fingerprint},
        "pending": pending,
        "orders": [dict(row) for row in order_rows],
        "fills": [dict(row) for row in fill_rows],
        "latestQuote": None if latest_quote is None else dict(latest_quote),
        "reconciliation": reconcile(ledger, config.account_id),
    }


def setup_account(payload: dict[str, Any]) -> dict[str, Any]:
    if CONFIG_PATH.exists():
        raise ValueError("模拟账户已经初始化，不能从看板覆盖现有配置")
    expected = read_json(EXAMPLE_CONFIG_PATH)
    allowed = set(PaperConfig.__dataclass_fields__)
    candidate = {key: payload.get(key, expected.get(key)) for key in allowed}
    candidate["paper_mode"] = True
    candidate["adapter"] = "deterministic"
    config = PaperConfig(**candidate)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CONFIG_PATH)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    ledger = PaperLedger(DB_PATH)
    ledger.initialize_account(config)
    return {"status": "INITIALIZED", "account_id": config.account_id}


def service() -> tuple[PaperConfig, PaperLedger, OrderService]:
    if not CONFIG_PATH.exists():
        raise ValueError("请先初始化模拟账户")
    config = load_config(CONFIG_PATH)
    ledger = PaperLedger(DB_PATH)
    ledger.initialize_account(config)
    broker = TencentPaperQuoteBroker(failure_threshold=config.quote_failover_failures)
    return config, ledger, OrderService(config, ledger, broker, RUNTIME_DIR)


class DashboardHandler(SimpleHTTPRequestHandler):
    csrf_token = ""
    allowed_origins: set[str] = set()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(DIST_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def send_json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(json_safe(value), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            try:
                self.send_json(dashboard_state(self.csrf_token))
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/quote":
            try:
                symbol = parse_qs(parsed.query).get("symbol", [""])[0]
                threshold = load_config(CONFIG_PATH).quote_failover_failures if CONFIG_PATH.exists() else 3
                quote = TencentPaperQuoteBroker(failure_threshold=threshold).latest_quote(symbol)
                self.send_json(quote.to_dict())
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def _authorized_post(self) -> bool:
        origin = self.headers.get("Origin")
        token = self.headers.get("X-Paper-CSRF")
        content_type = self.headers.get("Content-Type", "")
        return (
            token == self.csrf_token
            and (not origin or origin in self.allowed_origins)
            and content_type.startswith("application/json")
        )

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 100_000:
            raise ValueError("请求内容为空或过大")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self) -> None:
        if not self._authorized_post():
            self.send_json({"error": "请求未通过本地安全校验"}, HTTPStatus.FORBIDDEN)
            return
        try:
            payload = self._body()
            if self.path == "/api/setup":
                result = setup_account(payload)
            elif self.path == "/api/propose":
                config, ledger, orders = service()
                when = now_local()
                symbol = str(payload["symbol"]).upper()
                side = OrderSide(str(payload["side"]).upper())
                token = uuid.uuid4().hex[:10]
                intent = SignalIntent(
                    intent_id=f"{when.strftime('%Y%m%d%H%M%S')}-{symbol}-{side.value.lower()}-{token}",
                    created_at=when,
                    strategy_id="dashboard-manual-v1",
                    symbol=symbol,
                    side=side,
                    quantity=int(payload["quantity"]),
                    limit_price=str(payload["limit_price"]),
                    valid_until=when + timedelta(minutes=config.intent_ttl_minutes),
                    reason_codes=(str(payload.get("reason") or "dashboard_manual"),),
                    evidence_snapshot_id=f"dashboard-{token}",
                    requires_user_approval=True,
                )
                quote = orders.broker.latest_quote(symbol)
                result = orders.propose(intent, quote, when)
            elif self.path == "/api/action":
                config, ledger, orders = service()
                action = str(payload["action"])
                intent_id = str(payload.get("intent_id") or "")
                when = now_local()
                if action == "approve":
                    result = orders.approve(intent_id, "dashboard-user", str(payload.get("note") or "网页批准"), when)
                elif action == "reject":
                    result = orders.reject(intent_id, "dashboard-user", str(payload.get("note") or "网页拒绝"), when)
                elif action == "execute":
                    result = orders.execute(intent_id, when)
                elif action == "kill":
                    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                    (RUNTIME_DIR / "kill_switch").write_text(when.isoformat(), encoding="utf-8")
                    result = {"status": "KILL_SWITCH_ENABLED"}
                elif action == "resume":
                    (RUNTIME_DIR / "kill_switch").unlink(missing_ok=True)
                    result = {"status": "KILL_SWITCH_DISABLED"}
                else:
                    raise ValueError("未知操作")
            else:
                self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json(result)
        except (KeyError, TypeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> int:
    global CONFIG_PATH, RUNTIME_DIR, DB_PATH
    parser = argparse.ArgumentParser(description="Local A-share paper trading dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config")
    parser.add_argument("--runtime-dir")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("dashboard is local-only and must bind to loopback")
    if args.config:
        CONFIG_PATH = Path(args.config).resolve()
    if args.runtime_dir:
        RUNTIME_DIR = Path(args.runtime_dir).resolve()
        DB_PATH = RUNTIME_DIR / "paper_trading.sqlite3"
    token = secrets.token_urlsafe(32)
    DashboardHandler.csrf_token = token
    DashboardHandler.allowed_origins = {
        f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}"
    }
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"PAPER_DASHBOARD=http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
