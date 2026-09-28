from ollama import Client
from pathlib import Path
from helpers.config import read_config
import json
from helpers.helpers import clean_json_string, history_to_string


def promptReformulator(history: list, q: str = "What is your question? ") -> str:
    print("\n" + history_to_string(history))
    config = read_config()
    model = config.get("promptReformulatorModel", "gemma3:4b")
    PROMPT_PATH = Path(__file__).parent / "prompts" / "reformulator.txt"
    promptBoilerplate = PROMPT_PATH.read_text(encoding="utf-8")
    question = input(q)
    prompt = promptBoilerplate.replace("{{question}}", question).replace("{{chat_history}}", history_to_string(history))

    client = Client()
    messages = [
        {
            'role': 'user',
            'content': prompt,
        },
    ]

    print("Understanding your question and reformulating for better results...")

    responseJson = ""
    for part in client.chat(model, messages=messages, stream=True, format="json"):
        print(part.message.content, end='', flush=True)
        responseJson += part.message.content

    cleaned_json = clean_json_string(responseJson)
    response = json.loads(cleaned_json)

    history.append({"role": "user", "content": question})

    if response.get("blocking_ambiguity"):
        print("\n" + response.get("clarification_question"))
        if len(response.get("suggestions", [])) > 0:
            print("Here are some questions to help refine your query:")
            for i, clarifying_question in enumerate(response.get("suggestions", []), start=1):
                print(f"{i}. {clarifying_question}")
        history.append({"role": "assistant", "content": response.get("clarification_question")})
        promptReformulator(history, "Please clarify your question: ")
    elif response.get("direct_reply") and response.get("direct_reply").strip() != "":
        print("in elif direct reply \n" + response.get("direct_reply"))
        history.append({"role": "assistant", "content": response.get("direct_reply")})
        promptReformulator(history, "Any new questions? ")
    elif response.get("scope") == "OUT_OF_SCOPE":
        history.append({"role": "assistant", "content": response.get("direct_reply")})
        promptReformulator(history, "The question is out of scope. Please try asking a different question?")
    else:
        qToReturn = response.get("refined_question")
        return qToReturn