import json
import os
import sys
import time
import re
import logging
from typing import Dict
import pandas as pd
from sqlalchemy import create_engine, text
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pathlib import Path

# Fix console encoding
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

env_path = Path(__file__).resolve().parent.parent / "app" / ".env"
load_dotenv(dotenv_path=env_path)

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.db.database import readonly_engine
# We still need pinecone index for FAQ from current app state
from app.faq import get_relevant_qa

client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

BASE_DIR = os.path.dirname(__file__)
QUESTIONS_FILE = os.path.join(BASE_DIR, 'test_questions_200.json')
RESULTS_FILE = os.path.join(BASE_DIR, 'evaluation_results_baseline.json')
SUMMARY_FILE = os.path.join(BASE_DIR, 'evaluation_summary_baseline.json')
RUBRIC_FILE = os.path.join(BASE_DIR, 'eval_rubric.md')
BATCH = 15

# ==========================================
# OLD PROMPTS & LOGIC (From commit 55447c5)
# ==========================================

AGENT_INSTRUCTION = """
    You are an intelligent e-commerce routing agent. Your ONLY job is to analyze the user's query 
    and call the most appropriate tool (`search_product_database` or `search_faq_knowledge_base`).
    You must NOT attempt to answer the user's question directly. Always invoke a tool.
    Pass the user's EXACT query string into the tool you select.
    """

SQL_PROMPT = """You are an expert in understanding the database schema and generating SQL queries for a natural language question asked
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
IMPORTANT: Brand names in the database are inconsistent (e.g. "NIKE", "Nike", "nike").
Always use LOWER() on both sides for case-insensitive matching: LOWER(brand) LIKE LOWER('%nike%').
Apply the same LOWER() pattern for title searches too. Never use "ILIKE".
Create a single SQL query for the question provided. 
The query should have all the fields in SELECT clause (i.e. SELECT *)

Just the SQL query is needed, nothing more. Always provide the SQL in between the <SQL></SQL> tags."""

COMPREHENSION_PROMPT = """You are an expert in understanding the context of the question and replying based on the data pertaining to the question provided. You will be provided with Question: and Data:. The data will be in the form of an array or a dataframe or dict. Reply based on only the data provided as Data for answering the question asked as Question. Do not write anything like 'Based on the data' or any other technical words. Just a plain simple natural language response.
The Data would always be in context to the question asked. For example is the question is “What is the average rating?” and data is “4.3”, then answer should be “The average rating for the product is 4.3”. So make sure the response is curated with the question and data. Make sure to note the column names to have some context, if needed, for your response.
There can also be cases where you are given an entire dataframe in the Data: field. Always remember that the data field contains the answer of the question asked. All you need to do is to always reply in the following format when asked about a product: 
Product title, price in indian rupees, discount, and rating, and then product link as a clickable markdown link. Take care that all the products are listed in list format, one line after the other. Not as a paragraph.
IMPORTANT: Always format product links as markdown links like [View Product](url). Never paste raw URLs.
For example:
1. Campus Women Running Shoes: Rs. 1104 (35 percent off), Rating: 4.4 [View Product](https://www.flipkart.com/...)
2. Campus Women Running Shoes: Rs. 1104 (35 percent off), Rating: 4.4 [View Product](https://www.flipkart.com/...)
3. Campus Women Running Shoes: Rs. 1104 (35 percent off), Rating: 4.4 [View Product](https://www.flipkart.com/...)

"""

# Mock tools
def old_search_product_database(query: str) -> str:
    # SQL gen uses Pro
    chat_completion = client.models.generate_content(
        model='gemini-2.5-pro',
        contents=query,
        config=types.GenerateContentConfig(
            system_instruction=SQL_PROMPT,
            temperature=0.2,
        )
    )
    sql_query = chat_completion.text
    pattern = "<SQL>(.*?)</SQL>"
    matches = re.findall(pattern, sql_query, re.DOTALL)
    if not matches:
        return "Sorry, LLM is not able to generate a query for your question"
    
    query_str = matches[0].strip()
    if query_str.upper().startswith('SELECT'):
        with readonly_engine.connect() as conn:
            response = pd.read_sql_query(text(query_str), conn)
    else:
        response = None
        
    if response is None:
        return "Sorry, there was a problem executing SQL query"
    if response.empty:
        return "I could not find any products matching your criteria in our database."

    if len(response) > 5:
        answer = "Here are the top results from your search:\n"
        for _, row in response.head(10).iterrows():
            title = row.get('title', 'Product')
            price = row.get('price', 'N/A')
            discount_val = row.get('discount', 0)
            if discount_val:
                discount_str = f" ({int(discount_val * 100)}% off)"
            else:
                discount_str = ""
            rating = row.get('avg_rating', 'N/A')
            link = row.get('product_link', '#')
            answer += f"1. {title}: Rs. {price}{discount_str}, Rating: {rating} [View Product]({link})\n"

        if len(response) > 10:
            answer += f"\n*(Showing 10 of {len(response)} results)*"
        return answer

    context = response.to_dict(orient='records')
    # Comprehension uses Pro
    chat_completion = client.models.generate_content(
        model='gemini-2.5-pro',
        contents=f"QUESTION: {query}. DATA: {context}",
        config=types.GenerateContentConfig(
            system_instruction=COMPREHENSION_PROMPT,
            temperature=0.2,
        )
    )
    return chat_completion.text


