from __future__ import annotations

import unittest
from email.message import Message
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from tempfile import TemporaryDirectory

from paper_dashboard import server
from paper_trading.models import OrderSide, SignalIntent


class PaperDashboardTests(unittest.TestCase):
    def test_approval_requires_code_or_quantity_confirmation(self):
        now = datetime(2026, 9, 21, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        intent = SignalIntent(
            "intent", now, "manual", "002195.SZ", OrderSide.BUY, 300, "6.10",
            now + timedelta(minutes=30), ("test",), "evidence",
        )
        self.assertTrue(server.approval_confirmation_valid(intent, "2195"))
        self.assertTrue(server.approval_confirmation_valid(intent, "300"))
        self.assertFalse(server.approval_confirmation_valid(intent, "195"))

    def test_post_requires_access_cookie_csrf_and_origin(self):
        handler = object.__new__(server.DashboardHandler)
        handler.access_token = "access"
        handler.csrf_token = "csrf"
        handler.allowed_origins = {"http://127.0.0.1:8765"}
        headers = Message()
        headers["Cookie"] = "PaperDashboardToken=access"
        headers["X-Paper-CSRF"] = "csrf"
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "http://127.0.0.1:8765"
        handler.headers = headers
        self.assertTrue(handler._authorized_post())
        del headers["Origin"]
        self.assertFalse(handler._authorized_post())
    def test_setup_uses_paper_only_config_and_initializes_account(self):
        original = (server.CONFIG_PATH, server.RUNTIME_DIR, server.DB_PATH)
        try:
            with TemporaryDirectory() as temp:
                root = Path(temp)
                server.CONFIG_PATH = root / "config" / "paper_trading.json"
                server.RUNTIME_DIR = root / "runtime"
                server.DB_PATH = server.RUNTIME_DIR / "paper.sqlite3"
                payload = server.read_json(server.EXAMPLE_CONFIG_PATH)
                result = server.setup_account(payload)
                self.assertEqual(result["status"], "INITIALIZED")
                state = server.dashboard_state("csrf-test")
                self.assertFalse(state["setupRequired"])
                self.assertTrue(state["paperOnly"])
                self.assertEqual(state["account"]["equity"], "100000.00")
                self.assertEqual(state["orders"], [])
                self.assertEqual(state["reconciliation"], [])
                with self.assertRaisesRegex(ValueError, "不能从看板覆盖"):
                    server.setup_account(payload)
        finally:
            server.CONFIG_PATH, server.RUNTIME_DIR, server.DB_PATH = original


if __name__ == "__main__":
    unittest.main()
