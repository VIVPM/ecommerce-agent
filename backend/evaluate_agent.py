"""LLM-as-a-Judge evaluation over test_questions.json (150 cases: FAQ, SQL, Edge).

Runs the real agent (route + tool) on each question, then a Flash judge scores
routing / faithfulness / relevance against eval_rubric.md.

Resumable in batches of 15: results are written after every case, so if the run
is interrupted (e.g. a background-task time cap) just run it again and it picks
up where it left off. Delete evaluation_results.json to start fresh.

    python evaluate_agent.py            # run / resume all 150
"""
import json
import os
import sys
import time
from typing import Dict

# The console can be cp1252 (Windows); emojis/box-chars in the progress output
# would otherwise crash with UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from google import genai
from google.genai import types
from dotenv import load_dotenv

# Importing agent triggers app/.env loading (sql.py/faq.py etc. load it at import),
# so GEMINI_API_KEY and friends are set before we build the judge client below.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'app')))
from agent import run_agent

load_dotenv()
JUDGE_MODEL = 'gemini-2.5-flash'
client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

QUESTIONS_FILE = 'test_questions.json'
RESULTS_FILE = 'evaluation_results.json'
SUMMARY_FILE = 'evaluation_summary.json'
RUBRIC_FILE = 'eval_rubric.md'
BATCH = 15


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
            model=JUDGE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(eval_resp.text)
    except Exception as e:
        print(f"  judge error: {e}")
        return {"routing_accuracy": "Error", "faithfulness": 0, "relevance": 0, "reasoning": str(e)}


def save(results):
    """Atomic write so an interrupted run never leaves a half-written file."""
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


def main():
    questions = load_questions()
    rubric = load_rubric()
    total = len(questions)

    # Resume: skip ids already scored in a previous (possibly interrupted) run.
    results, done = [], set()
    if os.path.exists(RESULTS_FILE):
        try:
            results = json.load(open(RESULTS_FILE, encoding='utf-8'))
            done = {r['id'] for r in results}
        except Exception:
            results, done = [], set()
    print(f"🚀 Eval: {len(done)}/{total} already done, {total - len(done)} to go\n" if done
          else f"🚀 Eval: {total} cases, batches of {BATCH}\n")

    for q in questions:
        if q['id'] in done:
            continue
        t0 = time.time()
        agent_response = run_agent(q['question'])
        evaluation = judge_response(q['question'], q['category'], agent_response, rubric)
        results.append({
            "id": q['id'], "category": q['category'], "question": q['question'],
            "agent_response": agent_response, "evaluation": evaluation,
            "duration": round(time.time() - t0, 2),
        })
        save(results)

        n = len(results)
        ev = evaluation
        print(f"[{n:>3}/{total}] {q['category']:<10} route={ev.get('routing_accuracy'):<5} "
              f"F{ev.get('faithfulness')} R{ev.get('relevance')}  {q['question'][:44]}")
        if n % BATCH == 0:
            s = summarize(results, total)
            print(f"  ── batch {n // BATCH}: routing {s['routing_accuracy_rate']:.1%} · "
                  f"faith {s['avg_faithfulness']:.2f} · rel {s['avg_relevance']:.2f} · "
                  f"{s['avg_response_time']:.1f}s/case ──")
            time.sleep(5)
        elif n % 5 == 0:
            time.sleep(1)

    summary = summarize(results, total)
    with open(SUMMARY_FILE, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    print(f"\n✅ Complete. {summary}")


if __name__ == "__main__":
    main()
