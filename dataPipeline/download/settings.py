"""Constants and keyword lists for the PDF download step.

Keywords are matched against normalised text (lower case, with `-_/.?=&` turned
into spaces) by `keyword_score`:
  * a keyword containing a space is a substring match ("annual report"),
  * a keyword of 3 characters or fewer must equal a whole word ("ir", "q1"),
  * a longer keyword matches any word starting with it, so stems such as
    "report" also match "reports" and "porocil" matches "porocila".
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = ROOT / "reports"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://username:postgres@localhost:5432/postgres",
)

USER_AGENT = os.getenv(
    "CRAWLER_USER_AGENT",
    "Mozilla/5.0 (compatible; demo-crag-report-crawler/0.1; financial research)",
)
REQUEST_DELAY = float(os.getenv("CRAWLER_DELAY", "1.0"))  # seconds between requests to one host
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 3
MAX_PDF_BYTES = 300 * 1024 * 1024
MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_HEAD_CHECKS = 200  # extension-less "download" links checked per site

# Playwright: newer Python Playwright builds may not find the pre-installed
# browser, so this path is used as a fallback.
CHROMIUM_PATH = os.getenv("PLAYWRIGHT_CHROMIUM_PATH", "/opt/pw-browsers/chromium")

CATEGORIES = ("reports", "announcements", "other")

# ---------------------------------------------------------------- language
EN_SEGMENTS = {"en", "eng", "english", "en-gb", "en-us", "en_gb", "en_us", "en-en"}
# First path segments that mark a non-English section of a site. This list is
# explicit on purpose: a regex such as `[a-z]{2}` would also skip "ir" or "pr".
OTHER_LANG_SEGMENTS = {
    "sl", "si", "slo", "sl-si", "hr", "hrv", "hr-hr", "de", "deu", "de-de", "de-at",
    "it", "ita", "sr", "srb", "rs", "sr-latn", "bs", "ba", "mk", "hu", "cs", "cz",
    "sk", "pl", "fr", "es", "ru", "me", "bg", "ro", "ua", "nl", "pt", "tr", "zh",
}
LANG_QUERY_KEYS = {"lang", "language", "locale", "lng", "l"}
EN_SWITCH_TEXT = {"en", "eng", "english", "en-gb", "gb", "uk english"}

# ---------------------------------------------------------------- keywords
NAV_KEYWORDS = [
    "investor", "ir", "shareholder", "stockholder", "financial information",
    "financials", "financial", "publication", "download", "document", "archive",
    "for investors", "capital market", "share", "governance", "media", "news",
    "report", "result", "announcement", "disclosure",
    # local-language fallbacks, used when a site has no English version
    "vlagatelj", "investitor", "delnicar", "delničar", "dionicar", "dioničar",
    "ulagatelj", "objav", "porocil", "poročil", "izvjesc", "izvješć", "financijsk",
]
REPORT_KEYWORDS = [
    "annual report", "financial report", "financial statement", "report",
    "half year", "half-year", "halfyear", "semi annual", "interim", "quarterly",
    "quarter", "q1", "q2", "q3", "q4", "h1", "h2", "9m", "results", "audited",
    "unaudited", "consolidated", "integrated report", "sustainability report",
    "esg", "esef",
    "letno", "polletn", "četrtletn", "cetrtletn", "porocil", "poročil",
    "godisnj", "godišnj", "polugodisnj", "polugodišnj", "tromjesecn",
    "tromjesečn", "izvjesc", "izvješć", "financijsk",
]
ANNOUNCEMENT_KEYWORDS = [
    "announcement", "press release", "press", "news", "notice", "notification",
    "ad hoc", "adhoc", "regulated information", "general meeting",
    "shareholders meeting", "agm", "egm", "dividend", "convocation", "resolution",
    "insider", "disclosure", "invitation", "voting", "proxy", "major holding",
    "managers transactions", "treasury share", "own shares",
    "obvestil", "objav", "sporocil", "sporočil", "skupscin", "skupščin",
    "obavijest", "skupstin", "skupštin",
]
NEGATIVE_KEYWORDS = [
    "career", "job", "privacy", "cookie", "login", "register", "cart", "shop",
    "product", "gdpr", "terms", "catalog", "catalogue", "brochure", "price list",
    "pricelist", "menu", "tender", "procurement", "recipe", "certificate",
    "safety data sheet", "datasheet", "manual", "instructions",
]

PAGINATION_RE = re.compile(
    r"([?&](page|p|pg|offset|start|year|y|leto|godina|pagenum)=)|(/page/\d+)|(/(19|20)\d\d/?$)",
    re.I,
)
PAGINATION_TEXT_RE = re.compile(
    r"^(\d{1,3}|(19|20)\d\d|next|more|older|previous|prev|archive|›|»|>|>>|"
    r"naprej|naslednja|starejše|sljedeća|starije)$",
    re.I,
)
LOAD_MORE_RE = re.compile(
    r"load more|show more|more results|view more|see more|show all|view all|"
    r"prikaži več|naloži več|učitaj više|prikaži više|prikaži sve",
    re.I,
)
COOKIE_ACCEPT_RE = re.compile(
    r"^(accept|accept all|agree|i agree|allow all|ok|got it|sprejmi|sprejmem|"
    r"strinjam se|prihvati|prihvaćam|slažem se)$",
    re.I,
)
DOWNLOAD_HINT_RE = re.compile(
    r"(download|getfile|get-file|file|document|attachment|blob|media|asset|dl)", re.I
)
NON_PAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".zip", ".rar", ".7z", ".xls", ".xlsx", ".xlsm", ".doc", ".docx", ".ppt",
    ".pptx", ".mp4", ".mp3", ".avi", ".mov", ".xml", ".json", ".rss", ".csv",
    ".txt", ".woff", ".woff2", ".ttf", ".eot", ".exe", ".dmg",
}
YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20\d\d)(?!\d)")

_NORMALISE_RE = re.compile(r"[-_/.?=&+%#:,;()\[\]|]+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def normalise(text: str) -> str:
    return " ".join(_NORMALISE_RE.sub(" ", (text or "").lower()).split())


def keyword_score(text: str, keywords) -> int:
    """Number of distinct keywords that match `text` (see module docstring)."""
    norm = normalise(text)
    if not norm:
        return 0
    words = _WORD_RE.findall(norm)
    score = 0
    for kw in keywords:
        if " " in kw:
            hit = kw in norm
        elif len(kw) <= 3:
            hit = kw in words
        else:
            hit = any(w.startswith(kw) for w in words)
        score += hit
    return score
