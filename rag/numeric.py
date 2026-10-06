from ollama import Client
from pathlib import Path
from helpers.config import read_config
import json
from helpers.helpers import clean_json_string


def numeric(prompt: str):
    config = read_config()
    model = config.get("sqlGeneratorModel", "gemma3:4b")

    SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "system.txt"
    GENERATION_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "generation.txt"
    FIXING_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "fixing.txt"
    REEXAMINATION_PROMPT_PATH = Path(__file__).parent / "prompts" / "sql" / "reexamination.txt"
    
    systemPrompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    generationPrompt = GENERATION_PROMPT_PATH.read_text(encoding="utf-8").replace("{{question}}", prompt)

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
