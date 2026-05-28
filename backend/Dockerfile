# Backend image — serves the FastAPI app. Single service (no worker queue), so
# one image, one command.
FROM python:3.12-slim AS base

# PYTHONUNBUFFERED so the structured JSON logs reach `docker logs` immediately
# rather than sitting in a pipe buffer (a crashed container otherwise looks
# silent). PYTHONDONTWRITEBYTECODE keeps the layers free of .pyc noise.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUTF8=1

WORKDIR /app

# Requirements first so the expensive install layer stays cached across code
# edits. curl is for the HEALTHCHECK; build-essential is purged in the same
# layer so it never reaches the final image (a few deps still build wheels).
COPY backend/requirements.txt ./requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# Source last (changes constantly). main.py + app/ land at /app, so `main:app`
# imports and the `app.*` package both resolve.
COPY backend/ ./

# Non-root. The app writes nothing to disk (data lives in Neon / Pinecone).
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Hit the real health endpoint, not a TCP probe — uvicorn accepting a socket
# says nothing about whether the app booted (DB config, missing env).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
