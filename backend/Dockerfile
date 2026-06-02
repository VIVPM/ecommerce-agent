# Backend image: FastAPI served by uvicorn.
FROM python:3.12-slim AS base

# Unbuffered so JSON logs reach `docker logs` immediately, not a pipe buffer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUTF8=1

WORKDIR /app

# Requirements first so the install layer survives code edits. build-essential is
# purged in the same layer so it never reaches the final image.
COPY backend/requirements.txt ./requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# Source last. main.py and app/ land at /app, so `main:app` and `app.*` resolve.
COPY backend/ ./

# Non-root; the app writes nothing to disk.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Real health endpoint, not a TCP probe: an open socket doesn't mean it booted.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
