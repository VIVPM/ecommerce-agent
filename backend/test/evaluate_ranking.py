"""NDCG@10: does the most RELEVANT shoe come first, or just the best-rated one?

The 200-case suite scores the reply as a whole -- routing, faithfulness,
relevance -- so it cannot see inside the result list. Ranking is `ORDER BY` a
Bayesian rating score, which means a 4.8-rated formal shoe that slipped through
a title match outranks a 4.2 actual running shoe, and nothing today would notice.

Labels are ESCI, Amazon's scale, which is why the number is recognisable:
    Exact (3)      satisfies the query
    Substitute (2) does not, but is usable instead
    Complement (1) would be bought alongside
    Irrelevant (0) no

ONE DESIGN DECISION DOES THE HEAVY LIFTING. Labelling only the 10 shown would
measure ordering *within what was retrieved*, so "the best match ranked 25th"
would be invisible -- and if all 10 labels came out equal, NDCG would read 1.00
no matter what order they were in. So a POOL of 30 is retrieved and labelled,
NDCG@10 is computed over the 10 actually shown, and the ideal comes from all 30.
A buried match now costs score, which is the question worth asking.

Cheap and resumable, like the 200-case suite: one model call per QUERY (not per
pair), labels cached on the query plus the exact product set, and results written
after every query. Re-running after a ranking change only re-labels the queries
whose retrieved set actually moved.

    python test/evaluate_ranking.py --queries    # print the query set, spend nothing
    python test/evaluate_ranking.py              # run; resumes if interrupted
    python test/evaluate_ranking.py --sample 50  # CSV of pairs to hand-label

HONEST LIMIT: an LLM writes the labels and an LLM wrote the SQL that ranked
them, so this is partly self-grading. --sample emits pairs for a human to label
blind; the agreement rate is what says whether the score can be quoted. It also
cannot see RECALL -- a product missing from the pool of 30 is invisible to any
ranking metric.
"""
import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
from math import log2

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")          # Windows console is cp1252
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "app", ".env"))

from app import sql                                # noqa: E402
from app.cache import cache_get, cache_set         # noqa: E402
from app.llm_provider import complete              # noqa: E402

RESULTS_FILE = os.path.join(os.path.dirname(__file__), "ranking_results.json")
LOCK_FILE = RESULTS_FILE + ".lock"
SAMPLE_FILE = os.path.join(os.path.dirname(__file__), "esci_sample.csv")
SAMPLE_KEY_FILE = os.path.join(os.path.dirname(__file__), "esci_sample_key.json")
POOL_SIZE = 30
K = 10
GAIN = {"E": 3, "S": 2, "C": 1, "I": 0}

