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


SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "system.txt"
systemPrompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def numeric(prompt: str, prevResponse):
    config = read_config()
    model = config.get("sqlGeneratorModel", "gemma3:4b")

    GENERATION_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "generation.txt"
    
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
            if result.get("row_count") == 0:
                processZeroRows(prompt, sql_query, result, 1)
        else:
            processPromptFixing(prompt, sql_query, result, 1)


def processZeroRows(q, sql, result, attempt):
    if attempt > 2:
        print("\nMaximum attempts reached. The model could not find the requested data.")
        return
    
    config = read_config()
    model = config.get("sqlGeneratorModel", "gemma3:4b")
    REEXAMINATION_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "reexamination.txt"
    reexaminationPrompt = REEXAMINATION_PROMPT_PATH.read_text(encoding="utf-8")
    reexaminationPrompt = reexaminationPrompt.replace("{{question}}", q)
    reexaminationPrompt = reexaminationPrompt.replace("{{previous_sql}}", sql)

    client = Client()
    messages = [
        {
            'role': 'system',
            'content': systemPrompt,
        },
        {
            'role': 'user',
            'content': reexaminationPrompt,
        }
    ]

    print(f"\nFetched no data, reexamining the process...")

    responseJson = ""
    for part in client.chat(model, messages=messages, stream=True, format="json"):
        print(part.message.content, end='', flush=True)
        responseJson += part.message.content

    cleaned_json = clean_json_string(responseJson)
    response = json.loads(cleaned_json)

    if response.get("verdict") == "retry":
        print("\nRetrying")
        sql_query = response.get("sql")
        
        result_json = execute_sql_query(sql_query)
        result = json.loads(result_json)

        if result.get("status") == "success":
            print(f"\nQuery executed successfully. Row count: {result.get('row_count')}")
            print(f"Rows: {result.get('rows')}")
            if result.get("row_count") == 0:
                processZeroRows(q, sql_query, result, attempt+1)
        else:
            processPromptFixing(q, sql_query, result, attempt)
    else:
        print("\nThe model indicated it cannot answer the question.")


def processPromptFixing(q, sql_query, result, attempt): 
    if attempt > 2:
        print("\nMaximum attempts reached. The model failed to fetch the requested data.")
        return
    
    config = read_config()
    model = config.get("sqlGeneratorModel", "gemma3:4b")
    FIXING_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "fixing.txt"
    fixingPrompt = FIXING_PROMPT_PATH.read_text(encoding="utf-8")
    fixingPrompt = fixingPrompt.replace("{{question}}", q)
    fixingPrompt = fixingPrompt.replace("{{previous_sql}}", sql_query)
    fixingPrompt = fixingPrompt.replace("{{error_message}}", result.get("error", ""))
    fixingPrompt = fixingPrompt.replace("{{attempt_number}}", str(attempt))

    print(f"\nFixing prompt:\n{fixingPrompt}")
    
    client = Client()
    messages = [
        {
            'role': 'system',
            'content': systemPrompt,
        },
        {
            'role': 'user',
            'content': fixingPrompt,
        }
    ]

    print(f"\nAn error occurred, attempting to fix {attempt}/2...")

    responseJson = ""
    for part in client.chat(model, messages=messages, stream=True, format="json"):
        print(part.message.content, end='', flush=True)
        responseJson += part.message.content

    cleaned_json = clean_json_string(responseJson)
    response = json.loads(cleaned_json)

    sql_query = response.get("sql")

    result_json = execute_sql_query(sql_query)
    result = json.loads(result_json)

    if result.get("status") == "success":
        print(f"\nQuery executed successfully. Row count: {result.get('row_count')}")
        print(f"Rows: {result.get('rows')}")
        if result.get("row_count") == 0:
            processZeroRows(q, sql_query, result, 1)
    else:
        processPromptFixing(q, sql_query, result, attempt+1)

    