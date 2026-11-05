"""Find a company's English website and collect PDF links from its
financial-report and announcement pages.

The crawl is focused rather than complete. It is a priority queue on the
company's own domain. Links whose URL or anchor text match investor, report or
announcement keywords are followed first. Pagination and year links are
followed only from pages that are already relevant.
"""
import heapq
import itertools
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from .fetcher import FetchError, Fetcher, Page
from .settings import (
    ANNOUNCEMENT_KEYWORDS, DOWNLOAD_HINT_RE, EN_SEGMENTS, EN_SWITCH_TEXT,
    LANG_QUERY_KEYS, MAX_HEAD_CHECKS, NAV_KEYWORDS, NEGATIVE_KEYWORDS,
    NON_PAGE_EXT, OTHER_LANG_SEGMENTS, PAGINATION_RE, PAGINATION_TEXT_RE,
    REPORT_KEYWORDS, keyword_score,
)

CONTEXT_TAGS = ("tr", "li", "article", "dd", "figure", "section")
MULTI_PART_SUFFIXES = {"co", "com", "org", "net", "gov", "ac", "edu"}


@dataclass
class PdfLink:
    url: str
    text: str
    context: str
    page_url: str
    page_title: str
    page_relevance: int


@dataclass
class CrawlResult:
    base_url: str
    english: str  # how the English version was found
    pdfs: dict = field(default_factory=dict)  # url -> PdfLink
    pages_visited: int = 0
    used_js: bool = False


# ------------------------------------------------------------------ URL helpers
def site_key(host: str) -> str:
    """Registrable domain, approximately: www.ir.company.si -> company.si."""
    host = host.lower().split(":")[0]
    if host.replace(".", "").isdigit() or host == "localhost":
        return host
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in MULTI_PART_SUFFIXES and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def clean_url(url: str) -> str:
    return urldefrag(url.strip())[0]


def path_ext(url: str) -> str:
    path = urlsplit(url).path.lower()
    name = path.rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def first_segment(url: str) -> str:
    parts = [p for p in urlsplit(url).path.lower().split("/") if p]
    return parts[0] if parts else ""


def is_other_language(url: str) -> bool:
    """True for URLs that clearly point to a non-English section of a site."""
    if first_segment(url) in OTHER_LANG_SEGMENTS:
        return True
    for key, value in parse_qsl(urlsplit(url).query):
        if key.lower() in LANG_QUERY_KEYS and value and not value.lower().startswith("en"):
            return True
    return False


def element_text(el) -> str:
    text = el.get_text(" ", strip=True)
    if not text:
        text = el.get("title") or el.get("aria-label") or ""
        img = el.find("img")
        if not text and img is not None:
            text = img.get("alt") or ""
    return " ".join(text.split())


def link_context(a) -> str:
    """Text around a link (its table row or list item), where years and types often appear."""
    parent = a.find_parent(CONTEXT_TAGS) or a.parent
    text = parent.get_text(" ", strip=True) if parent is not None else ""
    heading = a.find_previous(["h1", "h2", "h3", "h4"])
    if heading is not None:
        text = heading.get_text(" ", strip=True) + " | " + text
    return " ".join(text.split())[:400]


# ------------------------------------------------------------------ English version
def find_english_base(fetcher: Fetcher, website_url: str) -> tuple[str, str, Page]:
    """Return (english_url, how_found, page). `how_found` is 'not_found' when no English version exists."""
    if "://" in website_url:
        home = fetcher.get_page(website_url)
    else:  # bare domain in the database: prefer https, fall back to http
        try:
            home = fetcher.get_page("https://" + website_url)
        except FetchError:
            home = fetcher.get_page("http://" + website_url)
    soup = BeautifulSoup(home.html or "", "html.parser")
    home_key = site_key(urlsplit(home.url).netloc)

    def same_site(url):
        return site_key(urlsplit(url).netloc) == home_key

    # 1. <link rel="alternate" hreflang="en">: the most reliable signal
    for link in soup.find_all("link", hreflang=True, href=True):
        if link["hreflang"].lower().startswith("en"):
            url = clean_url(urljoin(home.url, link["href"]))
            if same_site(url):
                return url, "hreflang", home if url.rstrip("/") == home.url.rstrip("/") else fetcher.get_page(url)

    # 2. The page itself declares English and is not under a non-English prefix
    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").lower() if html_tag else ""
    if lang.startswith("en") and not is_other_language(home.url):
        return home.url, "html_lang", home

    # 3. A language-switcher link
    for a in soup.find_all("a", href=True):
        href = clean_url(urljoin(home.url, a["href"]))
        if not same_site(href) or href.rstrip("/") == home.url.rstrip("/"):
            continue
        text = element_text(a).lower()
        hreflang = (a.get("hreflang") or a.get("lang") or "").lower()
        if hreflang.startswith("en") or text in EN_SWITCH_TEXT or first_segment(href) in EN_SEGMENTS:
            try:
                page = fetcher.get_page(href)
                if page.html:
                    return page.url, "language_switcher", page
            except FetchError:
                continue

    # 4. Common English paths
    parts = urlsplit(home.url)
    origin = f"{parts.scheme}://{parts.netloc}"
    for path in ("/en/", "/en", "/eng/", "/en-gb/", "/english/"):
        try:
            page = fetcher.get_page(origin + path)
        except FetchError:
            continue
        if page.html and page.url.rstrip("/") != home.url.rstrip("/") and not is_other_language(page.url):
            return page.url, "path_probe", page

    return home.url, "not_found", home


