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
    def __init__(self, stage: str, message: str, status: int | None = None, retry: bool = False):
        super().__init__(message)
        self.stage = stage
        self.status = status
        self.retry = retry  # a broken transfer, worth another attempt


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


_PART_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{2,39}$")


def part_number_from_url(source_url: str, page_url: str | None = None, link_text: str | None = None,
                         context: str | None = None) -> str | None:
    """The part number in a datasheet URL, when it can be read safely; otherwise None.

    Many sites put the orderable part number in the folder of the PDF, e.g.
    .../datasheet/NCA1462-Q1DNR/NCA1462-Q1_Datasheet_1.2_EN.pdf -> NCA1462-Q1DNR.
    The folder name is used only if it looks like a part number (letters and digits,
    no spaces) and also appears in the page URL, the link text or the table row where
    the link was found, so folders such as /specification/ or /2024/ are never used.
    """
    parts = [unquote(p) for p in urlsplit(source_url).path.split("/") if p]
    if len(parts) < 2:
        return None
    candidate = parts[-2].strip()
    if not _PART_NUMBER.match(candidate) or not re.search(r"[A-Za-z]", candidate) \
            or not re.search(r"\d", candidate):
        return None
    seen_in = " ".join(unquote(x) for x in (page_url, link_text, context) if x).lower()
    return candidate if candidate.lower() in seen_in else None


DOWNLOAD_ATTEMPTS = 3  # a transfer cut off half-way (timeout, dropped connection) is tried again


def download(fetcher: Fetcher, pdf: DatasheetLink, target_dir: Path, known_hashes: dict, base_dir: Path) -> dict:
    """Download `pdf` into `target_dir` as {file name from the server or URL}_{hash8}.pdf.

    Returns a dict with file_path (relative to `base_dir`, i.e. datasheets/), file_hash,
    file_size and duplicate_of (set when the same bytes were saved earlier).
    Raises DownloadError on failure. Broken transfers (timeouts, dropped connections,
    a body that is not a PDF although the server said it was) are retried on a fresh
    connection; HTTP errors and HTML answers are not.
    """
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return _download_once(fetcher, pdf, target_dir, known_hashes, base_dir)
        except DownloadError as exc:
            if attempt == DOWNLOAD_ATTEMPTS or not exc.retry:
                raise
            fetcher.reconnect()


def _download_once(fetcher: Fetcher, pdf: DatasheetLink, target_dir: Path, known_hashes: dict,
                   base_dir: Path) -> dict:
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
            expected = resp.headers.get("content-length")
            if expected and expected.isdigit() and "content-encoding" not in resp.headers and int(expected) != size:
                raise DownloadError("download", f"transfer cut off: {size} of {expected} bytes", retry=True)
        except DownloadError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection dropped mid-transfer etc.
            raise DownloadError("download", f"{exc.__class__.__name__}: {exc}", retry=True) from exc
        finally:
            resp.close()

        with tmp.open("rb") as fh:
            head = fh.read(1024)
        if b"%PDF-" not in head:
            # the server said PDF but sent something else: usually a broken transfer
            raise DownloadError("verify", f"not a PDF (starts with {head[:16]!r})", retry="pdf" in ctype)

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
