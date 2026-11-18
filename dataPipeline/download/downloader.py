"""Download one datasheet PDF, verify it, and save it under datasheets/{manufacturer}/."""
import hashlib
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .crawler import DatasheetLink
from .fetcher import FetchError, Fetcher
from .settings import MAX_PDF_BYTES


class DownloadError(Exception):
    def __init__(self, stage: str, message: str, status: int | None = None):
        super().__init__(message)
        self.stage = stage
        self.status = status


def slugify(text: str, max_len: int = 80) -> str:
    """File-name-safe text: accents folded (č -> c), CJK kept (规格书), everything else -> '-'."""
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    text = re.sub(r"[\W_]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "document"


def guess_filename(pdf: DatasheetLink, disposition: str) -> str:
    """The document's own name: Content-Disposition, then the URL's last part, then the link text."""
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition or "", re.I)
    if match:
        return Path(unquote(match.group(1))).stem
    name = Path(unquote(urlsplit(pdf.url).path)).stem
    return name or pdf.text or "document"


def download(fetcher: Fetcher, pdf: DatasheetLink, target_dir: Path, known_hashes: dict, base_dir: Path) -> dict:
    """Download `pdf` into `target_dir` as {file name from the server or URL}_{hash8}.pdf.

    Returns a dict with file_path (relative to `base_dir`, i.e. datasheets/), file_hash,
    file_size and duplicate_of (set when the same bytes were saved earlier).
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
        existing = known_hashes.get(file_hash)
        if existing and (base_dir / existing).exists():
            tmp.unlink()
            return {"file_path": existing, "file_hash": file_hash, "file_size": size, "duplicate_of": existing}

        final = target_dir / f"{slugify(guess_filename(pdf, disposition))}_{file_hash[:8]}.pdf"
        try:
            tmp.replace(final)
        except OSError as exc:
            raise DownloadError("write", f"{exc.__class__.__name__}: {exc}") from exc
        rel = final.relative_to(base_dir).as_posix()
        known_hashes[file_hash] = rel
        return {"file_path": rel, "file_hash": file_hash, "file_size": size, "duplicate_of": None}
    finally:
        if tmp.exists():
            tmp.unlink()
