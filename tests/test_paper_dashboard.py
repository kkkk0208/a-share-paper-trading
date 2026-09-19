from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paper_dashboard import server


class PaperDashboardTests(unittest.TestCase):
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
