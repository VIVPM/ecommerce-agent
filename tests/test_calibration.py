"""Free contract tests: calibration waits for completion without duplicate work."""
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import load_test


def response(payload):
    return Mock(json=Mock(return_value=payload))


class CalibrationTests(unittest.TestCase):
    @patch.object(load_test.time, "sleep")
    @patch.object(load_test.httpx, "get")
    @patch.object(load_test.httpx, "post")
    def test_waits_for_terminal_job(self, post, get, sleep):
        post.return_value = response({"job_id": "j1"})
        get.side_effect = [response({"status": state})
                           for state in ("queued", "running", "succeeded")]
        job_id, elapsed = load_test._calibration_message("http://test", {}, "c1")
        self.assertEqual(job_id, "j1")
        self.assertGreaterEqual(elapsed, 0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch.object(load_test.httpx, "get")
    @patch.object(load_test.httpx, "post")
    def test_failed_job_is_not_counted_or_retried(self, post, get):
        post.return_value = response({"job_id": "j2"})
        get.return_value = response({"status": "failed", "error": "provider failure"})
        with self.assertRaisesRegex(RuntimeError, "provider failure"):
            load_test._calibration_message("http://test", {}, "c1")
        self.assertEqual(post.call_count, 1)

    @patch.object(load_test.time, "monotonic", side_effect=[0, 0, 121])
    @patch.object(load_test.httpx, "get")
    @patch.object(load_test.httpx, "post")
    def test_timeout_requests_cancellation_not_resubmission(self, post, get, clock):
        post.return_value = response({"job_id": "j3"})
        get.return_value = response({"status": "running"})
        with self.assertRaisesRegex(TimeoutError, "cancellation requested"):
            load_test._calibration_message("http://test", {}, "c1")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.args[0], "http://test/api/jobs/j3/cancel")


if __name__ == "__main__":
    unittest.main()
