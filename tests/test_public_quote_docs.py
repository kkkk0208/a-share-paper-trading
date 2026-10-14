"""Guard the public quote documents against machine-specific runtime details."""

from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOCAL_NOTES = "runtime/paper_trading/LOCAL_OPERATIONS.md"


class PublicQuoteDocumentationTests(unittest.TestCase):
    def test_public_docs_reference_local_notes_without_machine_details(self):
        patterns = (
            r"127\.0\.0\.1:\d+",
            r"[A-Za-z]:[\\/]Users[\\/]",
            r"Windows Time[^\n]*(?:未启动|已停止|手动)",
            r"(?:交流|直流|电池)[^\n]*(?:为.{0,8}从不|\d+\s*分钟)",
            r"NTP[^\n]*\d+\.\d+",
        )
        for path in (ROOT / "docs" / "quote-collector.md", ROOT / "PROJECT_STATUS.md"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertIn(LOCAL_NOTES, text, "missing reference to ignored local operation notes")
                for pattern in patterns:
                    self.assertIsNone(re.search(pattern, text), f"machine detail matched {pattern}")

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "Git checkout required")
    def test_local_operation_notes_cannot_enter_normal_git_staging(self):
        ignored = subprocess.run(["git", "check-ignore", "--quiet", LOCAL_NOTES], cwd=ROOT, timeout=10)
        self.assertEqual(ignored.returncode, 0)
        tracked = subprocess.run(["git", "ls-files", "--", LOCAL_NOTES], cwd=ROOT, capture_output=True,
                                 text=True, timeout=10)
        self.assertEqual(tracked.returncode, 0)
        self.assertEqual(tracked.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
