from ollama import Client
from pathlib import Path
from helpers.config import read_config
import json
from helpers.helpers import clean_json_string
from rag.promptReformulator import promptReformulator


def promptClassifier(history: list, prompt: str):
    config = read_config()
    model = config.get("promptReformulatorModel", "gemma3:4b")
    SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "classifier" / "system.txt"
    USER_PROMPT_PATH = Path(__file__).parent / "prompts" / "classifier" / "user.txt"
    promptBoilerplate = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    userPrompt = USER_PROMPT_PATH.read_text(encoding="utf-8").replace("{{question}}", prompt)

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

    print("Choosing best path to process your question...")

    responseJson = ""
    for part in client.chat(model, messages=messages, stream=True, format="json"):
        print(part.message.content, end='', flush=True)
        responseJson += part.message.content

    cleaned_json = clean_json_string(responseJson)
    response = json.loads(cleaned_json)

    # check if the question is clear enough
    if response.get("needs_clarification"):
        history.append({"role": "assistant", "content": response.get("clarification_question")})
        print("\n" + response.get("clarification_question"))
        newPrompt = promptReformulator(history, "Please clarify your question: ")
        promptClassifier(history, newPrompt)
    elif response.get("classification") == "NUMERIC":
        print("\nNumeric processing path selected. Proceeding with numeric processing...")
    elif response.get("classification") == "NARRATIVE":
        print("\nNarrative processing path selected. Proceeding with narrative processing...")
    else:
        print("\nMixed processing path selected. Proceeding with mixed processing...")