# Grouped by the FILTER SHAPE each one exercises, so a low score points at a
# layer rather than at "search feels off". Written by hand, not sampled: the
# point is to cover what the SQL layer actually implements.
QUERIES = [
    ("brand", "show me nike shoes"),
    ("brand", "puma sneakers"),
    ("brand", "campus shoes"),
    ("brand", "adidas footwear"),
    ("brand", "sparx shoes"),
    ("price", "shoes under 1000"),
    ("price", "shoes under 2000"),
    ("price", "cheapest shoes you have"),
    ("price", "shoes between 2000 and 3000"),
    ("price", "premium shoes above 5000"),
    ("brand+price", "puma shoes under 3000"),
    ("brand+price", "nike under 5000"),
    ("brand+price", "campus shoes under 1500"),
    ("brand+price", "cheap adidas shoes"),
    ("brand+price", "bata shoes under 2000"),
    ("rating", "best rated shoes"),
    ("rating", "top rated running shoes"),
    ("rating", "highest rated shoes under 2000"),
    ("rating", "most reviewed shoes"),
    ("rating", "best rated puma shoes"),
    ("threshold", "shoes rated above 4.5"),
    ("threshold", "shoes rated above 4 under 2000"),
    ("threshold", "nike shoes rated above 4.5"),
    ("threshold", "shoes with rating more than 4.2"),
    ("gender", "running shoes for men"),
    ("gender", "shoes for women"),
    ("gender", "womens sneakers under 2000"),
    ("gender", "mens formal shoes"),
    ("gender", "mens sports shoes under 3000"),
    ("descriptive", "running shoes"),
    ("descriptive", "walking shoes"),
    ("descriptive", "casual sneakers"),
    ("descriptive", "gym training shoes"),
    ("descriptive", "waterproof shoes"),
    ("descriptive", "leather formal shoes"),
    ("descriptive", "canvas shoes"),
    ("descriptive", "sports shoes for running"),
    ("descriptive", "boots for men"),
    ("descriptive", "slip on shoes"),
    ("compound", "4 nike and 5 puma shoes"),
    ("compound", "3 campus and 3 sparx shoes"),
    ("compound", "5 running shoes and 5 formal shoes"),
    ("relative", "shoes cheaper than the Campus Mike"),
    ("relative", "shoes better rated than the Sparx SM 852 sneakers"),
    ("mixed", "nike running shoes for men under 3000 rated above 4"),
    ("mixed", "cheap womens walking shoes with good ratings"),
    ("mixed", "best value puma sneakers under 2500"),
    ("mixed", "durable leather shoes for office under 4000"),
    ("mixed", "lightweight running shoes under 1500"),
    ("mixed", "top rated waterproof boots"),
]

LABEL_PROMPT = """You are labelling search results for a SHOE store, using Amazon's ESCI scale.

SHOPPER'S QUERY: {query}
{reference}
For EACH product below, output exactly one label:
  E = Exact       - satisfies EVERY expressed and inherited constraint
  S = Substitute  - usable instead, but misses a constraint or is not in the
                    requested top tier
  C = Complement  - bought alongside the requested product, not instead of it
  I = Irrelevant  - wrong product type or use

RULES THAT PREVENT OPTIMISTIC LABELLING:
- "best", "top", "highest rated", "most reviewed", "cheapest" and "best value"
  are CONSTRAINTS, not decoration. Only products in the strongest tier among the
  candidates can be Exact; a relevant but weaker option is Substitute. Use both
  rating and review count when the query says best-rated or best-value.
- A relative query INHERITS the reference product's type, use and gender. Merely
  being cheaper or better-rated is not enough: a women's casual shoe is a
  Substitute for a men's running-shoe anchor, not Exact.
- If the query names a brand, another brand cannot be Exact. If it names gender,
  the other gender is at best Substitute.
- A high rating never repairs the wrong type: a 4.9 formal shoe is Irrelevant to
  "running shoes".
- Be conservative between E and S. Exact means the shopper asked for THIS class
  of product, not merely that one numeric predicate happens to pass.

PRODUCTS (the whole candidate pool; compare products when judging superlatives):
{products}

Return ONLY a JSON array of {n} single-letter labels, in the same order, e.g.
["E","E","S","I",...]. No prose."""

# A relative comparison names a product, and its type/gender are part of the
# intent even when the shopper does not repeat them. "Sparx SM" matched sneakers
# and running shoes, so the first query set itself was ambiguous; 852 is explicit.
REFERENCE_TITLES = {
    "shoes cheaper than the Campus Mike": "campus mike",
    "shoes better rated than the Sparx SM 852 sneakers": "sparx sm 852",
}



def _reference_context(query):
    """Metadata for a named comparison anchor, or an empty prompt section."""
    fragment = REFERENCE_TITLES.get(query)
    if not fragment:
        return ""
    safe = fragment.replace("'", "''")       # values above are code-owned, still quote safely
    frame = sql.run_query(
        "SELECT title, brand, price, avg_rating, total_ratings FROM product "
        "WHERE availability = 'InStock' AND LOWER(title) LIKE LOWER('%"
        + safe + "%') ORDER BY total_ratings DESC NULLS LAST LIMIT 1")
    if frame is None or frame.empty:
        return ""
    r = frame.iloc[0]
    return ("\nREFERENCE PRODUCT (inherit its type/use/gender):\n"
            f"{r.get('title')} | brand: {r.get('brand')} | Rs. {r.get('price')} | "
            f"rating: {r.get('avg_rating')} | reviews: {r.get('total_ratings')}\n")


