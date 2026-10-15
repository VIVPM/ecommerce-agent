"""Small offline contract for k6 report parsing and job timing."""
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("load_test_k6", ROOT / "load_test_k6.py")
k6 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(k6)


class FakeEngine:
    def __init__(self, rows):
        self.rows = rows

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def execute(self, _query, params):
        assert params["ids"] == [7]
        return self

    def mappings(self):
        return self

    def all(self):
        return self.rows


class K6ReportTest(unittest.TestCase):
    def test_queue_timing_and_endpoint_errors(self):
        t = datetime(2026, 10, 14, tzinfo=timezone.utc)
        rows = [{"status": "succeeded", "created_at": t,
                 "started_at": t + timedelta(seconds=2),
                 "finished_at": t + timedelta(seconds=5)}]
        with patch.object(k6, "engine", FakeEngine(rows)):
            jobs = k6.job_stats([7], t)
        self.assertEqual((jobs["submitted"], jobs["succeeded"], jobs["unfinished"]), (1, 1, 0))
        self.assertEqual(jobs["queue_wait_s"]["p95"], 2)
        self.assertEqual(jobs["run_s"]["p95"], 3)

        api = k6.api_stats({"metrics": {
            "api_chats": {"values": {"count": 2, "p(50)": 1, "p(95)": 2, "p(99)": 3}},
            "err_chats": {"values": {"rate": 0.5}},
            "http_req_failed": {"values": {"rate": 0.25}},
        }})
        self.assertEqual(api["endpoints"]["chats"]["error_pct"], 50)
        self.assertEqual(api["error_pct"], 25)


if __name__ == "__main__":
    unittest.main()
