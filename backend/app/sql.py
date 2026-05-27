from google import genai
import os
import re
import asyncio
import logging
from sqlalchemy import create_engine, text
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
from pandas import DataFrame
from pathlib import Path

logger = logging.getLogger(__name__)

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# SQL generation. Flash produces the SAME result set as Pro on every eval case
# (16/16 verified: gender filter, relative comparison, Bayesian ranking, colour/
# size no-op, out-of-catalogue) at ~1.6s vs ~4.5s — a ~3s cut on every product
# query. Pro's edge disappeared once the prompt gained an explicit template for
# each hard case, which is what lets a smaller model get them right.
GEMINI_MODEL = 'gemini-2.5-flash'

from app.db.database import readonly_engine
from app.llm_utils import with_retry
from app.cache import cache_get, cache_set

client_sql = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
FALLBACK_MODEL = 'gemini-2.5-pro'  # only if Flash errors or is rate-limited
# Turning rows into prose doesn't need Pro either — same reasoning.
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
than a 4.6 from 500, and a 4.7 from 50 is not better rated than a 4.6 from 509 —
the raw average alone gives a tiny sample too much weight. Rank by a
confidence-weighted (Bayesian) score so a strong average backed by many ratings
beats a slightly higher average from a handful. For any question about top / best
/ highest rated shoes, AND for rating THRESHOLD questions ("rated higher than 4.5"):
    WHERE avg_rating IS NOT NULL AND total_ratings >= 50
    ORDER BY ( total_ratings::numeric / (total_ratings + 50) * avg_rating
             + 50.0 / (total_ratings + 50) * 4.1 ) DESC
Here 4.1 is the catalogue's average rating and 50 a confidence prior: a shoe needs
enough ratings to pull its score away from that average. For a threshold question
KEEP the user's cutoff in WHERE (e.g. `AND avg_rating > 4.5`) and still order by
that weighted score. The only exception is when the user sets their own
rating-count condition — then honour exactly what they asked.

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
Create a single SQL query for the question provided. 
The query should have all the fields in SELECT clause (i.e. SELECT *)

Just the SQL query is needed, nothing more. Always provide the SQL in between the <SQL></SQL> tags."""


comprehension_prompt = """You are an expert in understanding the context of the question and replying based on the data pertaining to the question provided. You will be provided with Question: and Data:. The data will be in the form of an array or a dataframe or dict. Reply based on only the data provided as Data for answering the question asked as Question. Do not write anything like 'Based on the data' or any other technical words. Just a plain simple natural language response.
The Data would always be in context to the question asked. For example is the question is “What is the average rating?” and data is “4.3”, then answer should be “The average rating for the product is 4.3”. So make sure the response is curated with the question and data. Make sure to note the column names to have some context, if needed, for your response.
There can also be cases where you are given an entire dataframe in the Data: field. Always remember that the data field contains the answer of the question asked. All you need to do is to always reply in the following format when asked about a product: 
Product title, price in indian rupees, rating WITH its rating count, and then product link as a clickable markdown link. Take care that all the products are listed in list format, one line after the other. Not as a paragraph.
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
    client = client_sql

    def _gen(model):
        return client.models.generate_content(
            model=model,
            contents=question,
            config=genai.types.GenerateContentConfig(
                system_instruction=sql_prompt,
                temperature=0.2,
            )
        ).text

    try:
        return with_retry(_gen, GEMINI_MODEL)
    except Exception as e:
        # Pro exhausted/unavailable after retries -> fall back to Flash so the user still gets an answer.
        logger.warning("SQL generation on %s failed, falling back to %s: %s", GEMINI_MODEL, FALLBACK_MODEL, e)
        return with_retry(_gen, FALLBACK_MODEL)



def run_query(query):
    if query.strip().upper().startswith('SELECT'):
        with readonly_engine.connect() as conn:
            df = pd.read_sql_query(text(query), conn)
            return df


def data_comprehension(question, context):
    client = client_sql

    def _gen():
        return client.models.generate_content(
            model=COMPREHENSION_MODEL,
            contents=f"QUESTION: {question}. DATA: {context}",
            config=genai.types.GenerateContentConfig(
                system_instruction=comprehension_prompt,
                temperature=0.2,
            )
        ).text

    return with_retry(_gen)