def _key(query, pids, reference=""):
    """Labels are reusable only for the same judge, query and product set.

    The old key omitted the PROMPT. Tightening the rubric then silently replayed
    old labels, which is worse than an uncached run because it looks evaluated.
    """
    payload = LABEL_PROMPT + "|" + reference + "|" + query + "|" + "|".join(pids)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _requested_count(generated: str):
    """How many products the shopper actually asked for, or None for "some".

    A compound query carries one LIMIT per UNION branch and the ask is their SUM
    ("4 Nike and 5 Puma" -> 9); a plain query carries at most a trailing LIMIT.
    Returning None means no count was named, so the full K applies.
    """
    if not generated:
        return None
    limits = [int(x) for x in re.findall(r"\bLIMIT\s+(\d+)\b", generated, re.I)]
    if not limits:
        return None
    if re.search(r"\bUNION\b", generated, re.I):
        return sum(limits) or None
    # A trailing LIMIT is the ask; an internal one (relative-comparison subquery)
    # is not, and only the trailing position can be trusted to be the count.
    trailing = re.search(r"\bLIMIT\s+(\d+)\s*;?\s*$", generated, re.I)
    return int(trailing.group(1)) if trailing else None


def _pool_sql(generated: str) -> str:
    """Same ranked query, but enough candidates to build an ideal top-K.

    Production deliberately leaves UNION branch limits alone: widening only the
    LAST branch turns "4 Nike and 5 Puma" into 4+10 and violates the request. The
    evaluator has a different job -- build a pool -- so it widens ALL branches
    proportionally to about POOL_SIZE, preserving the requested mix.

    A non-UNION trailing LIMIT is safe to widen. Internal limits (e.g. LIMIT 1 in
    a relative-comparison subquery) are deliberately untouched.
    """
    if not generated:
        return generated
    if re.search(r"\bUNION\b", generated, re.I):
        pattern = re.compile(r"\bLIMIT\s+(\d+)\b", re.I)
        limits = [int(x) for x in pattern.findall(generated)]
        total = sum(limits)
        if not total:
            return generated
        widened = iter(max(n, round(n * POOL_SIZE / total)) for n in limits)
        return pattern.sub(lambda _m: f"LIMIT {next(widened)}", generated)
    return sql._TRAILING_LIMIT_RE.sub(f" LIMIT {POOL_SIZE}", generated)


def retrieve(query):
    """(shown, pool) as lists of product dicts, in rank order.

    `shown` is exactly what the app would display; `pool` is the same query
    widened to POOL_SIZE, which is what makes a buried match visible.
    """
    shown_df, error = sql._run_sql_for_question(query)
    if shown_df is None or shown_df.empty:
        return [], [], error

    # Re-run the SAME generated SQL and keep more of it. The first version tried
    # to widen a trailing LIMIT, which never fired on ordinary queries: those
    # carry no LIMIT at all -- the app trims to 10 in pandas afterwards -- so the
    # pool silently collapsed back to the 10 shown, and the ideal was computed
    # over the very list being scored. Compound UNION queries are the opposite:
    # every branch carries its requested LIMIT, so _pool_sql widens ALL branches
    # proportionally rather than skewing one. NDCG can now see a buried match.
    generated = cache_get("sql", query)
    pool_df = None
    if generated:
        raw = sql.run_query(_pool_sql(generated))
        if raw is not None and not raw.empty:
            pool_df = sql._dedup_frame(raw).head(POOL_SIZE)

    def rows(df):
        return [{"pid": r.get("pid"), "title": r.get("title"), "brand": r.get("brand"),
                 "price": r.get("price"), "rating": r.get("avg_rating"),
                 "reviews": r.get("total_ratings")}
                for r in df.to_dict("records")]

    shown = rows(shown_df)
    pool = rows(pool_df) if pool_df is not None else shown
    # The shown items must be IN the pool, or the ideal is computed over a
    # different set than the one being scored.
    seen = {p["pid"] for p in pool}
    pool = pool + [s for s in shown if s["pid"] not in seen]
    return shown, pool, None


