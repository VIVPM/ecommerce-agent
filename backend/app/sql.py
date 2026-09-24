# Generates read-only product SQL and formats grounded catalogue answers.
import re
import json
import asyncio
import logging
from sqlalchemy import text
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)


GEMINI_MODEL = 'gemini-2.5-flash'


DEFAULT_DISPLAY_ROWS = 10

from app.db.database import readonly_engine
from app.cache import cache_get, cache_set
from app.llm_provider import complete, stream as llm_stream

FALLBACK_MODEL = 'gemini-2.5-pro'
COMPREHENSION_MODEL = 'gemini-2.5-flash'

sql_prompt = """You are an expert in understanding the database schema and generating SQL queries for a natural language question asked
pertaining to the data you have. The schema is provided in the schema tags. 
<schema> 
table: product 

fields: 
product_link - string (hyperlink to product)	
title - string (name of the product)	
brand - string (brand of the product)	
price - integer (price of the product in Indian Rupees)
avg_rating - float (average rating of the product. Range 0-5, 5 is the highest.)
total_ratings - integer (total number of ratings for the product)
availability - string ('InStock' = can be bought now, 'OutOfStock' = listed but unbuyable, 'Unavailable' = delisted)
scraped_at - timestamp (when this row's price/rating was last verified against Flipkart)

</schema>
EMPTY RATINGS: avg_rating and total_ratings are NULL for newly listed products
that have no reviews yet (245 of them are in stock). Postgres sorts NULLs FIRST
on `ORDER BY ... DESC`, so a naive "top rated" query returns UNRATED products —
the exact opposite of what was asked. Whenever the question involves rating,
add `avg_rating IS NOT NULL` and write `ORDER BY avg_rating DESC NULLS LAST`.

"TOP RATED" MEANS PROVEN, NOT PERFECT: a 5.0 from 3 reviews is not better rated
than a 4.6 from 500 — the raw average alone gives a tiny sample too much weight.
Rank by a confidence-weighted (Bayesian) score so a strong average backed by many
ratings beats a slightly higher average from a handful. The weighting lives in the
ORDER BY only — NEVER silently exclude products by rating count. For any question
about top / best / highest rated shoes, AND for rating THRESHOLD questions
("rated higher than 4.5"):
    WHERE avg_rating IS NOT NULL AND total_ratings IS NOT NULL
    ORDER BY ( total_ratings::numeric / (total_ratings + 50) * avg_rating
             + 50.0 / (total_ratings + 50) * 4.1 ) DESC
Here 4.1 is the catalogue's average rating and 50 a confidence prior: a tiny sample
is pulled toward that average, so it sorts LOW without being dropped. For a threshold
question KEEP the user's cutoff in WHERE (e.g. `AND avg_rating > 4.5`) and just order
by that weighted score — do NOT add any total_ratings floor; return exactly what
matches the cutoff the user asked for.

GENDER: there is no gender column — it appears only inside `title`, and the
substring 'men' also matches 'women'. So:
  men's   -> LOWER(title) LIKE '%men%' AND LOWER(title) NOT LIKE '%women%'
  women's -> LOWER(title) LIKE '%women%'
Never filter men's shoes with LIKE '%men%' alone; it returns women's shoes.

ATTRIBUTES THAT AREN'T COLUMNS: there is no size, colour, material, width or
waterproof column. Two different cases — treat them differently:
 (a) DESCRIPTIVE words sellers routinely put in the product TITLE: waterproof,
     leather, mesh, canvas, running, walking, casual, sports, gym, sneaker.
     Matching these IS useful — LOWER(title) LIKE '%waterproof%' finds products
     whose seller states it. Do match them, against `title` ONLY, never `brand`.
 (b) Words that generate FALSE matches: colours and sizes. 'red' matches the
     brands RED TAPE and RED CHIEF, and no row records a size at all. Do NOT
     match these against anything — ignore the constraint entirely.
Either way the caller appends a note telling the user which parts could not be
applied and that title matches reflect the seller's own description.
BUT if colour, size or width is the ONLY thing the user is filtering on — there is
no brand, price, rating, gender or describable type ((a)-style word) left to search
besides the word "shoes" — then this catalogue has nothing it can actually filter
on, and returning the whole catalogue would fake an answer. In that one case emit a
query that returns nothing:
    SELECT * FROM product WHERE 1=0
The caller then explains that colour and size can't be searched. ("red shoes in
size 9" -> WHERE 1=0; but "red Nike shoes under 2000" still searches brand+price.)

NO DISCOUNT DATA: the catalogue holds current prices only — there is no discount,
MRP, "was" price or offer information at all. If the user asks about discounts,
offers or sales, do NOT invent a column for it: answer on price alone (e.g. the
cheapest matching products) and let the final response make clear you are ranking
by price.
OUT-OF-CATALOGUE REQUESTS: this catalogue is FOOTWEAR ONLY. If the user asks for a
different product type — laptops, phones, shirts, watches, bags — you must NOT fall
back to returning shoes that happen to fit their price range. Returning a Rs. 180
sneaker for "laptops under 50000" is a wrong answer dressed up as a result. In that
case emit a query that deliberately returns nothing:
    SELECT * FROM product WHERE 1=0
The caller then explains that the catalogue only covers footwear.

CRITICAL RULE: The dataset ONLY contains shoes. If the user asks about "shoes", DO NOT add a SQL filter for `title LIKE '%shoe%'` or `title LIKE '%shoes%'`. This will incorrectly filter out shoes that do not have the word "shoe" in their title. Completely ignore the word "shoe" when constructing your WHERE clauses.
STOCK RULE: Most of the catalogue is buyable, but a chunk is not. ALWAYS add `availability = 'InStock'` to the WHERE clause so you only ever recommend products a user can actually buy.
The ONLY exception: if the user asks specifically about a named product's availability ("is X in stock?"), omit that filter so you can answer honestly.
IMPORTANT: Brand names in the database are inconsistent (e.g. "NIKE", "Nike", "nike").
Always use LOWER() on both sides for case-insensitive matching: LOWER(brand) LIKE LOWER('%nike%').
Apply the same LOWER() pattern for title searches too. Never use "ILIKE".
NEVER match a title with `=`. Titles carry extra tokens the user won't type
(e.g. the real row is 'CAMPUS MIKE (N) Running Shoes For Men', not 'CAMPUS MIKE
Running Shoes For Men'), so `=` silently matches nothing. This rule applies
EVERYWHERE, including inside subqueries — always LOWER(title) LIKE LOWER('%...%')
with the shortest distinctive fragment of the name.

RELATIVE COMPARISONS: when the user asks for something cheaper than / better rated
than a NAMED product, anchor with a subquery — but the same product often exists
as several rows at different prices, so the subquery MUST return exactly one
deterministic value. Use an aggregate, never a bare column with LIMIT 1.
Correct:
  SELECT * FROM product
   WHERE availability = 'InStock'
     AND price < (SELECT MIN(price) FROM product WHERE LOWER(title) LIKE LOWER('%CAMPUS MIKE%'))
   ORDER BY price ASC
Wrong (matches nothing, or is non-deterministic):
  price < (SELECT price FROM product WHERE LOWER(title) = LOWER('CAMPUS MIKE Running Shoes For Men') LIMIT 1)
Use MIN(price) for "cheaper than" and MAX(avg_rating) for "better rated than".

DIFFERENT COUNTS PER GROUP: if the user asks for N of one thing AND M of another
("4 Nike and 5 Puma shoes"), combine parenthesised subqueries — Postgres REQUIRES the
parentheses when a UNION branch has its own LIMIT:
  (SELECT * FROM product WHERE availability='InStock' AND LOWER(brand) LIKE LOWER('%nike%') LIMIT 4)
  UNION ALL
  (SELECT * FROM product WHERE availability='InStock' AND LOWER(brand) LIKE LOWER('%puma%') LIMIT 5)
A bare `LIMIT 4` before `UNION` (no parentheses) is a syntax error — never write that.
Create a single SQL query for the question provided.
The query should have all the fields in SELECT clause (i.e. SELECT *)

Just the SQL query is needed, nothing more. Always provide the SQL in between the <SQL></SQL> tags."""


