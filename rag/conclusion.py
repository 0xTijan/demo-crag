from ollama import Client
from pathlib import Path
from helpers.config import model_settings
import json
from helpers.helpers import clean_json_string

def conclusion(prompt: str, sql):
    model, options = model_settings("conclusionGeneratorModel")
    SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "conclusion" / "system.txt"
    USER_PROMPT_PATH = Path(__file__).parent / "prompts" / "conclusion" / "user.txt"
    promptBoilerplate = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    userPrompt = USER_PROMPT_PATH.read_text(encoding="utf-8").replace("{{question}}", prompt)
    userPrompt = userPrompt.replace("{{sql_results}}", sql)

    client = Client()
    messages = [
        {
            'role': 'system',
            'content': promptBoilerplate,
        },
        {
            'role': 'user',
            'content': userPrompt,
        }
    ]

    print("\nWriting conclusion...")
    print("\n", messages)

    responseJson = ""
    for part in client.chat(model, messages=messages, stream=True, format="markdown", options=options):
        print(part.message.content, end='', flush=True)
        responseJson += part.message.content

    cleaned_json = clean_json_string(responseJson)
    response = json.loads(cleaned_json)
