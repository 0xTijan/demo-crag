import json
from pathlib import Path


def read_config():
	"""Read and return the root-level config.json object."""
	config_path = Path(__file__).resolve().parent.parent / "config.json"
	with config_path.open(encoding="utf-8") as config_file:
		return json.load(config_file)


def model_settings(key: str, default_model: str = "gemma3:4b"):
	"""Return (model, options) for a config entry.

	Every field except "model" (e.g. temperature, num_ctx) is passed to Ollama as options.
	"""
	entry = read_config().get(key) or {}
	if isinstance(entry, str):
		return entry, {}
	options = {name: value for name, value in entry.items() if name != "model"}
	return entry.get("model", default_model), options
