"""Constants and keyword lists for the datasheet download step (crawler.py, run.py).

Two kinds of keyword matching are used on normalised text (lower case, with
`-_/.?=&` turned into spaces):
  * `keyword_score` (product and datasheet words): a keyword containing a space is
    a substring match ("data sheet"), one of 3 characters or fewer must equal a
    whole word ("evm"), a longer one matches any word starting with it, so
    "product" also matches "products";
  * `whole_word_score` (PART_NEGATIVE_KEYWORDS): whole words/phrases only, plural
    allowed, so "press release" never matches "Pressure Sensor".
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASHEETS_DIR = ROOT / "datasheets"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://username:postgres@localhost:5432/postgres",
)

USER_AGENT = os.getenv(
    "CRAWLER_USER_AGENT",
    "Mozilla/5.0 (compatible; demo-crag-datasheet-crawler/0.2)",
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

# ---------------------------------------------------------------- language
EN_SEGMENTS = {"en", "eng", "english", "en-gb", "en-us", "en_gb", "en_us", "en-en"}
# First path segments that mark a non-English section of a site. This list is
# explicit on purpose: a regex such as `[a-z]{2}` would also skip "ic" or "pr".
OTHER_LANG_SEGMENTS = {
    "sl", "si", "slo", "sl-si", "hr", "hrv", "hr-hr", "de", "deu", "de-de", "de-at",
    "it", "ita", "sr", "srb", "rs", "sr-latn", "bs", "ba", "mk", "hu", "cs", "cz",
    "sk", "pl", "fr", "es", "ru", "me", "bg", "ro", "ua", "nl", "pt", "tr", "zh",
    "zh-cn", "zh-tw", "cn", "tw", "jp", "ja", "ko", "kr",
}
LANG_QUERY_KEYS = {"lang", "language", "locale", "lng", "l"}

# ---------------------------------------------------------------- links
PAGINATION_RE = re.compile(
    r"([?&](page|p|pg|offset|start|pagenum|pageindex|curpage)=)|(/page/\d+)",
    re.I,
)
PAGINATION_TEXT_RE = re.compile(
    r"^(\d{1,3}|next|next page|more|previous|prev|first|last|›|»|>|>>|<|<<|下一页|上一页)$",
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

# ---------------------------------------------------------------- catalogue crawl
# crawler.py walks a component manufacturer's product catalogue
# (categories -> families -> series -> parts) and reports every datasheet.
PART_MAX_PAGES = 20000          # pages (static or rendered) per manufacturer
PART_MAX_DEPTH = 10             # link/click depth from the start URL
PART_MAX_CLICKS_PER_PAGE = 400  # clickable elements tried on one rendered page
PART_MAX_PAGINATION = 60        # "next page" clicks inside one rendered page (JS tables)

# A link or clickable element belongs to the product catalogue when its text, URL
# or the group it sits in (e.g. a footer column titled "Products") matches these.
PRODUCT_KEYWORDS = [
    "product", "catalog", "catalogue", "portfolio", "selection guide", "selector",
    "parametric", "series", "family", "families", "part number", "parts", "datasheet",
    "data sheet", "device", "solutions by product",
    "产品", "製品", "제품",
]
# Never followed or clicked when crawling a catalogue. Matched as whole words or
# phrases (plural allowed) by `whole_word_score`, never as prefixes: product names
# such as "Pressure Sensor" or "Sampling ADC" must not hit "press" or "sample".
PART_NEGATIVE_KEYWORDS = [
    "news", "newsroom", "career", "job", "about", "about us", "company profile", "contact",
    "contact us", "investor", "investor relations", "login", "log in", "sign in", "sign up",
    "register", "privacy", "privacy policy", "cookie", "terms", "terms of use", "legal",
    "imprint", "sitemap", "event", "press release", "press room", "media center", "blog",
    "webinar", "training", "online training", "sustainability", "esg", "culture", "history",
    "milestone", "certification", "certificate", "qualification certificate", "quality policy",
    "environment policy", "compliance", "cart", "shopping cart", "buy", "buy now", "order",
    "how to order", "sample", "sample application", "sample request", "request a quote", "quote",
    "distributor", "where to buy", "cross reference", "cross reference search", "search",
    "subscribe", "newsletter", "feedback",
    # general document libraries: the crawl follows the product tree instead
    "document download", "documents download", "download center", "download centre",
]
# Datasheet vs. other technical documents (decides which PDFs are kept).
DATASHEET_KEYWORDS = [
    "datasheet", "data sheet", "specification", "spec sheet",
    "规格书", "数据手册", "規格書", "データシート",
]
OTHER_DOC_KEYWORDS = [
    "application note", "app note", "appnote", "user guide", "user manual", "manual",
    "reliability", "qualification", "certificate", "rohs", "reach", "msds", "sds",
    "brochure", "catalog", "catalogue", "selection guide", "presentation", "white paper",
    "whitepaper", "errata", "pcn", "product change", "end of life", "eol", "flyer",
    "outline dimension", "package outline", "package drawing", "tape and reel", "packing",
    "marking", "soldering", "report", "press release", "guide", "solution",
    "报告", "应用笔记", "手册", "证书", "说明书",
    "design note", "reference design", "evaluation board", "evm", "software", "firmware",
]
# Clickable elements that are never navigation (checked against text, title, aria-label).
CLICK_SKIP_RE = re.compile(
    r"^(home|reset( table)?|reset all|expand|collapse|export|import|columns?|filter|sort|"
    r"share|print|preview|close|cancel|ok|confirm|yes|no|select all|clear|clear all|back|"
    r"back to top|top|menu|more|less|show less|zoom|play|pause|copy|like|"
    r"english|en|中文|简体中文|繁體中文|日本語|한국어|deutsch|français|español|"
    r"\d+ ?/ ?page|<|>|‹|›|«|»|\.\.\.|…)$",
    re.I,
)
# Text (or alt/title/aria-label) of an element that probably opens a document.
DOC_HINT_RE = re.compile(r"datasheet|data sheet|pdf|download|specification|spec\b|规格书|数据手册", re.I)
# Texts that mean "the same thing on every page" (keyed per page/row when clicked).
GENERIC_CLICK_RE = re.compile(
    r"^(datasheet|data sheet|download|downloads|pdf|view|open|details?|more details|"
    r"learn more|read more|view more|more products|specification|spec|documents?)$", re.I)
# Buttons that close a pop-up (language chooser, cookie banner, notices).
MODAL_DISMISS_RE = re.compile(
    r"^(english|en|eng|accept|accept all|agree|i agree|allow all|ok|got it|close|continue|"
    r"confirm|i understand|understood|dismiss|×|x)$",
    re.I,
)

_NORMALISE_RE = re.compile(r"[-_/.?=&+%#:,;()\[\]|]+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def normalise(text: str) -> str:
    return " ".join(_NORMALISE_RE.sub(" ", (text or "").lower()).split())


def whole_word_score(text: str, keywords) -> int:
    """Number of distinct keywords found in `text` as whole words or phrases (plural -s/-es allowed)."""
    norm = f" {normalise(text)} "
    return sum(bool(normalise(kw) in norm if _CJK_RE.search(kw) else
                    re.search(rf"(?<!\w){re.escape(normalise(kw))}(e?s)?(?!\w)", norm)) for kw in keywords)


def keyword_score(text: str, keywords) -> int:
    """Number of distinct keywords that match `text` (see module docstring)."""
    norm = normalise(text)
    if not norm:
        return 0
    words = _WORD_RE.findall(norm)
    score = 0
    for kw in keywords:
        if " " in kw or _CJK_RE.search(kw):  # phrases, and CJK (no spaces between words)
            hit = kw in norm
        elif len(kw) <= 3:
            hit = kw in words
        else:
            hit = any(w.startswith(kw) for w in words)
        score += hit
    return score
