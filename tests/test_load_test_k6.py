"""Small offline contract for k6 report parsing and job timing."""
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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

    def test_spike_phases_and_missing_submissions_fail(self):
        stages, phases = k6.spike_plan(10, 50)
        self.assertEqual((stages[2]["target"], stages[-1]["target"]), (50, 10))
        metrics = {"http_req_failed": {"values": {"rate": 0}}}
        for phase in phases:
            metrics[f"phase_{phase['name']}_chats"] = {
                "values": {"count": 2, "p(50)": 1, "p(95)": 2, "p(99)": 3}}
            metrics[f"phase_err_{phase['name']}_chats"] = {"values": {"rate": 0.25}}
        self.assertEqual(k6.phase_stats({"metrics": metrics}, phases)["after"]["chats"]["error_pct"], 25)
        args = SimpleNamespace(port=8021, ramp_up=1, hold=1, jobs_per_user=1, drain_timeout=1)
        accounts = [{"id": 7}, {"id": 8}]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            def fake_run(_cmd, env, check):
                Path(env["SUMMARY"]).write_text(json.dumps({"metrics": {
                    **metrics, "jobs_submitted": {"values": {"count": 1}}
                }}), encoding="utf-8")
                return SimpleNamespace(returncode=0)
            jobs = {"submitted": 1, "failed": 0, "unfinished": 0,
                    "queue_wait_s": {"p95": 1}}
            with patch.object(k6, "OUT", folder), patch.object(k6.subprocess, "run", fake_run), \
                 patch.object(k6, "job_stats", return_value=jobs):
                row = k6.run_level("k6", args, accounts, folder / "accounts.json", 2,
                                   stages, phases)
        self.assertFalse(row["passed"])
        self.assertEqual((row["api"]["jobs_submitted"], row["expected_jobs"]), (1, 2))

    def test_cleanup_rejects_non_test_tags(self):
        for tag in ("", "k6-xyz", "k6-12345678%", "loadtest_12345678"):
            with self.assertRaises(ValueError):
                k6.cleanup_by_tag(tag)


if __name__ == "__main__":
    unittest.main()