comprehension_prompt = """You are an expert in understanding the context of the question and replying based on the data pertaining to the question provided. You will be provided with Question: and Data:. The data will be in the form of an array or a dataframe or dict. Reply based on only the data provided as Data for answering the question asked as Question. Do not write anything like 'Based on the data' or any other technical words. Just a plain simple natural language response.
The Data would always be in context to the question asked. For example is the question is “What is the average rating?” and data is “4.3”, then answer should be “The average rating for the product is 4.3”. So make sure the response is curated with the question and data. Make sure to note the column names to have some context, if needed, for your response.
There can also be cases where you are given an entire dataframe in the Data: field. Always remember that the data field contains the answer of the question asked. All you need to do is to always reply in the following format when asked about a product: 
Product title, price in indian rupees, rating WITH its rating count, and then product link as a clickable markdown link. Take care that all the products are presented as a NUMBERED list (1., 2., 3., ...), one product per line — never as bullet points or a paragraph.
Always print the rating count next to the rating, e.g. "Rating: 4.4 (12,043 ratings)". A 5.0 from 3 people and a 4.4 from 12,000 are not comparable and the count is what shows that. The field is RATINGS — never call them "reviews", they are different counts on Flipkart.
IMPORTANT: Always format product links as markdown links like [View Product](url). Never paste raw URLs.
There is NO discount, MRP or "was" price in the data — never mention a discount or
percentage off. If a product has no rating, say "no ratings yet" rather than
inventing one or printing a blank.
For example:
1. Campus Women Running Shoes: Rs. 1104, Rating: 4.4 [View Product](https://www.flipkart.com/...)
2. Campus Women Running Shoes: Rs. 1104, Rating: 4.4 [View Product](https://www.flipkart.com/...)
3. Campus Women Running Shoes: Rs. 1104, no ratings yet [View Product](https://www.flipkart.com/...)

"""


