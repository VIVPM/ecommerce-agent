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

GEMINI_MODEL = 'gemini-2.5-pro'

from app.db.database import readonly_engine
from app.llm_utils import with_retry
from app.cache import cache_get, cache_set

client_sql = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
FALLBACK_MODEL = 'gemini-2.5-flash'

sql_prompt = """You are an expert in understanding the database schema and generating SQL queries for a natural language question asked
pertaining to the data you have. The schema is provided in the schema tags. 
<schema> 
table: product 

fields: 
product_link - string (hyperlink to product)	
title - string (name of the product)	
brand - string (brand of the product)	
price - integer (price of the product in Indian Rupees)	
discount - float (discount on the product. 10 percent discount is represented as 0.1, 20 percent as 0.2, and such.)	
avg_rating - float (average rating of the product. Range 0-5, 5 is the highest.)
total_ratings - integer (total number of ratings for the product)
availability - string ('InStock' = can be bought now, 'OutOfStock' = listed but unbuyable, 'Unavailable' = delisted)
scraped_at - timestamp (when this row's price/rating was last verified against Flipkart)

</schema>
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
Product title, price in indian rupees, discount, and rating, and then product link as a clickable markdown link. Take care that all the products are listed in list format, one line after the other. Not as a paragraph.
IMPORTANT: Always format product links as markdown links like [View Product](url). Never paste raw URLs.
For example:
1. Campus Women Running Shoes: Rs. 1104 (35 percent off), Rating: 4.4 [View Product](https://www.flipkart.com/...)
2. Campus Women Running Shoes: Rs. 1104 (35 percent off), Rating: 4.4 [View Product](https://www.flipkart.com/...)
3. Campus Women Running Shoes: Rs. 1104 (35 percent off), Rating: 4.4 [View Product](https://www.flipkart.com/...)

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
            model=GEMINI_MODEL,
            contents=f"QUESTION: {question}. DATA: {context}",
            config=genai.types.GenerateContentConfig(
                system_instruction=comprehension_prompt,
                temperature=0.2,
            )
        ).text

    return with_retry(_gen)


def _price_age_note(response):
    """Prices are a point-in-time snapshot, so say how old they are — a stale
    quote presented flatly reads as authoritative when it isn't."""
    if 'scraped_at' not in response.columns:
        return ""
    ts = pd.to_datetime(response['scraped_at'], errors='coerce', utc=True).max()
    if pd.isna(ts):
        return ""
    days = (pd.Timestamp.now(tz='UTC') - ts).days
    if days < 1:
        return "\n\n*Prices checked today.*"
    return (f"\n\n*Prices last checked {days} day{'s' if days != 1 else ''} ago — "
            f"see Flipkart for the current price.*")


def _format_top_results(response):
    """Format >5 rows into a numbered markdown list (no LLM call needed)."""
    answer = "Here are the top results from your search:\n"
    for i, (_, row) in enumerate(response.head(10).iterrows(), start=1):
        title = row.get('title', 'Product')
        price = row.get('price', 'N/A')
        discount_val = row.get('discount', 0)
        discount_str = f" ({int(discount_val * 100)}% off)" if discount_val else ""
        # Newly listed products often have no ratings yet — don't print "nan".
        rating = row.get('avg_rating')
        rating_str = f", Rating: {rating}" if pd.notna(rating) else ", no ratings yet"
        link = row.get('product_link', '#')
        # Normally everything here is in stock (the prompt filters on it), but the
        # "is X available?" path deliberately doesn't — so never imply buyable.
        stock = row.get('availability')
        stock_str = "" if stock in ('InStock', None) else f" — **{stock}**"
        answer += (f"{i}. {title}: Rs. {price}{discount_str}{rating_str}"
                   f"{stock_str} [View Product]({link})\n")
    if len(response) > 10:
        answer += f"\n*(Showing 10 of {len(response)} results)*"
    return answer + _price_age_note(response)


def _run_sql_for_question(question):
    """Shared prefix for sql_chain / sql_chain_stream_async: generate SQL, run it,
    and return (dataframe, error_message) — exactly one is non-None.

    The generated SQL is cached, NOT the rows: a hit skips the slow
    gemini-2.5-pro call but still re-executes against live data, so results
    can never go stale."""
    sql = cache_get("sql", question)
    if not sql:
        raw = generate_sql_query(question)
        matches = re.findall("<SQL>(.*?)</SQL>", raw, re.DOTALL)
        if len(matches) == 0:
            return None, "Sorry, LLM is not able to generate a query for your question"
        sql = matches[0].strip()
        cache_set("sql", question, sql)  # only cache a successful extraction
    logger.debug("SQL: %s", sql)
    response = run_query(sql)
    if response is None:
        return None, "Sorry, there was a problem executing SQL query"
    if response.empty:
        return None, "I could not find any products matching your criteria in our database."
    return response, None


def sql_chain(question):
    response, error = _run_sql_for_question(question)
    if error:
        return error
    if len(response) > 5:
        return _format_top_results(response)
    context = response.to_dict(orient='records')
    logger.debug("Sending context to Gemini for conversational formatting: %s", context)
    return data_comprehension(question, context)


async def sql_chain_stream_async(question):
    """Async streaming variant. SQL generation + execution (sync) run in a thread;
    the conversational reply streams via the async client."""
    response, error = await asyncio.to_thread(_run_sql_for_question, question)
    if error:
        yield error
        return
    if len(response) > 5:
        yield _format_top_results(response)
        return
    context = response.to_dict(orient='records')
    client = client_sql
    try:
        stream = await client.aio.models.generate_content_stream(
            model=GEMINI_MODEL,
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


if __name__ == "__main__":
    # question = "All shoes with rating higher than 4.5 and total number of reviews greater than 500"
    # sql_query = generate_sql_query(question)
    # logger.info(sql_query)
    question = "Show top 3 shoes in descending order of rating"
    answer = sql_chain(question)
    logger.info(answer)
