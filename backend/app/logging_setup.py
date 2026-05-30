"""Structured JSON logging correlated by request_id.

Call configure_logging() once at startup. Wrap request handling in
request_context(request_id) so every log line emitted from any module while
inside the block carries that id — `grep request_id=<x>` (or a Loki filter)
then shows the whole story of one request, and the id also goes back to the
caller as the X-Request-ID response header for support.

Mirrors the leads-coordinator's logging_setup.py, trimmed to the one id this
app has (no job/lead ids).
"""
import contextvars
import json
import logging
from contextlib import contextmanager
from typing import Optional

_request_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "request_id", default=None
)


class _CorrelationFilter(logging.Filter):
    """Attach the active request_id (if any) to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent. Replaces the root handler with a JSON one that carries request_id."""
    root = logging.getLogger()
    if getattr(root, "_correlation_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_CorrelationFilter())
    root.handlers = [handler]
    root.setLevel(level)
    root._correlation_configured = True


@contextmanager
def request_context(request_id):
    token = _request_id_var.set(str(request_id) if request_id is not None else None)
    try:
        yield
    finally:
        _request_id_var.reset(token)