def generate_sql_query(question):


    return complete(question, system=sql_prompt, temperature=0.2,
                    model=GEMINI_MODEL, fallback=FALLBACK_MODEL)


def run_query(query):

    if not query.strip().lstrip('(').upper().startswith('SELECT'):
        return None
    try:
        with readonly_engine.connect() as conn:
            return pd.read_sql_query(text(query), conn)
    except Exception as e:


        logger.warning("SQL execution failed: %s", e)
        return None


def data_comprehension(question, context):
    return complete(f"QUESTION: {question}. DATA: {context}",
                    system=comprehension_prompt, temperature=0.2, model=COMPREHENSION_MODEL)


_NOT_SEARCHABLE = {
    "size":   r"\bsize\b|\buk\s*\d|\beu\s*\d|\bus\s*\d",
    "colour": r"\bcolou?r\b|\b(red|blue|black|white|green|pink|grey|gray|yellow|brown)\b",
    "width":  r"\bwide\b|\bnarrow\b|\bwidth\b",
}
_TITLE_ONLY = {
    "waterproofing": r"\bwaterproof\b|\bwater[- ]resistant\b",
    "material":      r"\bleather\b|\bmesh\b|\bcanvas\b|\bsuede\b|\bmaterial\b",
}


def _join(words):
    words = sorted(words)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _unsupported_note(question: str) -> str:
    """Say which parts of the request couldn't be honoured, and how loosely the
    ones that were matched actually hold."""
    if not question:
        return ""
    ql = question.lower()
    blocked = {k for k, pat in _NOT_SEARCHABLE.items() if re.search(pat, ql)}
    loose = {k for k, pat in _TITLE_ONLY.items() if re.search(pat, ql)}
    if not blocked and not loose:
        return ""

    parts = []
    if blocked:


        parts.append(
            f"I can't search by {_join(blocked)} — the catalogue only records "
            f"title, brand, price, rating and stock, so please check that on the "
            f"Flipkart listing."
        )
    if loose:
        parts.append(
            f"Matches on {_join(loose)} come from the seller's own product title, "
            f"not a verified attribute."
        )
    return "\n\n*Note: " + " ".join(parts) + "*"


_ANCHOR_RE = re.compile(
    r"(cheaper|less expensive|lower priced|rated higher|better rated|higher rated)\s+than\s+(?:the\s+)?(.+?)\s*[\.\?]?$",
    re.I,
)


