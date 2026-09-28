from rag.promptReformulator import promptReformulator
from rag.promptClassifier import promptClassifier


def main() -> None:
	prompt = promptReformulator([], "What is your question? ")
	promptClassifier(prompt)


if __name__ == "__main__":
	main()
