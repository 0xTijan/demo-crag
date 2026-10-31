from ollama import Client
from pathlib import Path
from helpers.config import model_settings
import json
from helpers.helpers import clean_json_string
from rag.promptReformulator import promptReformulator
from rag.numeric import numeric

def promptClassifier(history: list, prompt: str):
    model, options = model_settings("promptReformulatorModel")
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

    print("\nChoosing best path to process your question...")

    responseJson = ""
    for part in client.chat(model, messages=messages, stream=True, format="json", options=options):
        print(part.message.content, end='', flush=True)
        responseJson += part.message.content

    cleaned_json = clean_json_string(responseJson)
    response = json.loads(cleaned_json)

    # check if the question is clear enough
    if response.get("needs_clarification"):
        history.append({"role": "assistant", "content": response.get("clarification_question")})
        print("\n" + response.get("clarification_question"))
        newPrompt = promptReformulator(history, "Please clarify your question: ")
        promptClassifier(newPrompt.get("history"), newPrompt.get("prompt"))
    elif response.get("classification") == "NUMERIC":
        print("\nNumeric processing path selected. Proceeding with numeric processing...")
        numeric(prompt, response)
    elif response.get("classification") == "NARRATIVE":
        print("\nNarrative processing path selected. Proceeding with narrative processing...")
    else:
        print("\nMixed processing path selected. Proceeding with mixed processing...")
