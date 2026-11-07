"""Download one PDF, verify it, and save it under reports/{company}/{category}/."""
import hashlib
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, unquote_plus, urlsplit

from .crawler import PdfLink
from .fetcher import FetchError, Fetcher
from .settings import MAX_PDF_BYTES, YEAR_RE

GENERIC_TEXT = {"", "download", "pdf", "here", "click here", "open", "view", "more", "read more",
                "prenesi", "preuzmi", "datoteka", "file"}


class DownloadError(Exception):
    def __init__(self, stage: str, message: str, status: int | None = None):
        super().__init__(message)
        self.stage = stage
        self.status = status


def slugify(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "document"


def name_key(url: str) -> str | None:
    """File name of a PDF URL for `--skip-by-name`: last path part, query ignored, lower case.

    Returns None when the URL path does not end in .pdf (e.g. /download?id=5), because then
    the path says nothing about which document it is.
    """
    name = unquote_plus(urlsplit(url).path).rsplit("/", 1)[-1].strip().lower()
    return " ".join(name.split()) if name.endswith(".pdf") else None


def guess_year(pdf: PdfLink) -> int | None:
    for source in (pdf.text, unquote(urlsplit(pdf.url).path), pdf.context):
        years = YEAR_RE.findall(source or "")
        if years:
            return int(years[0])
    return None


def guess_title(pdf: PdfLink, disposition: str) -> str:
    if pdf.text.strip().lower() not in GENERIC_TEXT:
        return pdf.text
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition or "", re.I)
    if match:
        return Path(unquote(match.group(1))).stem
    name = Path(unquote(urlsplit(pdf.url).path)).stem
    return name or pdf.context[:80]


def download(fetcher: Fetcher, pdf: PdfLink, target_dir: Path, known_hashes: dict, reports_dir: Path) -> dict:
    """Download `pdf` into `target_dir`.

    Returns a dict with file_path (relative to reports/), file_hash, file_size,
    fiscal_year and duplicate_of (set when the same bytes were saved earlier).
    Raises DownloadError on failure.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_dir / f".{hashlib.sha1(pdf.url.encode()).hexdigest()}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        try:
            resp = fetcher.stream(pdf.url)
        except FetchError as exc:
            raise DownloadError("download", str(exc), exc.status) from exc
        try:
            disposition = resp.headers.get("content-disposition", "")
            ctype = resp.headers.get("content-type", "").lower()
            if "html" in ctype:
                raise DownloadError("verify", f"expected a PDF, got content-type {ctype}", resp.status_code)
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise DownloadError("download", f"file larger than {MAX_PDF_BYTES} bytes")
                    digest.update(chunk)
                    fh.write(chunk)
        except DownloadError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection dropped mid-transfer etc.
            raise DownloadError("download", f"{exc.__class__.__name__}: {exc}") from exc
        finally:
            resp.close()

        with tmp.open("rb") as fh:
            head = fh.read(1024)
        if b"%PDF-" not in head:
            raise DownloadError("verify", f"not a PDF (starts with {head[:16]!r})")

        file_hash = digest.hexdigest()
        year = guess_year(pdf)
        existing = known_hashes.get(file_hash)
        if existing and (reports_dir / existing).exists():
            tmp.unlink()
            return {"file_path": existing, "file_hash": file_hash, "file_size": size,
                    "fiscal_year": year, "duplicate_of": existing}

        name = f"{year or 'undated'}_{slugify(guess_title(pdf, disposition))}_{file_hash[:8]}.pdf"
        final = target_dir / name
        try:
            tmp.replace(final)
        except OSError as exc:
            raise DownloadError("write", f"{exc.__class__.__name__}: {exc}") from exc
        rel = final.relative_to(reports_dir).as_posix()
        known_hashes[file_hash] = rel
        return {"file_path": rel, "file_hash": file_hash, "file_size": size,
                "fiscal_year": year, "duplicate_of": None}
    finally:
        if tmp.exists():
            tmp.unlink()
