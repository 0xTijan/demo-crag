"""Shared pieces of the indexing steps (plan v1, parts_alternatives_plan.md, Step 1).

* Input: the markdown datasheets of our suppliers, ``markdowns/suppliers/<supplier>/**/*.md``
  (the supplier is the first folder name, plan decision 12).
* Output: one index-cache file per datasheet, ``index_cache/<supplier>/<file_hash>.json``
  (plan section 9). Step 0 (classification) writes its result into it; Step 1 (spec
  extraction) will add the parts to the same file.
* LLM: a local model through Ollama, configured in config.json like the rest of the repo.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_DIR = ROOT / "markdowns"
CACHE_DIR = ROOT / "index_cache"

PAGE_MARKER_RE = re.compile(r"<!--\s*page:\s*(\d+)\s*-->")


@dataclass
class Datasheet:
    path: Path          # absolute path of the markdown file
    rel: str            # path relative to the input folder, e.g. "goodark/10a05-thru-10a10.md"
    supplier: str       # first folder under the input folder
    file_hash: str      # SHA-256 of the markdown bytes

    @property
    def cache_path(self) -> Path:
        return CACHE_DIR / self.supplier / f"{self.file_hash}.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_datasheets(input_dir: Path, suppliers: list[str] | None = None) -> list[Datasheet]:
    """Every markdown under input_dir/<supplier>/..., sorted (deterministic order)."""
    found = []
    for path in sorted(input_dir.rglob("*.md")):
        rel = path.relative_to(input_dir)
        if len(rel.parts) < 2:
            continue  # a file directly in the input folder has no supplier folder
        supplier = rel.parts[0]
        if suppliers and supplier not in suppliers:
            continue
        found.append(Datasheet(path, rel.as_posix(), supplier, file_sha256(path)))
    return found


def split_pages(markdown: str) -> dict[int, str]:
    """{page number: text} from the converter's `<!-- page: N -->` markers ({1: all} without markers)."""
    marks = list(PAGE_MARKER_RE.finditer(markdown))
    if not marks:
        return {1: markdown}
    pages = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(markdown)
        pages[int(m.group(1))] = markdown[m.end():end].strip()
    return pages


def first_pages(markdown: str, pages: int = 2, max_chars: int = 12000) -> str:
    """The first `pages` pages (with their markers), cut at max_chars. Enough to tell the type."""
    split = split_pages(markdown)
    chosen = sorted(split)[:pages]
    text = "\n\n".join(f"<!-- page: {n} -->\n{split[n]}" for n in chosen) if len(split) > 1 else markdown
    text = re.sub(r"!\[[^\]]*\]\(data:[^)]*\)", "", text)  # inline base64 images carry no type information
    return text[:max_chars]


def read_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_cache(path: Path, data: dict) -> None:
    """Atomic write: a crash never leaves a half-written cache file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ LLM
class LLMError(Exception):
    pass


@dataclass
class LLMAnswer:
    data: dict
    seconds: float
    tokens_in: int
    tokens_out: int


class OllamaJSON:
    """One JSON-answer chat call to a local Ollama model (temperature etc. from config.json)."""

    def __init__(self, config_key: str, default_model: str):
        from ollama import Client  # imported here: tests and `report` do not need Ollama

        from helpers.config import model_settings

        self.model, self.options = model_settings(config_key, default_model=default_model)
        self.client = Client()

    def check_model(self) -> None:
        """Fail early with a clear message if the model is not pulled / Ollama is not running."""
        try:
            self.client.show(self.model)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Ollama model {self.model!r} not available ({exc}). "
                           f"Start Ollama and run: ollama pull {self.model}") from exc

    def __call__(self, system: str, user: str) -> LLMAnswer:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        started = time.monotonic()
        try:
            try:  # thinking models (qwen3, ...) would spend most of their output on reasoning
                resp = self.client.chat(self.model, messages=messages, format="json", options=self.options,
                                        think=False)
            except TypeError:
                resp = self.client.chat(self.model, messages=messages, format="json", options=self.options)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"{exc.__class__.__name__}: {exc}") from exc
        content = resp.message.content or ""
        try:
            data = json.loads(content)
        except ValueError as exc:
            raise LLMError(f"answer is not JSON: {content[:200]!r}") from exc
        return LLMAnswer(data, time.monotonic() - started, resp.prompt_eval_count or 0, resp.eval_count or 0)