def _anchor_phrase(question: str) -> str:
    """For "cheaper than X" questions, return "cheaper than X (Rs. 665)".

    This goes in the HEADER, not a footer: a note under ten products and a
    "showing 10 of 184" line is easy to miss, and the whole point is that the
    shopper can see what "cheaper" is being measured against before they read.
    """
    if not question:
        return ""
    m = _ANCHOR_RE.search(question.strip())
    if not m:
        return ""
    kind, name = m.group(1).lower(), m.group(2).strip().strip('"\'')
    if len(name) < 3:
        return ""
    cheaper = kind.startswith(("cheap", "less", "lower"))
    col, agg = ("price", "MIN") if cheaper else ("avg_rating", "MAX")
    try:
        with readonly_engine.connect() as conn:
            val = conn.execute(
                text(f"SELECT {agg}({col}) FROM product WHERE LOWER(title) LIKE LOWER(:frag)"),
                {"frag": f"%{name}%"},
            ).scalar()
    except Exception as e:
        logger.warning("anchor lookup failed for %r: %s", name, e)
        return ""
    if val is None:
        return ""
    shown = f"Rs. {int(val)}" if cheaper else f"{val} stars"
    verb = "cheaper than" if cheaper else "rated higher than"
    return f"{verb} {name} ({shown})"


_YES_NO_RE = re.compile(r"^\s*(do|does|are|is|any|have|got|can)\b", re.I)


def _header(question: str, total: int) -> str:
    anchor = _anchor_phrase(question)
    if _YES_NO_RE.match(question or ""):
        lead = f"Yes — {total} matching product{'s' if total != 1 else ''} in the catalogue."
        return f"{lead} Here are the top ones:\n"
    if anchor:
        return f"Here are the top results, {anchor}:\n"
    return "Here are the top results from your search:\n"


def _dedup_key(title, brand):
    """Collapse seller variants of ONE product to a single key. Keyed on brand +
    normalized title so same-brand listings of a shoe merge, but different brands
    that share a generic title (e.g. Skechers vs PUMA "Walking Shoes For Women")
    stay separate — collapsing those would hide real products."""
    t = str(title or "").lower()
    b = str(brand or "").lower()
    if b and t.startswith(b):
        t = t[len(b):]
    t = re.sub(r"\b[a-z]\b", " ", t)
    t = re.sub(r"[^a-z0-9]+", "", t)
    bkey = re.sub(r"[^a-z0-9]+", "", b)
    return bkey + "|" + (t or str(title or "").lower())


_INSTOCK_RE = re.compile(r"\bavailability\s*=\s*'InStock'", re.I)


_PRICE_RE = re.compile(r"\bprice\s*(?:<=|>=|<|>)\s*\d+(?:\.\d+)?", re.I)
_TERM_RE = re.compile(
    r"\b(title|brand)\)?\s+(NOT\s+)?LIKE\s+(?:LOWER\(\s*)?'%([^%']+)%'", re.I)


def _count(where_sql: str):
    df = run_query(f"SELECT COUNT(*) AS n FROM product WHERE {where_sql}")
    return None if df is None or df.empty else int(df['n'].iloc[0])


def _where_clause(sql: str):
    """The WHERE body of a single (non-UNION) query, without ORDER BY / LIMIT."""
    m = re.search(r"\bwhere\b(.*?)(?:\border\s+by\b|\blimit\b|$)", sql or "", re.I | re.S)
    return m.group(1).strip() if m else None