def old_search_faq_knowledge_base(query: str) -> str:
    # Reuse pinecone logic but use old prompt + Flash
    docs = get_relevant_qa(query)
    if not docs:
        return "I am unable to answer your question right now because the FAQ data is not processed. Please contact support."

    context = "\n".join([f"- {d.metadata.get('answer', '')}" for d in docs])
    prompt = f'''You are a helpful customer support assistant for an e-commerce store.
        Answer the user's question using ONLY the FAQ context provided below.
        The context contains relevant FAQ answers — use them to form a helpful, natural response.
        Only say "I don't know" if the context is completely unrelated to the question.
        
        FAQ CONTEXT:
        {context}
        
        CUSTOMER QUESTION: {query}
        '''
    completion = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
        )
    )
    return completion.text

def run_baseline_agent(optimized_query: str) -> str:
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=optimized_query,
            config=types.GenerateContentConfig(
                system_instruction=AGENT_INSTRUCTION,
                tools=[old_search_product_database, old_search_faq_knowledge_base],
                temperature=0.0,
            )
        )
        if response.function_calls:
            call = response.function_calls[0]
            function_name = call.name
            args = call.args
            query_arg = args.get('query', optimized_query)
            
            if function_name == 'old_search_product_database':
                return old_search_product_database(query_arg)
            elif function_name == 'old_search_faq_knowledge_base':
                return old_search_faq_knowledge_base(query_arg)
                
        return response.text if response.text else "I'm sorry, I encountered an issue routing your request."
    except Exception as e:
        return f"I'm sorry, my reasoning engine encountered a technical error: {e}"

# ==========================================
# JUDGE EVALUATION
# ==========================================

def load_rubric():
    with open(RUBRIC_FILE, 'r', encoding='utf-8') as f:
        return f.read()

def load_questions():
    with open(QUESTIONS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def judge_response(question: str, category: str, response: str, rubric: str) -> Dict:
    prompt = f"""
    You are an expert LLM judge. Evaluate the following E-commerce Agent response based on the provided rubric.

    User Question: {question}
    Question Category: {category}
    Agent Response: {response}

    Evaluation Rubric:
    {rubric}

    Return your evaluation in the following JSON format ONLY:
    {{
        "routing_accuracy": "Pass" or "Fail",
        "faithfulness": score 1-5,
        "relevance": score 1-5,
        "reasoning": "Brief explanation for the scores"
    }}
    """
    try:
        eval_resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(eval_resp.text)
    except Exception as e:
        return {"routing_accuracy": "Error", "faithfulness": 0, "relevance": 0, "reasoning": str(e)}

def save(results):
    tmp = RESULTS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    os.replace(tmp, RESULTS_FILE)

def summarize(results, total):
    graded = [r for r in results if r['evaluation'].get('routing_accuracy') in ('Pass', 'Fail')]
    n = len(graded) or 1
    return {
        "total_questions": total,
        "graded": len(graded),
        "routing_accuracy_rate": round(sum(1 for r in graded if r['evaluation']['routing_accuracy'] == "Pass") / n, 4),
        "avg_faithfulness": round(sum(r['evaluation']['faithfulness'] for r in graded) / n, 3),
        "avg_relevance": round(sum(r['evaluation']['relevance'] for r in graded) / n, 3),
        "avg_response_time": round(sum(r['duration'] for r in results) / (len(results) or 1), 2),
    }

import concurrent.futures
from threading import Lock

def process_question(q, rubric, total, results_list, results_lock, done_set, start_time):
    if q['id'] in done_set:
        return
    t0 = time.time()
    try:
        agent_response = run_baseline_agent(q['question'])
        evaluation = judge_response(q['question'], q['category'], agent_response, rubric)
    except Exception as e:
        agent_response = "Error"
        evaluation = {"routing_accuracy": "Error", "faithfulness": 0, "relevance": 0, "reasoning": str(e)}

    duration = round(time.time() - t0, 2)
    
    with results_lock:
        results_list.append({
            "id": q['id'], "category": q['category'], "question": q['question'],
            "agent_response": agent_response, "evaluation": evaluation,
            "duration": duration,
        })
        save(results_list)
        n = len(results_list)
        ev = evaluation
        print(f"[{n:>3}/{total}] {q['category']:<10} route={ev.get('routing_accuracy'):<5} "
              f"F{ev.get('faithfulness')} R{ev.get('relevance')}  T{duration}s  {q['question'][:44]}")
        
        if n % 25 == 0:
            s = summarize(results_list, total)
            elapsed = round(time.time() - start_time, 1)
            print(f"  ── progress: routing {s['routing_accuracy_rate']:.1%} · "
                  f"faith {s['avg_faithfulness']:.2f} · rel {s['avg_relevance']:.2f} · "
                  f"elapsed: {elapsed}s ──")

def main():
    questions = load_questions()
    rubric = load_rubric()
    total = len(questions)

    results, done = [], set()
    if os.path.exists(RESULTS_FILE):
        try:
            results = json.load(open(RESULTS_FILE, encoding='utf-8'))
            done = {r['id'] for r in results}
        except Exception:
            pass
    print(f"🚀 Baseline Eval: {len(done)}/{total} already done, {total - len(done)} to go\n" if done
          else f"🚀 Baseline Eval: {total} cases, concurrent workers=20\n")

    results_lock = Lock()
    start_time = time.time()
    
    # max_workers=20 is chosen to stay well under the max_overflow=20 + pool_size=10
    # database connection pool limit, to avoid SQLAlchemy QueuePool timeout errors.
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = []
        for q in questions:
            futures.append(executor.submit(process_question, q, rubric, total, results, results_lock, done, start_time))
        concurrent.futures.wait(futures)

    summary = summarize(results, total)
    with open(SUMMARY_FILE, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    print(f"\n✅ Baseline Eval Complete. {summary}")

if __name__ == "__main__":
    main()
