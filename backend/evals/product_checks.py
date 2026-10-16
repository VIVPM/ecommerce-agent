"""Check a product answer against the catalogue, not against a judge's opinion.

A case's `gold` lists the searchable constraints as one or more groups (several
groups = a "4 Nike and 3 Adidas" request). The answer's products are found by
the pid in their links and every one is looked up in Postgres.
"""
import re

from sqlalchemy import text

_LINK = re.compile(r"\(https?://[^)\s]*[?&]pid=([A-Za-z0-9]+)[^)\s]*\)")
_PRICE = re.compile(r"Rs\.?\s*([\d,]+)")
_RATING = re.compile(r"Rating:\s*([\d.]+)")


def _where(group: dict) -> tuple[str, dict]:
    """The group's constraints as SQL, matched the way the app matches them."""
    clauses, params = ["availability = 'InStock'"], {}
    if brand := group.get("brand"):
        clauses.append("LOWER(brand) LIKE :brand")
        params["brand"] = f"%{brand}%"
    for i, word in enumerate(group.get("words", [])):
        clauses.append(f"LOWER(title) LIKE :w{i}")
        params[f"w{i}"] = f"%{word}%"
    if group.get("gender") == "men":
        clauses.append("LOWER(title) LIKE '%men%' AND LOWER(title) NOT LIKE '%women%'")
    elif group.get("gender") == "women":
        clauses.append("LOWER(title) LIKE '%women%'")
    if "price_max" in group:
        clauses.append("price <= :price_max")
        params["price_max"] = group["price_max"]
    if group.get("rated") or "rating_gt" in group:
        clauses.append("avg_rating IS NOT NULL")
    if "rating_gt" in group:
        clauses.append("avg_rating > :rating_gt")
        params["rating_gt"] = group["rating_gt"]
    if "min_ratings" in group:
        clauses.append("total_ratings >= :min_ratings")
        params["min_ratings"] = group["min_ratings"]
    return " AND ".join(clauses), params


def _shown(answer: str) -> list[dict]:
    """One entry per linked product, with the price and rating written on its line."""
    out = []
    for line in answer.splitlines():
        for pid in _LINK.findall(line):
            price = _PRICE.search(line)
            rating = _RATING.search(line)
            out.append({"pid": pid.upper(),
                        "price": int(price.group(1).replace(",", "")) if price else None,
                        "rating": float(rating.group(1)) if rating else None})
    seen, unique = set(), []
    for item in out:
        if item["pid"] not in seen:
            seen.add(item["pid"])
            unique.append(item)
    return unique


def check(answer: str, gold: dict, engine) -> dict:
    """Return the per-check results and an overall pass flag."""
    shown = _shown(answer)
    if gold.get("expect_none"):
        return {"shown": len(shown), "pass": not shown,
                "problems": [] if not shown else ["listed products for a request the catalogue cannot serve"]}

    groups = gold["groups"]
    problems, per_group = [], [0] * len(groups)
    with engine.connect() as db:
        available = []
        for group in groups:
            where, params = _where(group)
            available.append(db.execute(text(f"SELECT COUNT(DISTINCT pid) FROM product WHERE {where}"),
                                        params).scalar())
        for item in shown:
            row = db.execute(text("SELECT price, avg_rating FROM product WHERE pid = :pid"),
                             {"pid": item["pid"]}).mappings().first()
            if row is None:
                problems.append(f"{item['pid']}: not in catalogue")
                continue
            if item["price"] is not None and item["price"] != row["price"]:
                problems.append(f"{item['pid']}: price says {item['price']}, catalogue {row['price']}")
            if (item["rating"] is not None and row["avg_rating"] is not None
                    and abs(item["rating"] - float(row["avg_rating"])) > 0.05):
                problems.append(f"{item['pid']}: rating says {item['rating']}, catalogue {row['avg_rating']}")
            matched = False
            for i, group in enumerate(groups):
                where, params = _where(group)
                hit = db.execute(text(f"SELECT 1 FROM product WHERE pid = :pid AND {where}"),
                                 {**params, "pid": item["pid"]}).scalar()
                if hit:
                    per_group[i] += 1
                    matched = True
                    break
            if not matched:
                problems.append(f"{item['pid']}: violates the requested constraints")

    for i, group in enumerate(groups):
        if available[i] and not per_group[i]:
            problems.append(f"group {i + 1}: {available[i]} matching products exist, none shown")
        if (want := group.get("count")) and per_group[i] != min(want, available[i]):
            problems.append(f"group {i + 1}: showed {per_group[i]}, expected {min(want, available[i])}")
        if not available[i] and per_group[i]:
            problems.append(f"group {i + 1}: nothing should match, yet products were shown")
    return {"shown": len(shown), "available": available, "per_group": per_group,
            "pass": not problems, "problems": problems}