def why_no_results(sql: str) -> dict:
    """Diagnose an empty search with verified catalogue FACTS — no wording, no
    decisions. The model reads these and decides what to tell the shopper, so a new
    kind of empty result is handled by reasoning instead of another special case.

    Each fact relaxes one kind of constraint and re-counts:
      - stock:  matches that exist but can't be bought (out of stock vs delisted)
      - price:  matches if the price limit were dropped
      - terms:  each title/brand word on its own (finds impossible combinations)
    plus the cheapest buyable listing of the brand asked for, if there is one."""
    facts = {}
    if re.search(r"\bunion\b", sql or "", re.I):


        groups = []
        for i, part in enumerate(re.split(r"\bunion\s+(?:all\s+)?", sql, flags=re.I), 1):
            part = part.strip().strip("()").strip()
            part_facts = why_no_results(part)
            if part_facts:
                groups.append({"group": i, **part_facts})
        return {"groups": groups} if groups else facts
    where = _where_clause(sql)
    if not where:
        return facts

    if _INSTOCK_RE.search(where):
        relaxed = _INSTOCK_RE.sub("1=1", where)
        total = _count(relaxed)
        if total:
            out = _count(f"({relaxed}) AND availability = 'OutOfStock'") or 0
            facts["matches_ignoring_stock"] = {
                "total": total, "temporarily_out_of_stock": out,
                "delisted_never_coming_back": total - out}

    if _PRICE_RE.search(where):
        n = _count(_PRICE_RE.sub("1=1", where))
        if n:
            facts["in_stock_matches_without_price_limit"] = n

    terms = []
    for m in _TERM_RE.finditer(where):
        if m.group(2):
            continue
        col, word = m.group(1).lower(), m.group(3).strip().lower()
        n = _count(f"availability = 'InStock' AND LOWER({col}) LIKE '%{word.replace(chr(39), chr(39)*2)}%'")
        if n is not None:
            terms.append({"field": col, "word": word, "in_stock_alone": n})
    if len(terms) > 1:
        facts["each_term_alone"] = terms

    brand = next((t["word"] for t in terms if t["field"] == "brand"), None)
    if brand:
        df = run_query("SELECT title, price FROM product WHERE availability = 'InStock' "
                       f"AND LOWER(brand) LIKE '%{brand.replace(chr(39), chr(39)*2)}%' "
                       "AND price IS NOT NULL ORDER BY price ASC LIMIT 1")
        if df is not None and not df.empty:
            facts["cheapest_buyable_of_brand"] = {
                "brand": brand, "price": int(df['price'].iloc[0]), "title": df['title'].iloc[0]}
    return facts


_EXPLAIN_SYS = """A shoe-store search returned NO products. You get the shopper's request and
verified FACTS about why. Write 1-3 short, friendly sentences that:
- say plainly why nothing matched, using only the facts (e.g. the items exist but are out
  of stock or delisted; each word matches alone but never together; the price limit is
  what excludes them)
- offer ONE concrete next step drawn from the facts, phrased as a question
Rules: use ONLY numbers and names present in FACTS — never invent products, prices or
counts. Delisted items will NOT come back; only out-of-stock items might. Never suggest
changing a brand or price the shopper didn't mention. Prices are in Rs."""

_NO_MATCH = ("I couldn't find any products matching that. Try broadening your search — "
             "a different brand, a higher price, or fewer conditions.")


def explain_no_results(question: str, sql: str) -> str:
    """Empty-result reply: code gathers the facts, the model decides what to say.
    Falls back to the plain message when there is nothing to reason about or the
    model call fails — never lets a diagnosis error break the search."""
    try:
        facts = why_no_results(sql)
        if not facts:
            return _NO_MATCH
        return (complete(f"REQUEST: {question}\nFACTS: {json.dumps(facts)}",
                         system=_EXPLAIN_SYS, temperature=0.2,
                         model=COMPREHENSION_MODEL) or "").strip() or _NO_MATCH
    except Exception as e:
        logger.warning("No-results diagnosis failed: %s", e)
        return _NO_MATCH


def _split_unbuyable(rows):
    """(temporarily_out, delisted) counts. 'OutOfStock' can come back; 'Unavailable'
    is delisted at the source and will not — promising a restock for those is a lie,
    and ~45% of the catalogue's unbuyable rows are delisted, not merely out."""
    if rows is None or 'availability' not in rows.columns:
        return 0, 0
    avail = rows['availability']
    return int((avail == 'OutOfStock').sum()), int((avail != 'InStock').sum() - (avail == 'OutOfStock').sum())


def _unbuyable_footer(rows) -> str:
    """Footer for a result set with nothing buyable in it. Only offer Notify Me when
    something might actually return; a delisted product never will."""
    out, gone = _split_unbuyable(rows)
    if out and not gone:
        return ("None of these can be bought right now — use **Notify Me** on the "
                "Flipkart listing to be alerted when they're back in stock.")
    if gone and not out:
        return ("None of these are sold any more — they've been delisted, so they "
                "won't come back. Try another brand or a wider price range.")
    return (f"None of these can be bought right now — {out} temporarily out of stock "
            f"(**Notify Me** on the listing will alert you) and {gone} delisted for good.")


