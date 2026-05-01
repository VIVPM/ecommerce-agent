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