# Attributes the catalogue has no column for. Split by whether matching the word
# against the TITLE is honest or misleading:
#   NOT_SEARCHABLE - matching produces false hits ('red' -> brand RED TAPE) or
#                    nothing at all (no row records a size). Never matched.
#   TITLE_ONLY     - sellers put these in the title, so a title match is genuinely
#                    useful; it just isn't a verified attribute.
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
        # Deliberately does NOT claim the results "match the rest of your request"
        # — when colour and size WERE the whole request, there is no rest.
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


# "Do you have Puma sneakers?" deserves a yes, not a bare list the reader has to
# infer the yes from.
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
    """Collapse seller variants of one product to a single key."""
    t = str(title or "").lower()
    b = str(brand or "").lower()
    if b and t.startswith(b):
        t = t[len(b):]
    t = re.sub(r"\b[a-z]\b", " ", t)      # stray single letters: the "W" in "NIKE W REVOLUTION 7"
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t or str(title or "").lower()


def _price_age_note(response):
    """Say when prices were last verified — and that they may have moved since.

    Two things this gets right that the first version didn't:
      * uses the OLDEST row shown, not the newest. max() meant one freshly
        checked product could make a list of month-old prices claim "today".
      * never claims currency. "Prices checked today" reads as a guarantee that
        these are the live prices, which a periodic scrape cannot support —
        Flipkart can change a price a minute after we read it.
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
    """Format >5 rows into a numbered markdown list (no LLM call needed)."""
    # The same shoe is listed by several sellers under slightly different titles —
    # "Revolution 7...", "NIKE Revolution 7...", "NIKE W REVOLUTION 7..." are one
    # product and were eating three of ten slots. Exact-title dedup missed them,
    # so normalise first: drop the brand prefix, stray single letters (the "W"),
    # and all punctuation.
    if 'title' in response.columns:
        response = response.copy()
        response['_dedup_key'] = [
            _dedup_key(t, b)
            for t, b in zip(response['title'], response.get('brand', [None] * len(response)))
        ]
        deduped = response
        if 'price' in response.columns:
            deduped = deduped.sort_values('price', kind='stable')
        deduped = deduped.drop_duplicates(subset='_dedup_key', keep='first')
        # preserve the ordering the SQL asked for (rating, price, ...)
        response = response.loc[response.index.isin(deduped.index)]

    answer = _header(question, len(response))
    for i, (_, row) in enumerate(response.head(10).iterrows(), start=1):
        title = row.get('title', 'Product')
        price = row.get('price', 'N/A')
        # Newly listed products often have no ratings yet — don't print "nan".
        # Always show the COUNT alongside the score: 5.0 from 3 reviews and 4.4
        # from 60,000 are not comparable, and the number is what makes that visible.
        rating = row.get('avg_rating')
        if pd.notna(rating):
            n = row.get('total_ratings')
            rating_str = (f", Rating: {rating} ({int(n):,} ratings)"
                          if pd.notna(n) else f", Rating: {rating}")
        else:
            rating_str = ", no ratings yet"
        link = row.get('product_link', '#')
        # Normally everything here is in stock (the prompt filters on it), but the
        # "is X available?" path deliberately doesn't — so never imply buyable.
        stock = row.get('availability')
        stock_str = "" if stock in ('InStock', None) else f" — **{stock}**"
        answer += (f"{i}. {title}: Rs. {price}{rating_str}"
                   f"{stock_str} [View Product]({link})\n")
    if len(response) > 10:
        answer += f"\n*(Showing 10 of {len(response)} results)*"

    # If nothing here can be bought, offer the thing that actually helps rather
    # than nagging about price currency on unbuyable listings.
    all_gone = ('availability' in response.columns
                and len(response) > 0
                and not (response['availability'] == 'InStock').any())
    if all_gone:
        answer += ("\n\n*None of these can be bought right now — use **Notify Me** "
                   "on the Flipkart listing to be alerted when they're back in stock.*")
        # and skip the price-currency footer: nagging about price accuracy on
        # products nobody can buy is noise.
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
        r"<SQL>(.*?)</SQL>",              # requested format
        r"```(?:sql)?\s*(.*?)```",        # markdown fence
        r"(SELECT\b.*)",                  # bare statement, last resort
    ):
        m = re.search(pattern, raw, re.DOTALL | re.I)
        if m:
            sql = m.group(1).strip().rstrip(";").strip()
            if sql.upper().startswith("SELECT"):
                return sql
    return None


def _run_sql_for_question(question):
    """Shared prefix for sql_chain / sql_chain_stream_async: generate SQL, run it,
    and return (dataframe, error_message) — exactly one is non-None.

    The generated SQL is cached, NOT the rows: a hit skips the slow
    gemini-2.5-pro call but still re-executes against live data, so results
    can never go stale."""
    sql = cache_get("sql", question)
    if not sql:
        raw = generate_sql_query(question)
        sql = _extract_sql(raw)
        if not sql:
            logger.warning("No SQL could be extracted from response: %r", (raw or "")[:200])
            return None, "Sorry, LLM is not able to generate a query for your question"
        cache_set("sql", question, sql)  # only cache a successful extraction
    logger.debug("SQL: %s", sql)
    response = run_query(sql)
    if response is None:
        return None, "Sorry, there was a problem executing SQL query"
    if response.empty:
        # Three reasons a query comes back empty, and they need different answers.
        # The model emits `WHERE 1=0` deliberately for requests it can't serve
        # (out-of-catalogue, or a pure colour/size ask) — a real search that just
        # matched nothing has a normal WHERE. Don't lecture the latter about the
        # catalogue; tell them to broaden.
        sentinel = re.search(r"\b1\s*=\s*0\b", sql or "")
        blocked = {k for k, pat in _NOT_SEARCHABLE.items() if re.search(pat, (question or "").lower())}
        if sentinel and blocked:
            return None, (f"I can't search by {_join(blocked)} — the catalogue only records "
                          f"title, brand, price, rating and stock. Try searching by brand, "
                          f"price or rating instead, e.g. \"Nike shoes under 3000\".")
        if sentinel:
            return None, ("I couldn't find any products matching that. This catalogue only "
                          "covers footwear — shoes, sneakers and boots — so I can't search "
                          "other product types.")
        return None, ("I couldn't find any products matching that. Try broadening your "
                      "search — a different brand, a higher price, or fewer conditions.")
    return response, None


def sql_chain(question):
    response, error = _run_sql_for_question(question)
    if error:
        return error
    if len(response) > 5:
        return _format_top_results(response, question)
    context = response.to_dict(orient='records')
    logger.debug("Sending context to Gemini for conversational formatting: %s", context)
    # small result sets are phrased by the LLM; still disclose unmatched filters
    return data_comprehension(question, context) + _unsupported_note(question)


async def sql_chain_stream_async(question):
    """Async streaming variant. SQL generation + execution (sync) run in a thread;
    the conversational reply streams via the async client."""
    response, error = await asyncio.to_thread(_run_sql_for_question, question)
    if error:
        yield error
        return
    if len(response) > 5:
        yield _format_top_results(response, question)
        return
    context = response.to_dict(orient='records')
    client = client_sql
    try:
        stream = await client.aio.models.generate_content_stream(
            model=COMPREHENSION_MODEL,
            contents=f"QUESTION: {question}. DATA: {context}",
            config=genai.types.GenerateContentConfig(
                system_instruction=comprehension_prompt,
                temperature=0.2,
            ),
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text
    except Exception as e:
        logger.error("SQL comprehension stream error: %s", e)
        yield "Sorry, there was a problem formatting the results."
        return
    # disclose any filter we couldn't apply, same as the list path
    note = _unsupported_note(question)
    if note:
        yield note


if __name__ == "__main__":
    # question = "All shoes with rating higher than 4.5 and total number of reviews greater than 500"
    # sql_query = generate_sql_query(question)
    # logger.info(sql_query)
    question = "Show top 3 shoes in descending order of rating"
    answer = sql_chain(question)
    logger.info(answer)