def label(query, products):
    """One model call for the whole list. Cached on query + exact product set."""
    pids = [p["pid"] or "" for p in products]
    reference = _reference_context(query)
    key = _key(query, pids, reference)
    if cached := cache_get("esci", key):
        try:
            labels = json.loads(cached)
            if len(labels) == len(products):
                return labels, True
        except json.JSONDecodeError:
            pass

    listing = "\n".join(
        f"{i}. {p['title']} | brand: {p['brand']} | Rs. {p['price']} | "
        f"rating: {p['rating']} | reviews: {p['reviews']}"
        for i, p in enumerate(products, 1))
    raw = complete(LABEL_PROMPT.format(query=query, reference=reference,
                                       products=listing, n=len(products)),
                   temperature=0.0)
    try:
        labels = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError):
        return None, False
    labels = [str(x).strip().upper()[:1] for x in labels]
    labels = [x if x in GAIN else "I" for x in labels]
    if len(labels) != len(products):
        return None, False
    cache_set("esci", key, json.dumps(labels))
    return labels, False


def ndcg(shown_labels, pool_labels, k=K):
    """NDCG@k of what was SHOWN, against the best possible from the POOL.

    The ideal comes from the pool, so burying a good match below the cut costs
    score. Computed over the shown list alone, a run of identical labels would
    score a perfect 1.00 in any order at all.
    """
    gains = [GAIN[x] for x in shown_labels[:k]]
    dcg = sum(g / log2(i + 2) for i, g in enumerate(gains))
    ideal = sorted((GAIN[x] for x in pool_labels), reverse=True)[:k]
    idcg = sum(g / log2(i + 2) for i, g in enumerate(ideal))
    return (dcg / idcg) if idcg else None      # nothing relevant anywhere: no score


def run():
    done = {}
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, encoding="utf-8") as f:
            done = {d["query"]: d for d in json.load(f)}
        print(f"Resuming: {len(done)} of {len(QUERIES)} already scored.\n")

    results, calls = [], 0
    for shape, query in QUERIES:
        if query in done:
            results.append(done[query])
            continue
        shown, pool, error = retrieve(query)
        if not shown:
            print(f"  --    {query[:52]:<54} (no results: {(error or '')[:28]})")
            results.append({"shape": shape, "query": query, "ndcg": None,
                            "n_shown": 0, "n_pool": 0, "note": "no results"})
        else:
            labels, cached = label(query, pool)
            if labels is None:
                print(f"  --    {query[:52]:<54} (labelling failed)")
                continue
            calls += 0 if cached else 1
            by_pid = {p["pid"]: x for p, x in zip(pool, labels)}
            shown_labels = [by_pid.get(s["pid"], "I") for s in shown]
            # Score at what was ASKED for. Scoring a 9-item request at K=10 is
            # measuring list length, not ranking -- nine perfect results cannot
            # reach 1.0 when the ideal is built from ten.
            asked = _requested_count(cache_get("sql", query))
            k = min(K, asked) if asked else K
            score = ndcg(shown_labels, labels, k)
            print(f"  {score:.3f}" if score is not None else "  ----",
                  f" {query[:52]:<54} shown={len(shown)} pool={len(pool)} k={k}"
                  f" {''.join(shown_labels[:K])}")
            results.append({"shape": shape, "query": query, "ndcg": score,
                            "k": k, "asked_for": asked,
                            "n_shown": len(shown), "n_pool": len(pool),
                            "shown_labels": shown_labels,
                            "pool_labels": labels,
                            "shown_pids": [s["pid"] for s in shown],
                            "pool": pool})
        tmp = RESULTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)
        os.replace(tmp, RESULTS_FILE)
    return results, calls


