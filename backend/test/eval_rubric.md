# E-commerce Agent Evaluation Rubric

This rubric is used by the LLM Judge (Gemini-2.5-Flash) to evaluate the performance of the E-commerce Agent.

## Scoring Categories

### 1. Routing Accuracy (Pass/Fail)
- **Goal**: Does the agent use the correct tool for the query?
- **Pass**: Uses `search_product_database` for product/stock queries, `search_faq_knowledge_base` for policy/help queries, and handles out-of-scope gracefully.
- **Fail**: Uses the wrong tool (e.g., FAQ for a specific product price) or fails to route an out-of-scope query.

### 2. Faithfulness (1-5)
- **Goal**: Is the response derived solely from the provided tool output without hallucinations?
- **5-Excellent**: Every detail in the response is directly supported by the tool output.
- **3-Fair**: Mostly accurate, but includes minor unsupported details or makes slight assumptions.
- **1-Poor**: Contains significant hallucinations or information not found in the tool output.

### 3. Relevance & Completeness (1-5)
- **Goal**: Does the response fully answer the user's question in a helpful manner?
- **5-Excellent**: Answer is concise, relevant, and covers all parts of the user's query.
- **3-Fair**: Answers the main part of the query but misses secondary details or is overly verbose.
- **1-Poor**: Does not address the user's core question or provides a generic, unhelpful response.

## Overall Performance Metric
- **Final Score**: (Routing == Pass ? 1 : 0) * (Average of Faithfulness and Relevance)
- **Benchmark**: Aiming for > 4.0 average on passed routes.
