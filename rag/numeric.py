from ollama import Client
from pathlib import Path
from helpers.config import read_config
import json
from helpers.helpers import clean_json_string
from rag.sql import execute_sql_query

"""
{
  "classification": "NUMERIC" | "NARRATIVE" | "MIXED",
  "reasoning": "<one short sentence: which rule decided it>",
  "entities": {
    "companies_mentioned": [],
    "portfolio_wide": false,
    "fiscal_years": [],
    "latest_period_requested": false,
    "metrics": []
  },
  "numeric_question": "<self-contained question or null>",
  "narrative_question": "<self-contained question or null>",
  "narrative_depends_on_numeric": false,
  "needs_clarification": false,
  "clarification_question": null
}
"""


def numeric(prompt: str, prevResponse):
    config = read_config()
    model = config.get("sqlGeneratorModel", "gemma3:4b")

    SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "system.txt"
    GENERATION_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "generation.txt"
    FIXING_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "fixing.txt"
    REEXAMINATION_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "reexamination.txt"
    
    systemPrompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    generationPrompt = GENERATION_PROMPT_PATH.read_text(encoding="utf-8").replace("{{question}}", prompt)
    generationPrompt = generationPrompt.replace("{{company_candidates}}", str(prevResponse.get("entities", {}).get("companies_mentioned", "[]")))
    generationPrompt = generationPrompt.replace("{{fiscal_years}}", str(prevResponse.get("entities", {}).get("fiscal_years", "[]")))
    generationPrompt = generationPrompt.replace("{{metrics}}", str(prevResponse.get("entities", {}).get("metrics", "[]")))

    print(f"\nGeneration prompt:\n{generationPrompt}")

    client = Client()
    messages = [
        {
            'role': 'system',
            'content': systemPrompt,
        },
        {
            'role': 'user',
            'content': generationPrompt,
        }
    ]

    print("\nSearching database...")

    responseJson = ""
    for part in client.chat(model, messages=messages, stream=True, format="json"):
        print(part.message.content, end='', flush=True)
        responseJson += part.message.content

    cleaned_json = clean_json_string(responseJson)
    response = json.loads(cleaned_json)

    if response.get("cannot_answer"):
        print("\nThe model indicated it cannot answer the question.")
    else:
        sql_query = response.get("sql")

        result_json = execute_sql_query(sql_query)
        result = json.loads(result_json)

        if result.get("status") == "success":
            print(f"\nQuery executed successfully. Row count: {result.get('row_count')}")
            print(f"Rows: {result.get('rows')}")
        else:
            print(f"\nError executing query: {result.get('error')}")
