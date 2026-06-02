import json
import os
import sys
import time
import logging
from typing import Dict
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pathlib import Path
import concurrent.futures
from threading import Lock

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

env_path = Path(__file__).resolve().parent.parent / "app" / ".env"
load_dotenv(dotenv_path=env_path)

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# IMPORT THE CURRENT LIVE (TUNED) AGENT
from app.agent import run_agent

client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

BASE_DIR = os.path.dirname(__file__)
QUESTIONS_FILE = os.path.join(BASE_DIR, 'test_questions_200.json')
RESULTS_FILE = os.path.join(BASE_DIR, 'evaluation_results_tuned.json')
SUMMARY_FILE = os.path.join(BASE_DIR, 'evaluation_summary_tuned.json')
RUBRIC_FILE = os.path.join(BASE_DIR, 'eval_rubric.md')

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

def process_question(q, rubric, total, results_list, results_lock, done_set, start_time):
    if q['id'] in done_set:
        return
    t0 = time.time()
    try:
        # USE THE LIVE FINE-TUNED AGENT (WHICH USES FLASH FOR BOTH FAQ AND SQL)
        agent_response = run_agent(q['question'])
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
    print(f"🚀 Tuned Eval: {len(done)}/{total} already done, {total - len(done)} to go\n" if done
          else f"🚀 Tuned Eval: {total} cases, concurrent workers=20\n")

    results_lock = Lock()
    start_time = time.time()
    
    # max_workers=20 is chosen to stay well under the database connection pool limit (30)
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = []
        for q in questions:
            futures.append(executor.submit(process_question, q, rubric, total, results, results_lock, done, start_time))
        concurrent.futures.wait(futures)

    summary = summarize(results, total)
    with open(SUMMARY_FILE, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    print(f"\n✅ Tuned Eval Complete. {summary}")

if __name__ == "__main__":
    main()
