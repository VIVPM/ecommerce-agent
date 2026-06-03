import logging
import random
import time

logger = logging.getLogger(__name__)

# Substrings that mark a transient (retryable) Gemini/network failure. An invalid
# API key or a bad request is NOT in here, so those fail fast instead of retrying.
_TRANSIENT = ("503", "502", "500", "429", "unavailable", "deadline",
              "timeout", "timed out", "overloaded", "internal error")


def is_transient(err) -> bool:
    s = str(err).lower()
    return any(t in s for t in _TRANSIENT)


def with_retry(fn, *args, attempts: int = 3, base_delay: float = 0.6, **kwargs):
    """Call fn with exponential-backoff retry on transient errors only.
    Non-transient errors (invalid key, bad request) raise on the first try.
    # ponytail: hand-rolled retry, ~15 lines; swap for tenacity if we need jitter/circuit-breaker."""
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            if not is_transient(e) or i == attempts - 1:
                raise
            # Full jitter. Without it every worker that hit the same provider
            # blip retries on the same schedule and re-creates the spike it is
            # backing off from.
            delay = random.uniform(0, base_delay * (2 ** i))
            logger.warning("Transient LLM error (attempt %d/%d), retrying in %.1fs: %s",
                           i + 1, attempts, delay, e)
            time.sleep(delay)
    raise last if last else RuntimeError("with_retry exhausted without an error")
