"""Self-contained checks for backend/app/logging_setup.py — the structured JSON
logging + request_id correlation. No network, no DB, no API keys. Run:

    python tests/test_logging.py
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.logging_setup import configure_logging, request_context, _JsonFormatter, _CorrelationFilter


def _emit(logger_name="t"):
    """Format one record the way the configured handler would, and return the JSON."""
    rec = logging.LogRecord(logger_name, logging.INFO, __file__, 1, "hello", None, None)
    _CorrelationFilter().filter(rec)
    return json.loads(_JsonFormatter().format(rec))


# JSON shape: always ts/level/logger/message.
out = _emit()
assert out["level"] == "INFO" and out["message"] == "hello" and out["logger"] == "t", out
assert "request_id" not in out, "request_id should be absent outside a request context"

# request_id appears only inside request_context, and unbinds after.
with request_context("abc123"):
    inside = _emit()
    assert inside["request_id"] == "abc123", inside
after = _emit()
assert "request_id" not in after, "request_id leaked out of its context"

# Nested contexts restore the previous id on exit.
with request_context("outer"):
    with request_context("inner"):
        assert _emit()["request_id"] == "inner"
    assert _emit()["request_id"] == "outer", "inner context did not restore outer"

# configure_logging is idempotent — a second call must not stack handlers.
configure_logging()
n1 = len(logging.getLogger().handlers)
configure_logging()
n2 = len(logging.getLogger().handlers)
assert n1 == n2 == 1, f"configure_logging not idempotent: {n1} -> {n2} handlers"

# exceptions are serialized, not dropped.
try:
    raise ValueError("boom")
except ValueError:
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    _CorrelationFilter().filter(rec)
    err = json.loads(_JsonFormatter().format(rec))
    assert "boom" in err.get("exc_info", ""), err

print("test_logging.py: all checks passed")
