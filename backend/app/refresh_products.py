"""Refresh product prices/ratings/availability from Flipkart.

Replaces the old Selenium notebook, which is broken: it extracted via minified
CSS class names (Nx9bqj, VU-ZEz, mEh187...) that Flipkart regenerates on every
frontend deploy — all six selectors are already dead. This reads the
schema.org/Product JSON-LD instead, which exists for Google Shopping and is far
more stable, and it's in the server HTML so no browser/Selenium is needed
(~1s per product with plain urllib).

What it updates: price, avg_rating, total_ratings, availability, scraped_at.
What it does NOT update: `discount` — JSON-LD carries no MRP, so the discount
can't be verified. It's set to NULL rather than left stale next to a fresh
price (the result formatter already omits a falsy discount).

The 'sql' LLM cache needs no purge after a run: it caches the generated QUERY,
not the rows, so refreshed data flows through automatically.

Be a good citizen: this hits a live commercial site. Keep --delay >= 1s, run it
rarely (a nightly/weekly refresh is plenty), and prefer Flipkart's affiliate API
if you ever need this at real volume or commercially.

Usage:
    python -m app.refresh_products --limit 20            # refresh 20 oldest rows
    python -m app.refresh_products --dry-run --limit 5   # look, don't write
    python -m app.refresh_products                       # everything
"""
import argparse
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

from sqlalchemy import text  # noqa: E402
from app.db.database import engine  # noqa: E402
from app.db.models import now_ist  # noqa: E402

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
LD_JSON = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.DOTALL | re.I)


def fetch_product(url: str, timeout: int = 30):
    """Return {price, rating, rating_count, availability} from the page's
    JSON-LD, or None if it can't be parsed."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")

    for block in LD_JSON.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict) or item.get("@type") != "Product":
                continue
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            rating = item.get("aggregateRating") or {}
            price = offers.get("price")
            return {
                "price": int(float(price)) if price is not None else None,
                "rating": float(rating["ratingValue"]) if rating.get("ratingValue") is not None else None,
                "rating_count": int(rating["ratingCount"]) if rating.get("ratingCount") is not None else None,
                "availability": str(offers.get("availability", "")).rsplit("/", 1)[-1] or None,
            }
    return None


def main():
    # Product titles contain characters the Windows cp1252 console can't encode,
    # which crashes on print. Setting PYTHONIOENCODING doesn't fix an already-open
    # stream — reconfigure() is the part that actually works.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="Refresh product data from Flipkart JSON-LD.")
    ap.add_argument("--limit", type=int, default=None, help="max products (oldest first)")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds each worker pauses between requests")
    ap.add_argument("--workers", type=int, default=3, help="concurrent fetches (keep small — be polite)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = ap.parse_args()

    sql = "SELECT product_link, title, price FROM product ORDER BY scraped_at NULLS FIRST"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    with engine.connect() as c:
        rows = c.execute(text(sql)).fetchall()

    print(f"Refreshing {len(rows)} products (delay={args.delay}s"
          f"{', DRY RUN' if args.dry_run else ''})\n")

    # Fetching is I/O-bound (~1.4MB per page), so a few workers cut wall time a
    # lot. Effective rate is roughly workers/(fetch+delay) — keep it modest, this
    # is someone else's infrastructure.
    counts = {"updated": 0, "failed": 0, "changed": 0, "delisted": 0}
    lock = threading.Lock()
    progress = {"n": 0}
    total = len(rows)

    def process(row):
        link, title, old_price = row
        try:
            info = fetch_product(link)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            # Transient: leave scraped_at untouched so the next run retries it.
            with lock:
                counts["failed"] += 1
                progress["n"] += 1
                print(f"  [{progress['n']}/{total}] FETCH FAILED {str(e)[:40]} | {title[:35]}")
            time.sleep(args.delay)
            return

        if not info or info["price"] is None:
            # Flipkart serves an empty ld+json ([]) for delisted products. This is
            # permanent, not transient — so STAMP the row. Otherwise it keeps
            # sorting to the front of the NULLS-FIRST queue and gets retried on
            # every future run, burning ~20% of each refresh on dead listings.
            if not args.dry_run:
                with engine.begin() as c:
                    c.execute(text("""
                        UPDATE product SET availability = 'Unavailable', scraped_at = :now
                         WHERE product_link = :link
                    """), {"now": now_ist(), "link": link})
            with lock:
                counts["delisted"] += 1
                progress["n"] += 1
                print(f"  [{progress['n']}/{total}] DELISTED (no product markup) | {title[:35]}")
            time.sleep(args.delay)
            return

        if not args.dry_run:
            with engine.begin() as c:
                c.execute(text("""
                    UPDATE product
                       SET price = :price,
                           avg_rating = COALESCE(:rating, avg_rating),
                           total_ratings = COALESCE(:rating_count, total_ratings),
                           availability = :availability,
                           discount = NULL,
                           scraped_at = :now
                     WHERE product_link = :link
                """), {**info, "now": now_ist(), "link": link})

        moved = old_price is not None and info["price"] != old_price
        with lock:
            if not args.dry_run:
                counts["updated"] += 1
            progress["n"] += 1
            if moved:
                counts["changed"] += 1
                print(f"  [{progress['n']}/{total}] Rs {old_price} -> Rs {info['price']} "
                      f"({info['price'] - old_price:+d}) {info['availability']} | {title[:35]}")
        time.sleep(args.delay)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(process, rows))

    updated, failed, changed, delisted = (
        counts["updated"], counts["failed"], counts["changed"], counts["delisted"])

    print(f"\nDone. checked={len(rows)} updated={updated} price_changed={changed} "
          f"delisted={delisted} failed={failed}")
    if failed and failed == len(rows):
        print("Every product failed to FETCH — Flipkart is likely blocking or offline. "
              "Check fetch_product() against a live page before trusting a scheduled run.")
        sys.exit(1)


if __name__ == "__main__":
    main()