# ------------------------------------------------------------------ crawl
def page_relevance(url: str, title: str, headings: str) -> int:
    text = f"{url} {title} {headings}"
    score = keyword_score(text, NAV_KEYWORDS) + keyword_score(text, REPORT_KEYWORDS) \
        + keyword_score(text, ANNOUNCEMENT_KEYWORDS)
    return max(0, score - 2 * keyword_score(text, NEGATIVE_KEYWORDS))


def link_score(url: str, text: str) -> int:
    path = urlsplit(url).path + " " + urlsplit(url).query
    blob = f"{path} {text}"
    score = 2 * keyword_score(blob, NAV_KEYWORDS) + keyword_score(blob, REPORT_KEYWORDS) \
        + keyword_score(blob, ANNOUNCEMENT_KEYWORDS)
    return score - 3 * keyword_score(blob, NEGATIVE_KEYWORDS)


def is_pagination(url: str, text: str) -> bool:
    return bool(PAGINATION_RE.search(url) or PAGINATION_TEXT_RE.match(text.strip()))


class SiteCrawler:
    def __init__(self, fetcher: Fetcher, log_error, max_pages=150, max_depth=3, js="auto"):
        self.fetcher = fetcher
        self.log_error = log_error
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.js = js  # auto | always | never

    def crawl(self, website_url: str) -> CrawlResult:
        base_url, how, first_page = find_english_base(self.fetcher, website_url)
        if how == "not_found":
            self.log_error("english_version", "no English version found; crawling the default language",
                           url=website_url)
        result = CrawlResult(base_url=base_url, english=how)

        use_js = self.js == "always" or (self.js == "auto" and self._needs_js(first_page))
        self._crawl(result, first_page if not use_js else None, use_js)
        if self.js == "auto" and not use_js and not result.pdfs:
            print("    no PDFs from static HTML, retrying with a browser")
            self._crawl(result, None, True)
        return result

    @staticmethod
    def _needs_js(page: Page) -> bool:
        soup = BeautifulSoup(page.html or "", "html.parser")
        links = len(soup.find_all("a", href=True))
        text = len(soup.get_text(" ", strip=True))
        return links < 10 or text < 300

    def _fetch(self, url: str, use_js: bool) -> Page:
        return self.fetcher.render(url) if use_js else self.fetcher.get_page(url)

    def _crawl(self, result: CrawlResult, first_page: Page | None, use_js: bool) -> None:
        result.used_js = result.used_js or use_js
        base = result.base_url
        home_key = site_key(urlsplit(base).netloc)
        en_prefix = first_segment(base) if first_segment(base) in EN_SEGMENTS else None
        allowed_keys = {home_key}
        counter = itertools.count()
        queue = [(0, next(counter), base, 0)]  # (-score, seq, url, depth)
        seen = {base}
        head_candidates: dict[str, PdfLink] = {}
        link_info: dict[str, PdfLink] = {}  # anchor text etc. of queued links, in case they are PDFs
        visited = 0

        def as_pdf(url: str, final_url: str) -> PdfLink:
            info = link_info.get(url)
            if info is None:
                return PdfLink(final_url, "", "", url, "", 1)
            return PdfLink(final_url, info.text, info.context, info.page_url, info.page_title,
                           info.page_relevance)

        while queue and visited < self.max_pages:
            _, _, url, depth = heapq.heappop(queue)
            if not self.fetcher.allowed(url):
                self.log_error("robots", "disallowed by robots.txt", url=url)
                continue
            try:
                page = first_page if (first_page is not None and url == base) else self._fetch(url, use_js)
            except FetchError as exc:
                self.log_error("crawl", exc, url=url, http_status=exc.status)
                continue
            except Exception as exc:  # noqa: BLE001
                # A browser cannot "render" a direct file download; check it over HTTP instead.
                try:
                    if use_js and self.fetcher.is_pdf(url):
                        result.pdfs.setdefault(url, as_pdf(url, url))
                        continue
                except FetchError:
                    pass
                self.log_error("render" if use_js else "crawl", exc, url=url)
                continue
            visited += 1
            if page.is_pdf:
                result.pdfs.setdefault(page.url, as_pdf(url, page.url))
                continue
            if not page.html:
                continue

            soup = BeautifulSoup(page.html, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            headings = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])[:5])
            relevance = page_relevance(page.url, title, headings)

            # iframes often embed hosted IR document lists
            frames = [(f["src"], "iframe") for f in soup.find_all("iframe", src=True)]
            anchors = [(a["href"], a) for a in soup.find_all("a", href=True)]
            for href, a in frames + anchors:
                link = clean_url(urljoin(page.url, href))
                if not link.startswith(("http://", "https://")):
                    continue
                is_frame = a == "iframe"
                text = "" if is_frame else element_text(a)
                ext = path_ext(link)

                if ext == ".pdf":
                    if link not in result.pdfs:
                        context = "" if is_frame else link_context(a)
                        result.pdfs[link] = PdfLink(link, text, context, page.url, title, relevance)
                    continue

                if ext in NON_PAGE_EXT or link in seen or is_other_language(link):
                    continue
                if is_frame:
                    allowed_keys.add(site_key(urlsplit(link).netloc))
                same_site = site_key(urlsplit(link).netloc) in allowed_keys
                in_english = not en_prefix or first_segment(link) == en_prefix \
                    or urlsplit(link).path in ("", "/")
                score = link_score(link, text)
                follow = (
                    same_site and in_english and depth + 1 <= self.max_depth
                    and (is_frame or score > 0 or (relevance > 0 and is_pagination(link, text)))
                )
                if follow:
                    # Extension-less links that turn out to be PDFs are caught when fetched.
                    seen.add(link)
                    link_info[link] = PdfLink(link, text, "" if is_frame else link_context(a),
                                              page.url, title, relevance)
                    priority = score + (2 if relevance > 0 else 0) + (5 if is_frame else 0)
                    heapq.heappush(queue, (-priority, next(counter), link, depth + 1))
                elif (relevance > 0 or score > 0) and DOWNLOAD_HINT_RE.search(urlsplit(link).path) \
                        and ext in ("", ".ashx", ".axd") and link not in head_candidates:
                    # e.g. /download?id=123 or a CDN document link: check its content-type later
                    head_candidates[link] = PdfLink(link, text, "" if is_frame else link_context(a),
                                                    page.url, title, relevance)

        result.pages_visited += visited

        checks = 0
        for link, candidate in head_candidates.items():
            if link in result.pdfs or checks >= MAX_HEAD_CHECKS:
                continue
            if candidate.page_relevance <= 0 and link_score(link, candidate.text) <= 0:
                continue
            checks += 1
            try:
                if self.fetcher.is_pdf(link):
                    result.pdfs[link] = candidate
            except FetchError as exc:
                self.log_error("head_check", exc, url=link, http_status=exc.status)


