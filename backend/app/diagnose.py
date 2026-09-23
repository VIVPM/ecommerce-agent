"""Why did that search come back empty? One answer, not a list of special cases.

Three separate functions grew here, each hardcoding one reason a result set could
be empty: stock (`_count_ignoring_stock`), two title words that never co-occur
(`_blocking_terms`), and the nearest affordable product (`_cheapest_buyable`).
Each was right about its own case and silent about everything else, so two real
failures fell through all three and answered "try a different brand, a higher
price" when neither brand nor price was the problem.

This asks the general question instead: drop each condition in turn and see which
one was holding the result set at zero. Measured on "Nike men's running under 3000
rated above 4": dropping the BRAND unlocks 115 products and dropping anything else
unlocks none -- so the honest answer is "no Nike, but 115 other running shoes for
men". I had guessed gender; the probe disagreed and the probe was right, which is
the point of measuring instead of enumerating cases.

The split that makes this work:
  * SQL produces the FACTS. "19 if you drop the price", "0 if you drop nothing".
    A model guessing those numbers would be inventing inventory, which is the
    single worst failure this project has had.
  * The MODEL decides which fact matters and how to say it. That is judgement,
    and it generalises to combinations nobody enumerated.

Costs one query per condition plus one short model call, on a path that already
returned nothing -- the cheapest path there is.
"""
import logging
import re

from app.llm_provider import complete

logger = logging.getLogger(__name__)

# Enough to find the blocker without turning a dead end into a scan.
MAX_CONDITIONS = 8


def _where_span(sql: str):
    """(start, end) of the WHERE body, stopping before ORDER BY / LIMIT."""
    m = re.search(r"\bWHERE\b", sql, re.I)
    if not m:
        return None
    start = m.end()
    tail = re.search(r"\b(ORDER\s+BY|LIMIT|GROUP\s+BY)\b", sql[start:], re.I)
    return start, (start + tail.start()) if tail else len(sql)