def _dedup_rows(df):
    """Collapse seller-variant listings of one product (same shoe, different pid,
    sometimes different price) to a single row — keeping the cheapest — while
    preserving the SQL's ordering. Applied to EVERY result so the short-list path
    dedups too, not just the >5 numbered-list path."""
    if df is None or 'title' not in df.columns or len(df) < 2:
        return df
    df = df.copy()
    df['_dedup_key'] = [
        _dedup_key(t, b)
        for t, b in zip(df['title'], df.get('brand', [None] * len(df)))
    ]
    keep = df.sort_values('price', kind='stable') if 'price' in df.columns else df
    keep = keep.drop_duplicates(subset='_dedup_key', keep='first')

    return df.loc[df.index.isin(keep.index)].drop(columns='_dedup_key')


def _price_age_note(response):
    """Note when prices were last verified.

    Uses the oldest row shown, not the newest, so one fresh product can't make a
    stale list look current. Never claims the prices are live.
    """
    if 'scraped_at' not in response.columns:
        return ""
    ts = pd.to_datetime(response['scraped_at'], errors='coerce', utc=True).min()
    if pd.isna(ts):
        return ""
    days = (pd.Timestamp.now(tz='UTC') - ts).days
    when = "today" if days < 1 else ("yesterday" if days == 1 else f"{days} days ago")
    return (f"\n\n*Prices were last verified {when} and may have changed since — "
            f"check the Flipkart listing before buying.*")


def _format_top_results(response, question=""):
    """Format the rows into a numbered markdown list (no LLM call needed). Rows are
    already seller-deduped upstream in _run_sql_for_question."""
    answer = _header(question, len(response))


    for i, (_, row) in enumerate(response.iterrows(), start=1):
        title = row.get('title', 'Product')
        price = row.get('price', 'N/A')


        rating = row.get('avg_rating')
        if pd.notna(rating):
            n = row.get('total_ratings')
            rating_str = (f", Rating: {rating} ({int(n):,} ratings)"
                          if pd.notna(n) else f", Rating: {rating}")
        else:
            rating_str = ", no ratings yet"
        link = row.get('product_link', '#')

        stock = row.get('availability')
        stock_str = "" if stock in ('InStock', None) else f" — **{stock}**"
        answer += (f"{i}. {title}: Rs. {price}{rating_str}"
                   f"{stock_str} [View Product]({link})\n")


    all_gone = ('availability' in response.columns
                and len(response) > 0
                and not (response['availability'] == 'InStock').any())
    if all_gone:
        answer += "\n\n*" + _unbuyable_footer(response) + "*"


        return answer + _unsupported_note(question)

    return answer + _unsupported_note(question) + _price_age_note(response)


def _extract_sql(raw: str):
    """Pull the SQL out of an LLM response, whatever wrapper it chose.

    The prompt asks for <SQL></SQL>, but the model also emits ```sql fences or a
    bare SELECT — and it switches between them as the prompt changes. Accepting
    only one format meant a perfectly good query was reported to the user as
    "LLM is not able to generate a query for your question": a silent failure
    that looked like the model's fault rather than a parsing bug.
    """
    if not raw:
        return None
    for pattern in (
        r"<SQL>(.*?)</SQL>",
        r"```(?:sql)?\s*(.*?)```",
        r"(SELECT\b.*)",
    ):
        m = re.search(pattern, raw, re.DOTALL | re.I)
        if m:
            sql = m.group(1).strip().rstrip(";").strip()


            if sql.lstrip("(").lstrip().upper().startswith("SELECT"):
                return sql
    return None


def _shortfall_note(requested, got: int) -> str:
    """Say so when the shopper named a count and the catalogue couldn't fill it.

    Only fires for an EXPLICIT count — `requested` is None for a broad search, where
    the 10 is our default, not something the shopper asked for. Deliberately doesn't
    blame stock: a short result can also mean the filters simply match fewer products,
    or that seller duplicates collapsed, and claiming a cause we haven't checked is
    how a helpful line becomes a wrong one."""
    if not requested or got >= requested:
        return ""
    return (f"\n\n*You asked for {requested} — only {got} "
            f"{'product matches' if got == 1 else 'products match'} and "
            f"{'is' if got == 1 else 'are'} available right now.*")