def report(results):
    scored = [r for r in results if r.get("ndcg") is not None]
    if not scored:
        print("\nNothing scored.")
        return
    mean = sum(r["ndcg"] for r in scored) / len(scored)
    print(f"\n{'=' * 64}\nNDCG@{K}: {mean:.3f}   ({len(scored)} of {len(results)} queries scored)")

    shapes = {}
    for r in scored:
        shapes.setdefault(r["shape"], []).append(r["ndcg"])
    print(f"\n{'SHAPE':<14}{'NDCG@10':>9}{'QUERIES':>9}")
    print("-" * 32)
    for shape, xs in sorted(shapes.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
        print(f"{shape:<14}{sum(xs) / len(xs):>9.3f}{len(xs):>9}")

    worst = sorted(scored, key=lambda r: r["ndcg"])[:8]
    print("\nWeakest queries (a buried better match costs score here):")
    for r in worst:
        print(f"  {r['ndcg']:.3f}  {r['query'][:50]:<52} {''.join(r['shown_labels'][:K])}")

    dead = [r for r in results if r.get("ndcg") is None
            and r.get("shown_labels") and set(r["shown_labels"]) == {"I"}]
    if dead:
        print(f"\n{len(dead)} queries returned results of which NONE was relevant "
              f"(worse than a low score -- the list is confidently wrong):")
        for r in dead:
            print(f"  {r['query']}  ({r['n_shown']} results, all Irrelevant)")

    empty = [r for r in results if r.get("note") == "no results"]
    if empty:
        print(f"\n{len(empty)} queries returned nothing at all "
              f"(a recall problem, which no ranking metric can see):")
        for r in empty[:5]:
            print(f"  {r['query']}")


def sample(n):
    """A BLIND, stratified sample: no model label is put in the CSV.

    The first sample exposed `model_label` in the same workbook and randomly drew
    43 Exact vs 7 Substitute labels. It reached 86% agreement, but did not test C
    or I and made accidental peeking possible. This one balances every label that
    actually occurs, then fills the remainder while preserving query-shape spread.
    The answer key is a separate JSON file consumed only by --agreement.
    """
    with open(RESULTS_FILE, encoding="utf-8") as f:
        results = json.load(f)
    shape_for = {q: shape for shape, q in QUERIES}
    pairs = [{"query": r["query"], "shape": shape_for[r["query"]], "product": p,
              "model_label": x}
             for r in results if r.get("pool_labels")
             for p, x in zip(r["pool"], r["pool_labels"])]
    random.seed(0)
    random.shuffle(pairs)

    buckets = {x: [p for p in pairs if p["model_label"] == x] for x in GAIN}
    present = [x for x in GAIN if buckets[x]]
    target = max(1, n // max(1, len(present)))
    picked = []
    for x in present:
        picked.extend(buckets[x][:target])

    # Fill any deficit from underrepresented QUERY SHAPES, not just random Exacts.
    chosen = {(p["query"], p["product"]["pid"]) for p in picked}
    remaining = [p for p in pairs if (p["query"], p["product"]["pid"]) not in chosen]
    while len(picked) < min(n, len(pairs)) and remaining:
        counts = {shape: sum(p["shape"] == shape for p in picked)
                  for shape, _ in QUERIES}
        remaining.sort(key=lambda p: counts.get(p["shape"], 0))
        picked.append(remaining.pop(0))
    picked = picked[:n]
    random.shuffle(picked)

    key = {}
    with open(SAMPLE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sample_id", "shape", "query", "product", "brand", "price",
                    "rating", "reviews", "your_label_E_S_C_I"])
        for i, item in enumerate(picked, 1):
            p = item["product"]
            sid = f"R{i:03d}"
            w.writerow([sid, item["shape"], item["query"], p["title"], p["brand"],
                        p["price"], p["rating"], p["reviews"], ""])
            key[sid] = item["model_label"]
    with open(SAMPLE_KEY_FILE, "w", encoding="utf-8") as f:
        json.dump(key, f, indent=1)

    spread = {x: sum(key[f"R{i:03d}"] == x for i in range(1, len(picked) + 1))
              for x in GAIN}
    print(f"Wrote {len(picked)} BLIND pairs to {SAMPLE_FILE}")
    print(f"Model-label spread (answer key kept separately): {spread}")
    absent = [x for x in GAIN if not buckets[x]]
    if absent:
        print(f"No {', '.join(absent)} candidates exist in the retrieved pools; "
              "those classes cannot be validated by this ranking sample.")
    print("Fill your_label_E_S_C_I, then re-run with --agreement.")


def agreement():
    with open(SAMPLE_KEY_FILE, encoding="utf-8") as f:
        key = json.load(f)
    with open(SAMPLE_FILE, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["your_label_E_S_C_I"].strip()]
    if not rows:
        print("No hand labels filled in yet.")
        return
    valid = [r for r in rows if r["your_label_E_S_C_I"].strip().upper()[:1] in GAIN]
    if len(valid) != len(rows):
        print(f"Ignoring {len(rows) - len(valid)} invalid hand labels.")
    human = [r["your_label_E_S_C_I"].strip().upper()[:1] for r in valid]
    model = [key[r["sample_id"]] for r in valid]
    same = sum(a == b for a, b in zip(human, model))
    n = len(valid)
    print(f"Agreement: {same}/{n} = {100 * same / n:.0f}%")

    # Cohen's kappa corrects raw agreement for an imbalanced sample. 86% on a
    # mostly-Exact sample was only kappa=0.59; both belong in the report.
    h = {x: human.count(x) for x in GAIN}
    m = {x: model.count(x) for x in GAIN}
    pe = sum(h[x] * m[x] for x in GAIN) / (n * n)
    po = same / n
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    print(f"Cohen's kappa: {kappa:.3f}")
    print(f"Human labels: {h}")
    print(f"Model labels: {m}")

    print("By query shape:")
    for shape in sorted({r["shape"] for r in valid}):
        subset = [(a, b) for r, a, b in zip(valid, human, model) if r["shape"] == shape]
        hit = sum(a == b for a, b in subset)
        print(f"  {shape:<13} {hit}/{len(subset)} = {100 * hit / len(subset):.0f}%")
    print("Below ~80% or with a systematic one-way error, the NDCG number is not "
          "worth quoting -- the labels are the measurement.")

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--queries", action="store_true", help="print the query set and exit")
    ap.add_argument("--sample", type=int, help="write N pairs to hand-label")
    ap.add_argument("--agreement", action="store_true", help="score the hand labels")
    args = ap.parse_args()

    if args.queries:
        shapes = {}
        for shape, q in QUERIES:
            shapes.setdefault(shape, []).append(q)
        for shape, qs in shapes.items():
            print(f"\n{shape} ({len(qs)}):")
            for q in qs:
                print(f"  {q}")
        print(f"\n{len(QUERIES)} queries across {len(shapes)} filter shapes.")
        return 0
    if args.sample:
        sample(args.sample)
        return 0
    if args.agreement:
        agreement()
        return 0

    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        print(f"Another evaluation owns {LOCK_FILE}. If no evaluator is running, "
              "remove this stale lock.")
        return 2
    try:
        results, calls = run()
        report(results)
        print(f"\nModel calls this run: {calls} (labels cached on judge + query + product set)")
    finally:
        try:
            os.unlink(LOCK_FILE)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
