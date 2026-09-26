import json
from pathlib import Path


def read_config():
	"""Read and return the root-level config.json object."""
	config_path = Path(__file__).resolve().parent.parent / "config.json"
	with config_path.open(encoding="utf-8") as config_file:
		return json.load(config_file)
