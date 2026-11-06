"""Vision-LLM OCR fallback (local Ollama model) for pages with (almost) no text.

Used by docling.py and pdfplumber_pipeline.py: after the normal conversion,
pages whose Markdown has fewer than MIN_PAGE_CHARS real characters are rendered
to an image and transcribed by the model configured as "ocrFallbackModel" in
config.json (default qwen2.5vl:7b).
"""
from __future__ import annotations

import io
import os
import re
import time
from pathlib import Path

from dataPipeline.convert.common import log
from helpers.config import model_settings

# Pages with fewer real characters than this (Markdown symbols and whitespace
# ignored) are treated as "OCR came back empty" and re-read by the vision model.
MIN_PAGE_CHARS = int(os.getenv("OCR_FALLBACK_MIN_PAGE_CHARS", "100"))
RENDER_SCALE = 2.0  # page render scale for the vision model (~144 dpi)
DEFAULT_MODEL = "qwen2.5vl:7b"
FALLBACK_PROMPT = (
    "Transcribe this page of a financial report into Markdown. Keep headings, "
    "paragraphs and lists. Render every table as a Markdown table, preserving all "
    "rows, columns and numbers exactly as printed. When a cell spans several rows or "
    "columns, repeat that cell's text in every row and column it covers (Markdown "
    "has no merged cells). Do not summarise, translate or "
    "invent anything. Write [unreadable] where text cannot be read. "
    "Output only the Markdown."
)


def text_length(markdown: str) -> int:
    """Characters of real text in a Markdown string.

    Whitespace and Markdown symbols (table pipes/dashes, #, *, _, >) are dropped,
    so an empty table skeleton or a lone heading does not count as page content.
    """
    return len(re.sub(r"[\s|#*_>\-]", "", markdown))


def model_name() -> str:
    return model_settings("ocrFallbackModel", default_model=DEFAULT_MODEL)[0]


def vision_ocr_pages(pdf: Path, page_numbers: list[int]) -> dict[int, str]:
    """Transcribe the given 1-based pages with the Ollama vision model.

    Renders each page with pypdfium2 and sends the PNG to the model. Returns
    {page_no: markdown}; a page with an empty answer is left out, so the caller
    keeps its own text for it. Imports are lazy: a run that never needs the
    fallback does not need Ollama.
    """
    import pypdfium2 as pdfium
    from ollama import Client

    model, options = model_settings("ocrFallbackModel", default_model=DEFAULT_MODEL)
    client = Client()
    results: dict[int, str] = {}
    pdf_doc = pdfium.PdfDocument(str(pdf))
    try:
        for i, page_no in enumerate(page_numbers, 1):
            # Per flagged page: render to an image -> PNG bytes -> vision model.
            started = time.time()
            log(f"    [vision {i}/{len(page_numbers)}] page {page_no}: sending to {model} ...")
            image = pdf_doc[page_no - 1].render(scale=RENDER_SCALE).to_pil()
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            reply = client.chat(
                model=model,
                messages=[{"role": "user", "content": FALLBACK_PROMPT, "images": [buf.getvalue()]}],
                options=options,
            )
            text_out = (reply["message"]["content"] or "").strip()
            if text_out:
                results[page_no] = text_out
                log(f"    [vision {i}/{len(page_numbers)}] page {page_no}: got {len(text_out)} chars "
                    f"in {time.time() - started:.1f}s")
            else:
                log(f"    [vision {i}/{len(page_numbers)}] page {page_no}: empty answer, keeping original text")
    finally:
        pdf_doc.close()  # always release the PDF handle, even if the model call raised
    return results


def apply_fallback(pdf: Path, page_markdown: dict[int, str]) -> tuple[dict[int, str], int, str | None, list]:
    """Run the fallback on sparse pages of {page_no: markdown}.

    Returns (updated page_markdown, pages replaced, model name or None, warnings).
    Never raises: a failing model call is returned as a warning so the file still
    converts with its original text.
    """
    sparse = [n for n, md in sorted(page_markdown.items()) if text_length(md) < MIN_PAGE_CHARS]
    log(f"  OCR check: {len(sparse)} sparse page(s) under {MIN_PAGE_CHARS} chars"
        f"{' ' + str(sparse[:10]) if sparse else ''}")
    if not sparse:
        return page_markdown, 0, None, []
    log(f"  running vision OCR fallback on {len(sparse)} page(s) ...")
    try:
        replacements = vision_ocr_pages(pdf, sparse)
    except Exception as exc:  # noqa: BLE001
        log(f"  vision OCR failed, keeping original text: {exc}")
        return page_markdown, 0, None, [("ocr_fallback", exc)]
    if not replacements:
        log("  vision OCR returned nothing; keeping original text")
        return page_markdown, 0, None, []
    log(f"  vision OCR replaced {len(replacements)}/{len(sparse)} page(s)")
    return {**page_markdown, **replacements}, len(replacements), model_name(), []


def join_pages(page_markdown: dict[int, str]) -> str:
    """Whole document with a `<!-- page: N -->` marker before every page."""
    return "\n\n".join(f"<!-- page: {n} -->\n\n{md.strip()}" for n, md in sorted(page_markdown.items())) + "\n"