def _run_sql_for_question(question):
    """Shared prefix for sql_chain / sql_chain_stream_async: generate SQL, run it,
    and return (dataframe, error_message) — exactly one is non-None.

    The generated SQL is cached, NOT the rows: a hit skips the slow
    gemini-2.5-pro call but still re-executes against live data, so results
    can never go stale.

    Returns (dataframe, error, requested) — exactly one of dataframe/error is non-None.
    `requested` is the count the shopper explicitly asked for, or None for a broad
    search, so the caller can flag a shortfall without mistaking our own default for
    a request."""
    sql = cache_get("sql", question)
    from_cache = sql is not None
    if not sql:
        raw = generate_sql_query(question)
        sql = _extract_sql(raw)
        if not sql:
            logger.warning("No SQL could be extracted from response: %r", (raw or "")[:200])
            return None, "Sorry, LLM is not able to generate a query for your question", None
    if re.search(r"\bunion\b", sql, re.I):


        branch_limits = [int(n) for n in re.findall(r"\blimit\s+(\d+)", sql, re.I)]
        requested = sum(branch_limits) if branch_limits else None
        display_n = requested or DEFAULT_DISPLAY_ROWS
        fetch_sql = sql.strip().rstrip("; ")
    else:


        m = re.search(r"\blimit\s+(\d+)\s*;?\s*$", sql.strip(), re.I)
        requested = int(m.group(1)) if m else None
        display_n = requested or DEFAULT_DISPLAY_ROWS

        fetch_sql = re.sub(r"\blimit\s+\d+\s*;?\s*$", "", sql.strip(), flags=re.I).rstrip("; ") + f" LIMIT {display_n * 2}"
    logger.debug("SQL (buffered): %s", fetch_sql)
    response = run_query(fetch_sql)
    if response is None:


        return None, ("I couldn't run that search — it may be too complex. Try asking for one "
                      "thing at a time, e.g. \"4 Nike shoes\" then \"5 Puma shoes\"."), None

    if not from_cache:
        cache_set("sql", question, sql)
    if response.empty:


        sentinel = re.search(r"\b1\s*=\s*0\b", sql or "")
        blocked = {k for k, pat in _NOT_SEARCHABLE.items() if re.search(pat, (question or "").lower())}
        if sentinel and blocked:
            return None, (f"I can't search by {_join(blocked)} — the catalogue only records "
                          f"title, brand, price, rating and stock. Try searching by brand, "
                          f"price or rating instead, e.g. \"Nike shoes under 3000\"."), None
        if sentinel:
            return None, ("I couldn't find any products matching that. This catalogue only "
                          "covers footwear — shoes, sneakers and boots — so I can't search "
                          "other product types."), None

        return None, explain_no_results(question, sql), None
    return _dedup_rows(response).head(display_n), None, requested


def sql_chain(question):
    response, error, requested = _run_sql_for_question(question)
    if error:
        return error
    shortfall = _shortfall_note(requested, len(response))
    if len(response) > 5:
        return _format_top_results(response, question) + shortfall
    context = response.to_dict(orient='records')
    logger.debug("Sending context to Gemini for conversational formatting: %s", context)


    return data_comprehension(question, context) + _unsupported_note(question) + shortfall


async def sql_chain_stream_async(question):
    """Async streaming variant. SQL generation + execution (sync) run in a thread;
    the conversational reply streams via the async client."""
    response, error, requested = await asyncio.to_thread(_run_sql_for_question, question)
    if error:
        yield error
        return
    shortfall = _shortfall_note(requested, len(response))
    if len(response) > 5:
        yield _format_top_results(response, question) + shortfall
        return
    context = response.to_dict(orient='records')
    try:
        async for tok in llm_stream(f"QUESTION: {question}. DATA: {context}",
                                    system=comprehension_prompt, temperature=0.2,
                                    model=COMPREHENSION_MODEL):
            yield tok
    except Exception as e:
        logger.error("SQL comprehension stream error: %s", e)
        yield "Sorry, there was a problem formatting the results."
        return


    note = _unsupported_note(question) + shortfall
    if note:
        yield note


if __name__ == "__main__":
    question = "Show top 3 shoes in descending order of rating"
    answer = sql_chain(question)
    logger.info(answer)
