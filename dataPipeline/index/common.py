"""Shared pieces of the indexing steps (plan v1, parts_alternatives_plan.md, Step 1).

* Input: the markdown datasheets of our suppliers, ``markdowns/<supplier>/**/*.md`` (written by
  dataPipeline/convert from ``datasheets/<supplier>/**/*.pdf``)
  (the supplier is the first folder name, plan decision 12).
* Output: one index-cache file per datasheet, ``index_cache/<supplier>/<file_hash>.json``
  (plan section 9). Step 0 (classification) writes its result into it; Step 1 (spec
  extraction, extract.py) adds the parts to the same file.
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
MARKDOWN_DIR = ROOT / "markdowns"   # the converter mirrors datasheets/<supplier>/ here
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


TABLE_SEP_CELL_RE = re.compile(r"^:?-{3,}:?$")


def compact_tables(text: str) -> str:
    """Shrink Markdown tables for the LLM: the converter pads every cell with spaces to the column width
    (a 9-column row is ~600 characters for ~70 of content) and repeats a spanning header cell in every column.
    Cells are stripped, a row whose cells are all the same becomes one cell, and separator rows become
    |---|. Only lines starting with '|' change; the text keeps every value. The raw-text check reads the
    original pages, so it is not affected."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|") and len(s) > 1):
            out.append(line)
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if all(TABLE_SEP_CELL_RE.match(c) for c in cells if c) and any(cells):
            out.append("|" + "|".join("---" for _ in cells) + "|")
            continue
        if len(cells) > 1 and len(set(cells)) == 1:
            cells = cells[:1]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def first_pages(markdown: str, pages: int = 2, max_chars: int = 12000) -> str:
    """The first `pages` pages (with their markers), cut at max_chars. Enough to tell the type."""
    split = split_pages(markdown)
    chosen = sorted(split)[:pages]
    text = "\n\n".join(f"<!-- page: {n} -->\n{split[n]}" for n in chosen) if len(split) > 1 else markdown
    text = re.sub(r"!\[[^\]]*\]\(data:[^)]*\)", "", text)  # inline base64 images carry no type information
    return compact_tables(text)[:max_chars]


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


DEFAULT_NUM_PREDICT = 8192    # most answered tokens per call; a looping model stops here instead of never
DEFAULT_TIMEOUT = 1800        # seconds per call (also counts time queued behind other requests on the server)


class OllamaJSON:
    """One JSON-answer chat call to a local Ollama model (temperature etc. from config.json).

    Two limits keep a run from hanging when the model loops (writes the same text or whitespace forever,
    which happens now and then in JSON mode): `num_predict` caps the answer length (config.json, default
    8192 tokens), and `timeout` (config.json, seconds, default 1800) ends a call that takes too long. Either
    one makes the call fail with LLMError; the commands then skip that datasheet and the next run retries it."""

    def __init__(self, config_key: str, default_model: str):
        from ollama import Client  # imported here: tests and `report` do not need Ollama

        from helpers.config import model_settings

        self.model, options = model_settings(config_key, default_model=default_model)
        options = dict(options)
        self.timeout = float(options.pop("timeout", DEFAULT_TIMEOUT))  # not an Ollama model option
        options.setdefault("num_predict", DEFAULT_NUM_PREDICT)
        self.options = options
        self.client = Client(timeout=self.timeout)

    def check_model(self) -> None:
        """Fail early with a clear message if the model is not pulled / Ollama is not running."""
        try:
            self.client.show(self.model)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Ollama model {self.model!r} not available ({exc}). "
                           f"Start Ollama and run: ollama pull {self.model}") from exc

    # Options for the one retry after a cut answer: at temperature 0 the same input loops the same way again,
    # so the retry samples a little and penalises repeats.
    RETRY_OPTIONS = {"temperature": 0.3, "repeat_penalty": 1.15}

    def _chat(self, messages, options):
        try:
            try:  # thinking models (qwen3, ...) would spend most of their output on reasoning
                return self.client.chat(self.model, messages=messages, format="json", options=options, think=False)
            except TypeError:
                return self.client.chat(self.model, messages=messages, format="json", options=options)
        except Exception as exc:  # noqa: BLE001
            if "timeout" in exc.__class__.__name__.lower() or "timed out" in str(exc).lower():
                raise LLMError(f"no answer after {self.timeout:.0f} s (model looping or server busy; "
                               f"raise `timeout` in config.json if calls are just slow)") from exc
            raise LLMError(f"{exc.__class__.__name__}: {exc}") from exc

    def __call__(self, system: str, user: str) -> LLMAnswer:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        started = time.monotonic()
        resp = self._chat(messages, self.options)
        tokens_in, tokens_out = resp.prompt_eval_count or 0, resp.eval_count or 0
        if getattr(resp, "done_reason", None) == "length":  # usually a loop: try once more, differently
            resp = self._chat(messages, {**self.options, **self.RETRY_OPTIONS})
            tokens_in, tokens_out = tokens_in + (resp.prompt_eval_count or 0), tokens_out + (resp.eval_count or 0)
            if getattr(resp, "done_reason", None) == "length":
                raise LLMError(f"answer cut at {self.options.get('num_predict')} tokens twice (model looping, or "
                               f"the answer is longer: raise `num_predict` in config.json)")
        content = resp.message.content or ""
        try:
            data = json.loads(content)
        except ValueError as exc:
            raise LLMError(f"answer is not JSON: {content[:200]!r}") from exc
        return LLMAnswer(data, time.monotonic() - started, tokens_in, tokens_out)
