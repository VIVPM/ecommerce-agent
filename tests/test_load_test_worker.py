"""Offline worker claim report and queue-safety checks; never starts a worker."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(ROOT))
import load_test  # noqa: E402
import load_test_worker as worker  # noqa: E402


class WorkerReportTest(unittest.TestCase):
    def test_claims_and_empty_log(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "worker.log"
            path.write_text("LT_CLAIM 101.000 8.0\nLT_CLAIM 102.000 12.0\n", encoding="utf-8")
            stats = worker.claim_stats([path], 100.0)
            self.assertEqual(stats["claims_won"], 2)
            self.assertEqual(stats["worker_boot_s"], [1.0])
            self.assertEqual(stats["claim_ms"]["p95"], 12)
            path.write_text("", encoding="utf-8")
            self.assertEqual(worker.claim_stats([path], 100.0)["claims_won"], 0)

    def test_remote_load_target_rejected(self):
        with self.assertRaises(SystemExit):
            load_test.local_base("https://example.com")
        self.assertEqual(load_test.local_base("http://127.0.0.1:8021/"),
                         "http://127.0.0.1:8021")

    def test_unowned_account_not_deleted(self):
        with patch.object(load_test, "_owned_user", False):
            load_test.cleanup()


if __name__ == "__main__":
    unittest.main()
