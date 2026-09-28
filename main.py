from rag.promptReformulator import promptReformulator
from rag.promptClassifier import promptClassifier


def main() -> None:
	chat = promptReformulator([], "What is your question? ")
	promptClassifier(chat.get("history"), chat.get("prompt"))


if __name__ == "__main__":
	main()
