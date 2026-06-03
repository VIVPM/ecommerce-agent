-- Indexes for the `product` table to speed up the LLM-generated SQL filters.
-- Safe + idempotent (IF NOT EXISTS). Run against the read-WRITE DB, not the
-- read-only engine used at query time.
--
-- Run:  python -c "from sqlalchemy import text; from app.db.database import engine; \
--        [engine.begin().__enter__().execute(text(s)) for s in open('migrations.sql').read().split(';') if s.strip()]"
--   (or just paste into the Neon SQL editor.)

-- Range / equality filters ("price under 2000", "rating above 4"): plain b-tree.
CREATE INDEX IF NOT EXISTS idx_product_price ON product (price);
CREATE INDEX IF NOT EXISTS idx_product_avg_rating ON product (avg_rating);

-- Case-insensitive substring search on brand/title (LOWER(col) LIKE '%nike%').
-- A leading-wildcard LIKE can't use a b-tree, so use a trigram GIN index.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_product_brand_lower_trgm ON product USING gin (LOWER(brand) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_product_title_lower_trgm ON product USING gin (LOWER(title) gin_trgm_ops);

-- Data-freshness tracking. Prices/ratings drift, and until now there was no way
-- to know how old a row was — see refresh_products.py.
ALTER TABLE product ADD COLUMN IF NOT EXISTS scraped_at TIMESTAMPTZ;
ALTER TABLE product ADD COLUMN IF NOT EXISTS availability TEXT;

-- Backfill the original scrape date. This is an ESTIMATE: the mtime of the
-- scrape artifact web-scrapping/flipkart_product_data.csv (2026-03-03 12:15).
-- Only fills rows that have never been refreshed.
UPDATE product SET scraped_at = TIMESTAMPTZ '2026-03-03 12:15:00+05:30' WHERE scraped_at IS NULL;

-- `pid` (Flipkart's product id) is the real identity of a product. The URL is
-- NOT: the same product appears under different tracking params (lid/srno/
-- otracker) depending on which search surfaced it, so URL-keyed dedup silently
-- creates duplicates — there were already 18 in the table.
ALTER TABLE product ADD COLUMN IF NOT EXISTS pid TEXT;
UPDATE product SET pid = substring(product_link from 'pid=([A-Z0-9]+)') WHERE pid IS NULL;

-- Drop duplicate pids, keeping the most recently refreshed row of each.
DELETE FROM product WHERE ctid IN (
    SELECT ctid FROM (
        SELECT ctid, row_number() OVER (
            PARTITION BY pid ORDER BY scraped_at DESC NULLS LAST, ctid
        ) AS rn
        FROM product WHERE pid IS NOT NULL
    ) t WHERE rn > 1
);

-- Required for discover_products.py's ON CONFLICT (pid) upsert.
CREATE UNIQUE INDEX IF NOT EXISTS uq_product_pid ON product (pid);

-- Drop `discount`. The source (schema.org JSON-LD) carries no MRP, so a discount
-- can never be verified — it was NULL for 94% of rows and the agent could still
-- see it in the schema and try to filter on it, producing confidently empty
-- results. Better to not have the field than to have one that is always wrong.
ALTER TABLE product DROP COLUMN IF EXISTS discount;

-- Drop `index`. It is the pandas DataFrame row number from the original CSV load
-- (a to_sql() call that didn't pass index=False) — it carries no product meaning,
-- is NULL for every discovered product, and nothing reads it. Dropped so nobody
-- later mistakes it for an identifier; `pid` is the real product key.
ALTER TABLE product DROP COLUMN IF EXISTS "index";

-- v0 job queue: the agent runs in a worker, not inside the HTTP request.
-- SQLAlchemy's create_all() makes these at boot; kept here for prod/manual runs.
CREATE TABLE IF NOT EXISTS jobs (
    id               VARCHAR PRIMARY KEY,
    user_id          INTEGER,
    chat_id          VARCHAR,
    status           VARCHAR DEFAULT 'queued',
    query            TEXT,
    history          TEXT,
    tool             VARCHAR,
    result           TEXT,
    error            TEXT,
    cancel_requested BOOLEAN DEFAULT false,
    attempts         INTEGER DEFAULT 0,
    emitted          BOOLEAN DEFAULT false,
    lease_until      TIMESTAMPTZ,
    worker_id        VARCHAR,
    created_at       TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_jobs_claim       ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_user_status ON jobs (user_id, status);

CREATE TABLE IF NOT EXISTS job_events (
    id         BIGSERIAL PRIMARY KEY,
    job_id     VARCHAR,
    seq        INTEGER,
    type       VARCHAR,
    data       TEXT,
    created_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_job_events_job_seq ON job_events (job_id, seq);

-- v1: idempotency + per-job token accounting.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS input_tokens  INTEGER DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS output_tokens INTEGER DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cached_tokens INTEGER DEFAULT 0;
-- One key per user: a retried submit finds the original job instead of starting
-- a second one. Partial, so the many NULL keys don't collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_user_idem ON jobs (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
-- The daily token budget sums this window.
CREATE INDEX IF NOT EXISTS ix_jobs_user_created ON jobs (user_id, created_at);

-- v2: TTFT and the provider that served each job (failover makes it vary).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ttft_ms  INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS provider VARCHAR;
