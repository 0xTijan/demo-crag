"""Ask the local model which row holds each line item, then check its answer.

The model only copies label and cell strings (prompts/system.txt). Every string
it returns is looked up in the page text: a number that is not printed there is
dropped as a hallucination. The sign and separators are taken from the cell
actually found, so a model that drops parentheses cannot flip a sign.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from helpers.config import model_settings
from helpers.helpers import clean_json_string

from .metrics import INDUSTRY_HINTS, ITEMS, STATEMENT_NAMES, normalise
from .numbers import core, decimal_style
from .pages import join_pages, table_rows

PROMPTS = Path(__file__).resolve().parent / "prompts"
CONFIG_KEY = "extractionModel"
DEFAULT_MODEL = "qwen2.5:14b"
PERIOD_TYPES = ["Q1", "Q2", "Q3", "Q4", "1H", "FY"]

# chat(messages, schema) -> reply text. Replaced in tests.
ChatFn = Callable[[list[dict], dict], str]


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def build_schema(items: list[str]) -> dict:
    """JSON schema for Ollama's `format`: header fields plus one entry per item."""
    item = {
        "type": "object",
        "properties": {
            "row_label": {"type": "string"},
            "current": _nullable({"type": "string"}),
            "prior": _nullable({"type": "string"}),
            "page": _nullable({"type": "integer"}),
        },
        "required": ["row_label", "current", "prior", "page"],
    }
    return {
        "type": "object",
        "properties": {
            "period_end": _nullable({"type": "string"}),
            "period_type": _nullable({"enum": PERIOD_TYPES}),
            "unit": _nullable({"enum": ["units", "thousands", "millions"]}),
            "currency": _nullable({"type": "string"}),
            "items": {
                "type": "object",
                "properties": {name: _nullable(item) for name in items},
                "required": items,
            },
        },
        "required": ["period_end", "period_type", "unit", "currency", "items"],
    }


def build_messages(statement: str, items: list[str], pages_text: str, company: str,
                   scope: str | None, industry: str) -> list[dict]:
    system = (PROMPTS / "system.txt").read_text(encoding="utf-8").replace(
        "{{scope}}", "group (consolidated)" if scope != "separate" else "parent company (separate)")
    item_lines = "\n".join(f"- {name}: {ITEMS[name]['description']}" for name in items)
    user = ((PROMPTS / "user.txt").read_text(encoding="utf-8")
            .replace("{{company}}", company or "unknown")
            .replace("{{statement}}", STATEMENT_NAMES[statement])
            .replace("{{industry_hint}}", INDUSTRY_HINTS.get(industry, ""))
            .replace("{{items}}", item_lines)
            .replace("{{pages}}", pages_text))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def ollama_chat() -> tuple[ChatFn, str]:
    """The real model call, configured by config.json["extractionModel"]. Imported lazily."""
    from ollama import Client

    model, options = model_settings(CONFIG_KEY, default_model=DEFAULT_MODEL)
    options = {"temperature": 0, **options}
    client = Client()

    def chat(messages: list[dict], schema: dict) -> str:
        reply = client.chat(model=model, messages=messages, format=schema, options=options)
        return reply["message"]["content"] or ""

    return chat, model


@dataclass
class Found:
    """One verified number: the cell as printed and where it is."""
    raw: str
    page: int
    label: str | None
    decimal: str | None  # the page's decimal separator, for parse_number


@dataclass
class StatementResult:
    statement: str
    pages: list[int]
    header: dict                                   # period_end, period_type, unit, currency from the model
    items: dict[str, dict] = field(default_factory=dict)  # name -> {row_label, current: Found|None, prior: Found|None}
    dropped: list[str] = field(default_factory=list)       # "net_income.current: '123' not on page 5"


def _row_label(row: list[str]) -> str:
    for cell in row:
        if re.search(r"[A-Za-zÀ-ž]{3,}", cell):
            return cell
    return ""


def find_number(raw: str | None, row_label: str | None, pages: dict[int, str], page_numbers: list[int],
                cited: int | None) -> Found | None:
    """Look the model's number up in the given pages. None if it is not printed there.

    Table cells are compared by their digits and separators only (core()), so
    "12.345" from the model matches the cell "(12.345)" and the cell, with its
    sign, is returned. Of several matching cells the one whose row label is most
    like the model's label wins (the cited page gets a small bonus). Numbers in
    running text (a dividend in a sentence) are accepted as printed.
    """
    target = core(raw or "")
    if not re.search(r"\d", target):
        return None
    order = ([cited] if cited in page_numbers else []) + [p for p in page_numbers if p != cited]
    want = normalise(row_label or "")
    best: tuple[float, Found] | None = None
    for p in order:
        rows = table_rows(pages[p])
        dec = decimal_style([c for r in rows for c in r[1:]])
        for row in rows:
            label = _row_label(row)
            for cell in row:
                if cell == label or core(cell) != target:
                    continue
                sim = SequenceMatcher(None, want, normalise(label)).ratio() + (0.2 if p == cited else 0)
                if best is None or sim > best[0]:
                    best = (sim, Found(cell, p, label, dec))
    if best:
        return best[1]
    boundary = re.compile(r"(?<![\d.,])" + re.escape(target) + r"(?![\d]|[.,]\d)")
    for p in order:
        if boundary.search(pages[p]):
            return Found(raw.strip(), p, None, None)
    return None


def extract_statement(chat: ChatFn, statement: str, items: list[str], pages: dict[int, str],
                      page_numbers: list[int], company: str, scope: str | None, industry: str) -> StatementResult:
    """One model call for one statement, then verification of every number."""
    schema = build_schema(items)
    messages = build_messages(statement, items, join_pages(pages, page_numbers), company, scope, industry)
    reply = json.loads(clean_json_string(chat(messages, schema)) or "{}")
    result = StatementResult(statement, page_numbers,
                             {k: reply.get(k) for k in ("period_end", "period_type", "unit", "currency")})
    for name in items:
        entry = (reply.get("items") or {}).get(name)
        if not isinstance(entry, dict):
            continue
        label, cited = entry.get("row_label"), entry.get("page")
        kept = {"row_label": label, "current": None, "prior": None}
        for column in ("current", "prior"):
            raw = entry.get(column)
            if raw is None or not str(raw).strip() or not re.search(r"\d", str(raw)):
                continue
            found = find_number(str(raw), label, pages, page_numbers, cited if isinstance(cited, int) else None)
            if found is None:
                result.dropped.append(f"{name}.{column}: {str(raw)!r} is not printed on pages {page_numbers}")
            else:
                kept[column] = found
        if kept["current"] or kept["prior"]:
            result.items[name] = kept
    return result
