"""Discover NEW products from Flipkart search and add them to the catalog.

The companion to refresh_products.py. That script only re-checks links we
already have — it can't find products listed since the last crawl, nor replace
the ones that have gone out of stock. This does the discovery half that the old
Selenium notebook did (search -> paginate -> collect links), but without a
browser: product URLs match a stable pattern (/p/itm<hex>?pid=<PID>), so no
minified CSS class names are involved and there's nothing to rot.

Two-step flow:
    1. python -m app.discover_products --query "running shoes for women" --pages 10
    2. python -m app.refresh_products          # fills in price/rating/availability

Step 2 picks the new rows up first because it orders by `scraped_at NULLS
FIRST`, and new rows are inserted with scraped_at NULL. Until they're enriched
they have no availability, so an in-stock-filtered app query ignores them —
they can't show up as half-populated results.

Dedup is on `pid` (Flipkart's product id), NOT the URL: the same product
appears under different tracking params (lid/srno/otracker) depending on which
search surfaced it, so URL-keyed dedup silently creates duplicates.

Search results carry only name + url (no price/stock), which is why enrichment
is a separate per-product pass.

Be a good citizen: ~1 request per search page, --delay >= 1s, and prefer
Flipkart's affiliate API if you ever need this at volume or commercially.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

from sqlalchemy import text  # noqa: E402
from app.db.database import engine  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
LD_JSON = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.DOTALL | re.I)
PRODUCT_URL = re.compile(r'/[a-z0-9\-\.%]+/p/itm[a-z0-9]+\?pid=[A-Z0-9]+', re.I)
PID = re.compile(r'pid=([A-Z0-9]+)', re.I)


def search_page(query: str, page: int, timeout: int = 30):
    """Return [(product_link, title|None)] found on one search results page."""
    url = ("https://www.flipkart.com/search?q="
           + urllib.parse.quote_plus(query) + f"&page={page}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")

    # Preferred: the ItemList JSON-LD, which gives titles alongside the urls.
    found = {}
    for block in LD_JSON.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict) or item.get("@type") != "ItemList":
                continue
            for entry in item.get("itemListElement") or []:
                u = (entry or {}).get("url")
                if u and PID.search(u):
                    found[u.split("&")[0]] = entry.get("name")

    # Fallback: raw URL pattern (works even if the ItemList markup disappears).
    for path in PRODUCT_URL.findall(html):
        u = "https://www.flipkart.com" + path
        found.setdefault(u, None)

    return list(found.items())


def main():
    # Titles carry characters the Windows cp1252 console can't encode; reconfigure
    # the streams to utf-8 so printing a product name can't crash the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="Discover new Flipkart products into the catalog.")
    ap.add_argument("--query", required=True, help='search term, e.g. "running shoes for women"')
    ap.add_argument("--pages", type=int, default=10, help="search pages to walk (~34 new products each)")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests (be polite)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    print(f'Discovering: "{args.query}" over {args.pages} pages'
          f"{' (DRY RUN)' if args.dry_run else ''}\n")

    seen, inserted, known = {}, 0, 0
    for page in range(1, args.pages + 1):
        try:
            results = search_page(args.query, page)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"  page {page}: FETCH FAILED ({str(e)[:40]})")
            time.sleep(args.delay)
            continue

        new_here = 0
        for link, title in results:
            m = PID.search(link)
            if not m:
                continue
            pid = m.group(1).upper()
            if pid in seen:
                continue
            seen[pid] = (link, title)
            new_here += 1

        print(f"  page {page}: {len(results)} results, {new_here} not seen yet "
              f"(running total {len(seen)})")
        time.sleep(args.delay)

    if not seen:
        print("\nNothing found — Flipkart may have changed search markup or is blocking.")
        return

    if args.dry_run:
        print(f"\nDRY RUN: would consider {len(seen)} unique products.")
        return

    for pid, (link, title) in seen.items():
        with engine.begin() as c:
            res = c.execute(text("""
                INSERT INTO product (pid, product_link, title)
                VALUES (:pid, :link, :title)
                ON CONFLICT (pid) DO NOTHING
            """), {"pid": pid, "link": link, "title": title})
            if res.rowcount:
                inserted += 1
            else:
                known += 1

    print(f"\nDone. unique={len(seen)} new={inserted} already_known={known}")
    if inserted:
        print("Now run:  python -m app.refresh_products    # fills price/rating/availability")


if __name__ == "__main__":
    main()
