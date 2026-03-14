import json
import os
import time
from typing import Dict, List
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pathlib import Path
import sys

# Add the app directory to sys.path to import agent
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'app')))
from agent import run_agent

# Load environment
load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
JUDGE_MODEL = 'gemini-2.5-flash'
client = genai.Client(api_key=GEMINI_API_KEY)

# Paths
QUESTIONS_FILE = 'test_questions.json'
RESULTS_FILE = 'evaluation_results.json'
RUBRIC_FILE = 'eval_rubric.md'

def load_rubric():
    with open(RUBRIC_FILE, 'r') as f:
        return f.read()

def load_questions():
    with open(QUESTIONS_FILE, 'r') as f:
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
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(eval_resp.text)
    except Exception as e:
        print(f"Error during judging: {e}")
        return {
            "routing_accuracy": "Error",
            "faithfulness": 0,
            "relevance": 0,
            "reasoning": str(e)
        }

def main():
    print("🚀 Starting E-commerce Agent Evaluation...")
    questions = load_questions()
    rubric = load_rubric()
    results = []
    
    total = len(questions)
    for i, q in enumerate(questions):
        print(f"[{i+1}/{total}] Evaluating: {q['question'][:50]}...")
        
        # Start timer
        start_time = time.time()
        
        # Run agent
        agent_response = run_agent(q['question'])
        
        # Call judge
        evaluation = judge_response(q['question'], q['category'], agent_response, rubric)
        
        duration = time.time() - start_time
        
        result_entry = {
            "id": q['id'],
            "category": q['category'],
            "question": q['question'],
            "agent_response": agent_response,
            "evaluation": evaluation,
            "duration": round(duration, 2)
        }
        results.append(result_entry)
        
        # Small delay to avoid rate limits
        if (i + 1) % 15 == 0:
            print("⏳ Pausing for 5 seconds...")
            time.sleep(5)
        elif (i + 1) % 5 == 0:
            time.sleep(1)
            
    # Save results
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results, f, indent=2)
    
    # Calculate summary metrics
    summary = {
        "total_questions": total,
        "routing_accuracy_rate": sum(1 for r in results if r['evaluation']['routing_accuracy'] == "Pass") / total,
        "avg_faithfulness": sum(r['evaluation']['faithfulness'] for r in results) / total,
        "avg_relevance": sum(r['evaluation']['relevance'] for r in results) / total
    }
    
    print("\n✅ Evaluation Complete!")
    print(f"Summary: {summary}")
    
    with open('evaluation_summary.json', 'w') as f:
        json.dump(summary, f, indent=2)

if __name__ == "__main__":
    main()