def split_conditions(sql: str):
    """Top-level AND-separated conditions, ignoring ANDs inside parentheses.

    A relative comparison puts a whole subquery inside one condition, and a
    naive split on " AND " would tear it in half and produce SQL that cannot
    run.
    """
    span = _where_span(sql or "")
    if not span:
        return []
    body = sql[span[0]:span[1]]
    parts, depth, cur, i = [], 0, [], 0
    while i < len(body):
        ch = body[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and re.match(r"\s+AND\s+", body[i:], re.I):
            token = re.match(r"\s+AND\s+", body[i:], re.I)
            parts.append("".join(cur).strip())
            cur = []
            i += token.end()
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _rebuild(sql: str, keep: list) -> str:
    """The same query with only `keep` in its WHERE. An empty list becomes
    TRUE rather than an empty WHERE, which is a syntax error."""
    span = _where_span(sql)
    body = " " + (" AND ".join(keep) if keep else "TRUE") + " "
    return sql[:span[0]] + body + sql[span[1]:]


# What a condition MEANS, for a model that should not have to read SQL. Falls
# back to the raw condition, which is still better than silence.
def describe(cond: str) -> str:
    c = " ".join(cond.split())
    if re.search(r"availability\s*=\s*'InStock'", c, re.I):
        return "in stock"
    if m := re.search(r"price\s*(<=?|>=?)\s*(\d+)", c, re.I):
        return f"price {'under' if '<' in m.group(1) else 'over'} Rs. {m.group(2)}"
    if m := re.search(r"price\s+BETWEEN\s+(\d+)\s+AND\s+(\d+)", c, re.I):
        return f"price between Rs. {m.group(1)} and Rs. {m.group(2)}"
    # An anchor comparison must be read as a WHOLE. Its subquery carries a title
    # LIKE, so the title rule below would label "better rated than the Sparx SM
    # 852" as 'title contains "Sparx SM 852"' -- and the model, handed that, said
    # the shopper wanted shoes better rated than themselves.
    if m := re.search(r"(avg_rating|price)\s*([<>]=?)\s*\(\s*SELECT\s+(MIN|MAX)"
                      r".*?LIKE\s+(?:LOWER\()?'%([^%']+)%'", c, re.I | re.S):
        field = "rated better than" if m.group(1).lower() == "avg_rating" else (
            "cheaper than" if "<" in m.group(2) else "more expensive than")
        return f"{field} \"{m.group(4)}\""
    if m := re.search(r"brand\)?\s+LIKE\s+(?:LOWER\()?'%([^%']+)%'", c, re.I):
        return f"brand {m.group(1)}"
    if re.search(r"NOT\s+LIKE\s+(?:LOWER\()?'%women", c, re.I):
        return "men's (excludes women's)"
    if m := re.search(r"title\)?\s+LIKE\s+(?:LOWER\()?'%([^%']+)%'", c, re.I):
        return f'title contains "{m.group(1)}"'
    if m := re.search(r"avg_rating\s*(>=?|<=?)\s*([\d.]+)", c, re.I):
        return f"rated {'above' if '>' in m.group(1) else 'below'} {m.group(2)}"
    if re.search(r"avg_rating\s+IS\s+NOT\s+NULL", c, re.I):
        return "has any rating"
    return c[:70]


_ANCHOR = re.compile(
    r"\(\s*SELECT\s+(?:MIN|MAX)\s*\([^)]*\)\s*FROM\s+product\s+WHERE\s+"
    r"LOWER\(title\)\s+LIKE\s+(?:LOWER\()?'%([^%']+)%'\)?\s*\)", re.I | re.S)


def unresolved_anchor(sql: str, run_query):
    """The product a comparison is anchored to, when that product isn't found.

    "better rated than the Sparx SM 852 sneakers" builds
    `avg_rating > (SELECT MAX(avg_rating) ... LIKE '%Sparx SM 852 sneakers%')`.
    The real title is "Sparx SM 852 | Stylish, Comfortable | Sneakers For Men",
    so the phrase never matches, MAX() is NULL, and `rating > NULL` is false for
    every row. The search then looks like "nothing is better rated", when the
    truth is that the thing being compared against was never found -- a
    completely different thing to tell the shopper.
    """
    m = _ANCHOR.search(sql or "")
    if not m:
        return None
    name = m.group(1)
    df = run_query("SELECT title FROM product WHERE LOWER(title) LIKE LOWER('%"
                   + name.replace("'", "''") + "%') LIMIT 1")
    return None if (df is not None and not df.empty) else name


def probe(sql: str, run_query, dedup):
    """[(description, rows_without_it, condition)] -- what each condition costs.

    `run_query` and `dedup` are injected rather than imported to keep this module
    free of a circular import with sql.py, and to make it testable offline.
    """
    conditions = split_conditions(sql)
    if not 2 <= len(conditions) <= MAX_CONDITIONS:
        return []
    out = []
    for i, cond in enumerate(conditions):
        df = run_query(_rebuild(sql, conditions[:i] + conditions[i + 1:]))
        rows = 0 if df is None or df.empty else len(dedup(df))
        out.append((describe(cond), rows, cond))
    return out


EXPLAIN_PROMPT = """A shopper searched a SHOE store and got NO results. Explain why, helpfully.

THEIR REQUEST: {question}

MEASURED FACTS -- how many products would match if ONE condition were dropped:
{facts}

Every number above is real. Do NOT invent products, counts or prices, and do not
mention SQL, columns or queries -- the shopper never saw any.

Write 1-2 sentences:
- Name the condition that is actually blocking them, which is the one whose
  removal unlocks the most products. If several do, name the most useful to relax.
- If dropping a condition unlocks products, say how many and offer that as the
  next step, in their words ("all the Nike under Rs. 3000 we have are women's --
  want those?").
- If dropping ANY single condition still gives nothing, say plainly that this
  combination is not in the catalogue and suggest the loosest sensible search.
- Never blame a condition that was not the blocker. "Try a different brand" when
  the brand was fine is worse than saying nothing.
- If the facts come in GROUPS, the shopper asked for several things at once (e.g.
  "4 Nike and 5 Puma"). Explain each group on its own terms, and never apply one
  group's numbers to another."""


def explain(question: str, sql: str, run_query, dedup) -> str:
    """A shopper-facing sentence, or "" when there is nothing useful to say.

    Fails quiet: any problem here must leave the ordinary "nothing found"
    message in place rather than break the answer.
    """
    try:
        # A comparison against a product we cannot find is not a "no results"
        # problem, and no amount of relaxing other conditions explains it.
        missing = unresolved_anchor(sql, run_query)
        if missing:
            return (f"I couldn't find a product called \"{missing}\" to compare "
                    f"against, so I had nothing to measure the others by. Try the "
                    f"shortest distinctive part of its name, or tell me the brand "
                    f"and I'll search that instead.")
        # "4 Nike and 5 Puma" is several searches glued with UNION. Probing it
        # whole only ever read the FIRST branch (the WHERE span stops at its
        # LIMIT), so "drop the price -> 12" meant 12 NIKE and the model could say
        # it about Puma too. Split it and give the model one fact set per group.
        parts = [p.strip().strip("()").strip()
                 for p in re.split(r"\bUNION\s+(?:ALL\s+)?", sql, flags=re.I)]
        blocks = []
        for i, part in enumerate(parts, 1):
            facts = probe(part, run_query, dedup)
            if not facts:
                continue
            lines = [f"- drop \"{d}\" -> {n} product(s)"
                     for d, n, _ in sorted(facts, key=lambda f: -f[1])]
            if all(rows == 0 for _, rows, _ in facts):
                lines.append("Nothing in the catalogue matches any part of this combination.")
            head = f"GROUP {i}:\n" if len(parts) > 1 else ""
            blocks.append(head + "\n".join(lines))
        if not blocks:
            return ""
        listing = "\n\n".join(blocks)
        out = complete(EXPLAIN_PROMPT.format(question=question, facts=listing),
                       temperature=0.2)
        return (out or "").strip()
    except Exception as e:
        logger.error("diagnose.explain failed: %s", e)
        return ""
