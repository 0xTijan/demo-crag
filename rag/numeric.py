from ollama import Client
from pathlib import Path
from helpers.config import read_config
import json
from helpers.helpers import clean_json_string
from rag.conclusion import conclusion
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
    isDataAvailable = checkDatabaseForData(prevResponse)
    if not isDataAvailable:
        print("\nNo data available for the requested companies and fiscal years.")
        return
    
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
            if result.get("row_count") > 0:
                conclusion(prompt, result_json)
            else:
                processZeroRows(prompt, sql_query, result, prevResponse, 1)
        else:
            processPromptFixing(prompt, sql_query, result, prevResponse, 1)


def processZeroRows(q, sql, result, prevResponse, attempt):  
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
            if result.get("row_count") > 0:
                conclusion(q, result_json)
            else:
                processZeroRows(q, sql_query, result, prevResponse, attempt+1)
        else:
            processPromptFixing(q, sql_query, result, prevResponse, attempt)
    else:
        print("\nThe model indicated it cannot answer the question.")


def checkDatabaseForData(prevResponse):
    companies = prevResponse.get("entities", {}).get("companies_mentioned", [])
    fiscal_years = prevResponse.get("entities", {}).get("fiscal_years", [])

    availability = {}
    isDataAvailable = True
    for company in companies:
        for fiscal_year in fiscal_years:
            # Escape quotes because execute_sql_query accepts a SQL string, not parameters.
            company_value = str(company).replace("'", "''")
            year_value = str(fiscal_year).replace("'", "''")
            sql_query = (
                "SELECT COUNT(*) AS report_count "
                "FROM reports r "
                "JOIN companies c ON c.company_id = r.company_id "
                f"WHERE c.name LIKE '%{company_value}%' "
                f"AND EXTRACT(YEAR FROM r.period_end) = {year_value}"
            )

            result = json.loads(execute_sql_query(sql_query))
            if result.get("status") != "success":
                availability[(company, fiscal_year)] = None
                print(
                    f"Could not check reports for {company} in fiscal year "
                    f"{fiscal_year}: {result.get('error', 'unknown error')}"
                )
                continue

            rows = result.get("rows", [])
            count = rows[0].get("report_count", 0) if rows else 0
            availability[(company, fiscal_year)] = count > 0
            if count > 0:
                print(f"Report found for {company} in fiscal year {fiscal_year}.")
            else:
                isDataAvailable = False
                print(f"No report found for {company} in fiscal year {fiscal_year}.")

    return isDataAvailable


def processPromptFixing(q, sql_query, result, prevResponse, attempt): 
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
        print(result.get("row_count"))
        if result.get('row_count') > 0:
            print("\ndoing conclusion...")
            conclusion(q, result_json)
        if result.get("row_count") == 0:
            processZeroRows(q, sql_query, result, prevResponse, attempt+1)
    else:
        processPromptFixing(q, sql_query, result, prevResponse, attempt+1)

    