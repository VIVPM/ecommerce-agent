"""Freeze the hand-labelled pairs into a permanent test set for the JUDGE."""
import csv
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "judge_set.json")

SOURCES = [
    ("esci_audit1.csv", False),
    ("esci_sample_labeled.csv", True),
]


def _shapes():
    """Audit 1's sheet had no shape column; the query set is the source of truth."""
    sys.path.insert(0, HERE)
    from evaluate_ranking import QUERIES
    return {q: shape for shape, q in QUERIES}


def load(path, saw_reviews):
    full = os.path.join(HERE, path)
    if not os.path.exists(full):
        print(f"  skipped (missing): {path}")
        return []
    with open(full, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        human = (r.get("your_label_E_S_C_I") or "").strip().upper()[:1]
        if human not in "ESCI":
            continue
        out.append({
            "query": r["query"],
            "shape": r.get("shape") or _SHAPES.get(r["query"], ""),
            "title": r["product"],
            "brand": r.get("brand", ""),
            "price": r.get("price", ""),
            "rating": r.get("rating", ""),
            "reviews": r.get("reviews", "") if saw_reviews else "",
            "saw_reviews": saw_reviews,
            "human": human,
            "source": path,
        })
    print(f"  {path}: {len(out)} labelled pairs (reviews shown: {saw_reviews})")
    return out


_SHAPES = {}


def main():
    global _SHAPES
    _SHAPES = _shapes()
    pairs = []
    for path, saw in SOURCES:
        pairs.extend(load(path, saw))
    if not pairs:
        print("No labelled audit files found; nothing to freeze.")
        return 1

    seen = {}
    for p in pairs:
        seen[(p["query"], p["title"])] = p
    frozen = list(seen.values())

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(frozen, f, indent=1, ensure_ascii=False)

    spread = {x: sum(p["human"] == x for p in frozen) for x in "ESCI"}
    shapes = {}
    for p in frozen:
        shapes[p["shape"]] = shapes.get(p["shape"], 0) + 1
    print(f"\nFroze {len(frozen)} unique human-labelled pairs -> {OUT}")
    print(f"  human label spread: {spread}")
    print(f"  query shapes: {dict(sorted(shapes.items()))}")
    print("\nScore any future rubric against it with:")
    print("  python test/evaluate_ranking.py --judge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
