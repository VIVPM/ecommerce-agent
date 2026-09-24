"""Dead-end audit: which searches CAN'T be answered because nothing is buyable."""
import argparse
import os
import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(os.path.join(os.path.dirname(__file__), "app", ".env"))

from sqlalchemy import text                        # noqa: E402

from app.db.database import engine                 # noqa: E402

PRICE_CEILINGS = (1000, 2000, 3000, 5000)
RATING_FLOORS = (4.0, 4.5)
MIN_PRODUCTS = 3


def _rows_for(conn, dimension: str):
    """(label, threshold, matches, buyable) per brand for one dimension."""
    if dimension == "price":
        cases = ", ".join(
            f"count(*) FILTER (WHERE price < {c}) AS t{c}, "
            f"count(*) FILTER (WHERE price < {c} AND availability = 'InStock') AS s{c}"
            for c in PRICE_CEILINGS)
        thresholds = PRICE_CEILINGS
        fmt = "under {}"
    else:
        cases = ", ".join(
            f"count(*) FILTER (WHERE avg_rating >= {f}) AS t{int(f * 10)}, "
            f"count(*) FILTER (WHERE avg_rating >= {f} AND availability = 'InStock')"
            f" AS s{int(f * 10)}"
            for f in RATING_FLOORS)
        thresholds = RATING_FLOORS
        fmt = "rated {}+"

    sql = f"""
        WITH names AS (
            SELECT LOWER(brand) AS brand
              FROM product
             WHERE brand IS NOT NULL AND price IS NOT NULL
             GROUP BY LOWER(brand)
            HAVING count(*) >= {MIN_PRODUCTS}
        )
        SELECT n.brand, count(*) AS n, {cases}
          FROM names n
          JOIN product ON LOWER(product.brand) LIKE '%' || n.brand || '%'
         WHERE product.price IS NOT NULL
         GROUP BY n.brand
    """
    out = []
    for row in conn.execute(text(sql)):
        m = row._mapping
        for t in thresholds:
            key = t if dimension == "price" else int(t * 10)
            out.append((m["brand"], fmt.format(t), m[f"t{key}"], m[f"s{key}"]))
    return out


def summarise(rows):
    """(dead_ends, live, stranded) -- pure, so it is testable without a database."""
    live = [r for r in rows if r[2] > 0]
    dead = [r for r in live if r[3] == 0]
    stranded = sum(r[2] for r in dead)
    return dead, live, stranded


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--top", type=int, default=0,
                    help="limit to the N largest brands by catalogue size")
    ap.add_argument("--max-rate", type=float,
                    help="exit 1 if the dead-end rate exceeds this percentage")
    args = ap.parse_args()

    with engine.connect() as conn:
        total, in_stock = conn.execute(text(
            "SELECT count(*), count(*) FILTER (WHERE availability = 'InStock') "
            "FROM product")).one()
        if args.top:
            keep = {b for (b,) in conn.execute(text(
                "SELECT LOWER(brand) FROM product WHERE brand IS NOT NULL "
                "GROUP BY LOWER(brand) ORDER BY count(*) DESC LIMIT :n"),
                {"n": args.top})}
        else:
            keep = None
        rows = _rows_for(conn, "price") + _rows_for(conn, "rating")

    if keep is not None:
        rows = [r for r in rows if r[0] in keep]

    dead, live, stranded = summarise(rows)
    rate = 100 * len(dead) / len(live) if live else 0.0

    gone = {b for b in {r[0] for r in dead}
            if all(r[3] == 0 for r in live if r[0] == b)}
    partial = [r for r in dead if r[0] not in gone]

    print(f"\nCatalogue: {total} products, {in_stock} buyable "
          f"({100 * in_stock // total}%), {total - in_stock} not in stock\n")
    print(f"Answerable filter combinations : {len(live)}")
    print(f"Dead ends (match, none buyable): {len(dead)}  -> {rate:.1f}%")
    print(f"Products stranded behind them  : {stranded}\n")

    if gone:
        sizes = {b: max(r[2] for r in live if r[0] == b) for b in gone}
        print(f"Brands with NOTHING buyable ({len(gone)}):")
        for brand in sorted(gone, key=lambda b: -sizes[b])[:10]:
            print(f"  {brand[:24]:<24} {sizes[brand]:>3} products, all unavailable")
        if len(gone) > 10:
            print(f"  ... and {len(gone) - 10} more")
        print()

    if partial:
        print("Filters that strand products in a brand we DO otherwise stock:")
        print(f"  {'BRAND':<22} {'FILTER':<14} {'MATCHES':>8}  (none buyable)")
        print("  " + "-" * 58)
        for brand, label, matches, _ in sorted(partial, key=lambda r: -r[2])[:15]:
            print(f"  {brand[:22]:<22} {label:<14} {matches:>8}")
        if len(partial) > 15:
            print(f"  ... and {len(partial) - 15} more")
        print()

    if args.max_rate is not None and rate > args.max_rate:
        print(f"FAIL: dead-end rate {rate:.1f}% exceeds {args.max_rate}%")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
