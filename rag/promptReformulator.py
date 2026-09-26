from ollama import Client
from pathlib import Path
from helpers.config import read_config

def promptReformulator():
    config = read_config()
    model = config.get("promptReformulatorModel", "gemma3:4b")
    PROMPT_PATH = Path(__file__).parent / "prompts" / "reformulator.txt"
    promptBoilerplate = PROMPT_PATH.read_text(encoding="utf-8")
    question = input("What is your question? ")
    prompt = promptBoilerplate.replace("{{question}}", question)

    client = Client()
    messages = [
        {
            'role': 'user',
            'content': prompt,
        },
    ]

    print("Understanding your question and reformulating for better results...")

    for part in client.chat(model, messages=messages, stream=True):
        print(part.message.content, end='', flush=True)