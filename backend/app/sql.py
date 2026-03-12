from google import genai
import os
import re
from sqlalchemy import create_engine, text
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
from pandas import DataFrame
from pathlib import Path

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

GEMINI_MODEL = 'gemini-2.5-flash'

neon_db_url = os.getenv('DATABASE_URL')
engine = create_engine(neon_db_url)

client_sql = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

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

</schema>
CRITICAL RULE: The dataset ONLY contains shoes. If the user asks about "shoes", DO NOT add a SQL filter for `title LIKE '%shoe%'` or `title LIKE '%shoes%'`. This will incorrectly filter out shoes that do not have the word "shoe" in their title. Completely ignore the word "shoe" when constructing your WHERE clauses.
Make sure whenever you try to search for the brand name, the name can be in any case. 
So, make sure to use %LIKE% to find the brand in condition. Never use "ILIKE". 
Create a single SQL query for the question provided. 
The query should have all the fields in SELECT clause (i.e. SELECT *)

Just the SQL query is needed, nothing more. Always provide the SQL in between the <SQL></SQL> tags."""


comprehension_prompt = """You are an expert in understanding the context of the question and replying based on the data pertaining to the question provided. You will be provided with Question: and Data:. The data will be in the form of an array or a dataframe or dict. Reply based on only the data provided as Data for answering the question asked as Question. Do not write anything like 'Based on the data' or any other technical words. Just a plain simple natural language response.
The Data would always be in context to the question asked. For example is the question is “What is the average rating?” and data is “4.3”, then answer should be “The average rating for the product is 4.3”. So make sure the response is curated with the question and data. Make sure to note the column names to have some context, if needed, for your response.
There can also be cases where you are given an entire dataframe in the Data: field. Always remember that the data field contains the answer of the question asked. All you need to do is to always reply in the following format when asked about a product: 
Produt title, price in indian rupees, discount, and rating, and then product link. Take care that all the products are listed in list format, one line after the other. Not as a paragraph.
For example:
1. Campus Women Running Shoes: Rs. 1104 (35 percent off), Rating: 4.4 <link>
2. Campus Women Running Shoes: Rs. 1104 (35 percent off), Rating: 4.4 <link>
3. Campus Women Running Shoes: Rs. 1104 (35 percent off), Rating: 4.4 <link>

"""


def generate_sql_query(question):
    chat_completion = client_sql.models.generate_content(
        model=GEMINI_MODEL,
        contents=question,
        config=genai.types.GenerateContentConfig(
            system_instruction=sql_prompt,
            temperature=0.2,
        )
    )

    return chat_completion.text



def run_query(query):
    if query.strip().upper().startswith('SELECT'):
        with engine.connect() as conn:
            df = pd.read_sql_query(text(query), conn)
            return df


def data_comprehension(question, context):
    chat_completion = client_sql.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"QUESTION: {question}. DATA: {context}",
        config=genai.types.GenerateContentConfig(
            system_instruction=comprehension_prompt,
            temperature=0.2,
        )
    )

    return chat_completion.text



def sql_chain(question):
    sql_query = generate_sql_query(question)
    pattern = "<SQL>(.*?)</SQL>"
    matches = re.findall(pattern, sql_query, re.DOTALL)

    if len(matches) == 0:
        return "Sorry, LLM is not able to generate a query for your question"

    print(matches[0].strip())

    response = run_query(matches[0].strip())
    if response is None:
        return "Sorry, there was a problem executing SQL query"
    
    if response.empty:
        return "I could not find any products matching your criteria in our database."

    if len(response) > 5:
        # Optimization: Don't send massive datasets to the LLM for formatting, it takes too long.
        # Format it natively in Python instead.
        answer = "Here are the top results from your search:\n"
        for _, row in response.head(10).iterrows(): # Show top 10 max
            title = row.get('title', 'Product')
            price = row.get('price', 'N/A')
            discount_val = row.get('discount', 0)
            if discount_val:
                discount_str = f" ({int(discount_val * 100)}% off)"
            else:
                discount_str = ""
            rating = row.get('avg_rating', 'N/A')
            link = row.get('product_link', '#')
            
            answer += f"1. {title}: Rs. {price}{discount_str}, Rating: {rating} [Link]({link})\n"
            
        if len(response) > 10:
            answer += f"\n*(Showing 10 of {len(response)} results)*"
        return answer

    context = response.to_dict(orient='records')
    print("Sending context to Gemini for conversational formatting:", context)
    print()
    answer = data_comprehension(question, context)
    return answer


if __name__ == "__main__":
    # question = "All shoes with rating higher than 4.5 and total number of reviews greater than 500"
    # sql_query = generate_sql_query(question)
    # print(sql_query)
    question = "Show top 3 shoes in descending order of rating"
    answer = sql_chain(question)
    print(answer)
