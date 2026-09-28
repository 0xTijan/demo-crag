from ollama import Client
from pathlib import Path
from helpers.config import read_config
import json
from helpers.helpers import clean_json_string


def promptClassifier(prompt: str):
    config = read_config()
    model = config.get("promptReformulatorModel", "gemma3:4b")
    PROMPT_PATH = Path(__file__).parent / "prompts" / "classifier.txt"
    promptBoilerplate = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = promptBoilerplate.replace("{{question}}", prompt)

    client = Client()
    messages = [
        {
            'role': 'user',
            'content': prompt,
        },
    ]

    print("Choosing best path to process your question...")

    responseJson = ""
    for part in client.chat(model, messages=messages, stream=True, format="json"):
        print(part.message.content, end='', flush=True)
        responseJson += part.message.content

    cleaned_json = clean_json_string(responseJson)
    response = json.loads(cleaned_json)