# ------------------------------------------------------------------ selection
def wanted(pdf: PdfLink) -> bool:
    """Keep PDFs that look financial themselves or were found on an investor/report page."""
    blob = f"{pdf.url} {pdf.text} {pdf.context}"
    own = keyword_score(blob, REPORT_KEYWORDS) + keyword_score(blob, ANNOUNCEMENT_KEYWORDS) \
        + keyword_score(blob, NAV_KEYWORDS)
    negative = keyword_score(f"{pdf.url} {pdf.text}", NEGATIVE_KEYWORDS)
    if own > 0 and own > negative:
        return True
    return pdf.page_relevance > 0 and negative == 0


def categorise(pdf: PdfLink) -> str:
    blob = f"{urlsplit(pdf.url).path} {pdf.text} {pdf.context}"
    rep = keyword_score(blob, REPORT_KEYWORDS)
    ann = keyword_score(blob, ANNOUNCEMENT_KEYWORDS)
    if rep == ann:
        page = f"{urlsplit(pdf.page_url).path} {pdf.page_title}"
        rep += keyword_score(page, REPORT_KEYWORDS)
        ann += keyword_score(page, ANNOUNCEMENT_KEYWORDS)
    if rep > ann:
        return "reports"
    if ann > rep:
        return "announcements"
    return "reports" if rep > 0 else "other"
