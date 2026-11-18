"""Crawl a component manufacturer's product catalogue and collect every datasheet PDF.

Generic: nothing in here knows a particular website. A catalogue is a tree
(categories -> families -> series -> part pages) and the datasheet is a PDF linked
or opened from somewhere in that tree. The crawler walks the whole tree:

* Static sites: every `<a href>` in the content of a catalogue page is followed
  (families, pagination, part pages), and every `.pdf` link is collected.
* JavaScript sites (the homepage has almost no links, e.g. Vue/React apps whose
  menus are `<div>`s with click handlers): every page is rendered in Chromium and
  every clickable element is *clicked*, in the page and in its iframes. A click can
  change the URL (a new page to crawl), open a new tab or start a download (a PDF
  is collected), or change the page in place. Tables with JavaScript pagination are
  paged through with their "next" button, so the parts on every page are clicked.

What counts as the catalogue: a link/element whose text, URL or surrounding group
(e.g. a footer column titled "Products") matches PRODUCT_KEYWORDS. Everything
reached from the *content* of a catalogue page (not its header or footer) is part
of the catalogue too, unless it matches PART_NEGATIVE_KEYWORDS (news, careers,
login, sample/buy buttons, ...). Which PDFs are datasheets: `is_datasheet`.

The crawler downloads nothing. Each new PDF is passed to `on_pdf` as soon as it is
found, so a long crawl saves its results as it goes.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from .fetcher import FetchError, Fetcher, Page
from .settings import (
    CLICK_SKIP_RE, DATASHEET_KEYWORDS, EN_SEGMENTS, LANG_QUERY_KEYS, OTHER_LANG_SEGMENTS, DOC_HINT_RE, DOWNLOAD_HINT_RE, GENERIC_CLICK_RE,
    MAX_HEAD_CHECKS, MODAL_DISMISS_RE, NON_PAGE_EXT, OTHER_DOC_KEYWORDS,
    PAGINATION_RE, PAGINATION_TEXT_RE, PART_MAX_CLICKS_PER_PAGE, PART_MAX_DEPTH,
    PART_MAX_PAGES, PART_MAX_PAGINATION, PART_NEGATIVE_KEYWORDS, PRODUCT_KEYWORDS,
    keyword_score, whole_word_score,
)

CONTEXT_TAGS = ("tr", "li", "article", "dd", "figure", "section")
MULTI_PART_SUFFIXES = {"co", "com", "org", "net", "gov", "ac", "edu"}

# Classes/ids of page-level header, footer and navigation bars (only checked on
# the first few levels below <body>, so "card-header" inside content is not chrome).
CHROME_RE = re.compile(r"(^|[-_\s])(header|footer|foot|navbar|nav|topbar|top-bar)(?=$|[-_\s\d])", re.I)
CHROME_DEPTH = 5
# Application-note numbering: "AN-12-0029", "AN_1234", "AN 15"
APP_NOTE_RE = re.compile(r"(?<![a-z0-9])an[-_ ]?\d", re.I)


@dataclass
class DatasheetLink:
    """A PDF found in the catalogue (downloader.py reads url, text and context)."""
    url: str
    text: str            # link / button text, or its alt/title
    context: str         # table row or group around it (part number, family)
    page_url: str
    page_title: str
    trail: tuple = ()    # link/click texts from the start page to this PDF
    on_product_page: bool = True
    page_relevance: int = 1


@dataclass
class PartCrawlResult:
    start_url: str
    pdfs: dict = field(default_factory=dict)      # url -> DatasheetLink (every PDF seen)
    pages_visited: int = 0
    clicks: int = 0
    used_js: bool = False
    unavailable: list = field(default_factory=list)  # (page, element text, message shown)


@dataclass
class Node:
    url: str
    depth: int
    product: bool        # inside the catalogue
    trail: tuple = ()


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
    """Text around a link (its table row or list item, and the heading above it)."""
    parent = a.find_parent(CONTEXT_TAGS) or a.parent
    text = parent.get_text(" ", strip=True) if parent is not None else ""
    heading = a.find_previous(["h1", "h2", "h3", "h4"])
    if heading is not None:
        text = heading.get_text(" ", strip=True) + " | " + text
    return " ".join(text.split())[:400]


# ------------------------------------------------------------------ classification
def product_score(text: str) -> int:
    return keyword_score(text, PRODUCT_KEYWORDS)


def negative_score(text: str) -> int:
    return whole_word_score(text, PART_NEGATIVE_KEYWORDS)


def is_datasheet(pdf: DatasheetLink) -> bool:
    """Keep datasheets; drop application notes, certificates, brochures, package drawings, ...

    The PDF's own URL and link/button text decide first, then its table row or list
    item, and a PDF without any hint is kept when it was found inside the catalogue."""
    own = f"{unquote(pdf.url)} {pdf.text} {pdf.trail[-1] if pdf.trail else ''}"
    positive = keyword_score(own, DATASHEET_KEYWORDS)
    negative = keyword_score(own, OTHER_DOC_KEYWORDS) + bool(APP_NOTE_RE.search(own))
    if positive and positive >= negative:
        return True
    if negative:
        return False
    positive = keyword_score(pdf.context, DATASHEET_KEYWORDS)
    negative = keyword_score(pdf.context, OTHER_DOC_KEYWORDS)
    if positive != negative:
        return positive > negative
    return pdf.on_product_page


def is_site_root(url: str) -> bool:
    """https://x.com/, https://x.com/en/, https://x.com/index.php -> True"""
    parts = [p for p in urlsplit(url).path.lower().split("/") if p]
    if parts and re.fullmatch(r"(index|default|home)(\.\w+)?", parts[-1]):
        parts = parts[:-1]
    return not parts or (len(parts) == 1 and (parts[0] in EN_SEGMENTS or parts[0] in OTHER_LANG_SEGMENTS))


def same_host(a: str, b: str) -> bool:
    a, b = a.lower().split(":")[0], b.lower().split(":")[0]
    return a.removeprefix("www.") == b.removeprefix("www.")


def in_chrome_static(tag) -> bool:
    """True if a BeautifulSoup tag sits in the page header, footer or navigation bar."""
    ancestors = [p for p in tag.parents if p.name not in (None, "[document]")]
    ancestors.reverse()  # html, body, ...
    for depth, node in enumerate(ancestors):
        if node.name in ("header", "footer", "nav"):
            return True
        if depth - 1 <= CHROME_DEPTH:  # depth 1 == body
            ident = " ".join(node.get("class") or []) + " " + (node.get("id") or "")
            if CHROME_RE.search(ident):
                return True
    return False


# ------------------------------------------------------------------ browser scripts
# Runs in every frame. Lists links, iframes, clickable elements (tagged with
# data-pc-i so Python can click them) and the table "next page" button.
HARVEST_JS = r"""
(args) => {
  const CHROME_RE = new RegExp(args.chromeRe, 'i');
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = el => clean(el.innerText || el.textContent).slice(0, 160);
  const hintOf = el => {
    const img = el.tagName === 'IMG' ? el : el.querySelector('img');
    return clean([el.getAttribute('title'), el.getAttribute('aria-label'),
                  img && img.getAttribute('alt'), img && img.getAttribute('title')].filter(Boolean).join(' ')).slice(0, 120);
  };
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const depthOf = el => { let d = 0; while (el && el !== document.body) { el = el.parentElement; d++; } return d; };
  const inChrome = el => {
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      if (['HEADER', 'FOOTER', 'NAV'].includes(p.tagName)) return true;
      if (depthOf(p) <= args.chromeDepth && CHROME_RE.test((p.className && p.className.toString()) + ' ' + (p.id || ''))) return true;
    }
    return false;
  };
  // Section label: the nearest short text before the element that is not another item
  // of the same kind, e.g. "Products" for the "Sensor" entry of a footer column.
  const labelOf = el => {
    let node = el;
    for (let n = 0; node && node !== document.body && n < 5; n++, node = node.parentElement) {
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
        if (s.tagName === node.tagName && s.className === node.className) continue;
        const t = textOf(s);
        if (t && t.length <= 40) return t;
        if (t) break;  // a long block: not a heading
      }
    }
    return '';
  };
  const groupOf = (el, own) => {
    let p = el.parentElement;
    for (let n = 0; p && p !== document.body && n < 6; n++, p = p.parentElement) {
      const t = textOf(p);
      if (t.length > own.length + 10) return t.slice(0, 300);
    }
    return '';
  };
  const rowOf = el => {
    const r = el.closest('tr, li, [role=row]');
    return r && r !== el ? textOf(r).slice(0, 200) : '';
  };
  const realHref = a => {
    const h = (a.getAttribute('href') || '').trim();
    return h && !h.startsWith('#') && !h.toLowerCase().startsWith('javascript:');
  };
  const SKIP = 'input, select, textarea, label, th, [role=checkbox], [role=radio], [role=switch], ' +
               '[role=tab], [role=combobox], [role=listbox], [role=option], [role=slider], [role=dialog], ' +
               '[aria-modal=true], [class*=pagination], [class*=pager], [class*=select-selector], [class*=checkbox]';

  const out = {title: document.title || '', headings: '', anchors: [], frames: [], cands: [], next: false, sig: ''};
  out.headings = clean([...document.querySelectorAll('h1, h2, [class*=breadcrumb], [class*=crumb]')]
                       .slice(0, 6).map(textOf).join(' | ')).slice(0, 400);
  for (const a of document.querySelectorAll('a[href], area[href]')) {
    if (!realHref(a)) continue;
    const own = textOf(a) || hintOf(a);
    const label = labelOf(a);
    out.anchors.push({href: a.href, text: own, label, context: clean(label + ' | ' + (rowOf(a) || groupOf(a, own))),
                      chrome: inChrome(a)});
  }
  for (const f of document.querySelectorAll('iframe[src], frame[src]')) out.frames.push(f.src);

  document.querySelectorAll('[data-pc-i]').forEach(e => e.removeAttribute('data-pc-i'));
  document.querySelectorAll('[data-pc-next]').forEach(e => e.removeAttribute('data-pc-next'));
  let i = 0;
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    if (['SCRIPT', 'STYLE', 'svg', 'path', 'IFRAME', 'HTML', 'BODY', 'OPTION'].includes(el.tagName)) continue;
    const cs = getComputedStyle(el);
    const parent = el.parentElement;
    const pointer = cs.cursor === 'pointer' && !(parent && getComputedStyle(parent).cursor === 'pointer');
    const button = (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' || el.hasAttribute('onclick'))
                   && !(parent && parent.closest('button, [role=button], [onclick]'));
    if (!pointer && !button) continue;
    if (el.tagName === 'A' && realHref(el)) continue;            // a normal link: harvested above
    if (el.closest(SKIP) || el.querySelector('input, select, textarea')) continue;
    if (el.querySelector('a[href]:not([href^="#"]):not([href^="javascript"])')) continue;  // wrapper of a link
    const text = textOf(el), hint = hintOf(el);
    if (!text && !hint) continue;
    el.setAttribute('data-pc-i', String(i));
    const label = labelOf(el);
    out.cands.push({i: i++, text, hint, row: rowOf(el), label,
                    context: clean(label + ' | ' + groupOf(el, text || hint)),
                    chrome: inChrome(el), visible: visible(el)});
  }
  // "next page" button of a JavaScript-paged table or list
  for (const box of document.querySelectorAll('[class*=pagination], [class*=pager], nav[aria-label*=pag i]')) {
    for (const el of box.querySelectorAll('*')) {
      const label = [el.className && el.className.toString(), el.getAttribute('title'), el.getAttribute('aria-label'),
                     el.getAttribute('rel'), (el.children.length ? '' : textOf(el))].join(' ').toLowerCase();
      if (!/(^|[^a-z])next([^a-z]|$)|^\s*(>|›|»)\s*$/.test(label)) continue;
      const host = el.closest('li, button') || el;
      const disabled = host.getAttribute('aria-disabled') === 'true' || host.hasAttribute('disabled') ||
                       /disabled/i.test((host.className && host.className.toString()) || '');
      if (!disabled && visible(host)) { host.setAttribute('data-pc-next', '1'); out.next = true; }
      break;
    }
    if (out.next) break;
  }
  out.sig = out.cands.slice(0, 40).map(c => c.text + '|' + c.row).join('\n');
  return out;
}
"""

# Closes language choosers, cookie banners and notices. Returns their texts.
DISMISS_JS = r"""
(args) => {
  const DISMISS = new RegExp(args.dismissRe, 'i');
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
                          return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const boxes = [...document.querySelectorAll('[role=dialog], [role=alertdialog], [aria-modal=true], ' +
                 '[class*=modal], [class*=dialog], [class*=popup], [class*=cookie], [id*=cookie]')]
                 .filter(visible);
  const outer = boxes.filter(b => !boxes.some(o => o !== b && o.contains(b)));
  const closed = [];
  for (const box of outer) {
    const items = [...box.querySelectorAll('button, [role=button], a, div, span')].filter(visible);
    const label = el => clean(el.innerText || el.getAttribute('aria-label') || el.getAttribute('title'));
    const pick = items.find(el => /^(english|en|eng)$/i.test(label(el)))
              || items.find(el => DISMISS.test(label(el)) && el.children.length <= 2)
              || items.find(el => /close/i.test((el.className && el.className.toString()) + ' ' + (el.getAttribute('aria-label') || '')));
    if (pick) { closed.push(clean(box.innerText).slice(0, 300)); pick.click(); }
  }
  return closed;
}
"""


class PartCrawler:
    """Walks one manufacturer's catalogue. See the module docstring."""

    def __init__(self, fetcher: Fetcher, log_error, on_pdf=None, max_pages=PART_MAX_PAGES,
                 max_depth=PART_MAX_DEPTH, js="auto", verbose=False):
        self.fetcher = fetcher
        self.log_error = log_error
        self.on_pdf = on_pdf or (lambda pdf: None)
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.js = js  # auto | always | never
        self.verbose = verbose
        self.hosts: set[str] = set()
        self.seen: set[str] = set()
        self.queue: deque[Node] = deque()
        self.click_memo: dict = {}
        self.result: PartCrawlResult | None = None

    # ------------------------------------------------------------ entry point
    def crawl(self, start_url: str) -> PartCrawlResult:
        if "://" not in start_url:
            start_url = "https://" + start_url
        first = self.fetcher.get_page(start_url)
        start = clean_url(first.url)
        self.result = PartCrawlResult(start_url=start)
        self.hosts = {urlsplit(start).netloc.lower()}
        use_js = self.js == "always" or (self.js == "auto" and self._needs_js(first))
        self.result.used_js = use_js
        print(f"    catalogue crawl of {start} ({'browser, clicking' if use_js else 'static HTML'})")

        title = self._title(first)
        self.seen.add(start)
        # A start URL inside the site is taken as the catalogue; the homepage is only a way in.
        self.queue.append(Node(start, 0, not is_site_root(start) or product_score(title) > 0))
        head_candidates: dict[str, DatasheetLink] = {}
        started = time.monotonic()
        while self.queue and self.result.pages_visited < self.max_pages:
            node = self.queue.popleft()
            if not self.fetcher.allowed(node.url):
                self.log_error("robots", "disallowed by robots.txt", url=node.url)
                continue
            try:
                if use_js:
                    self._explore_rendered(node)
                else:
                    page = first if node.url == start and first.html else self.fetcher.get_page(node.url)
                    self._explore_static(node, page, head_candidates)
            except FetchError as exc:
                self.log_error("crawl", exc, url=node.url, http_status=exc.status)
                continue
            except Exception as exc:  # noqa: BLE001 - one broken page must not stop the crawl
                self.log_error("render" if use_js else "crawl", exc, url=node.url)
                continue
            self.result.pages_visited += 1
            if self.verbose or self.result.pages_visited % 25 == 0:
                print(f"    .. {self.result.pages_visited} pages, {len(self.queue)} queued, "
                      f"{len(self.result.pdfs)} PDFs, {self.result.clicks} clicks, "
                      f"{time.monotonic() - started:.0f}s  [{node.url}]")

        self._check_head_candidates(head_candidates)
        if self.queue:
            print(f"    stopped at --max-pages {self.max_pages} with {len(self.queue)} pages still queued")
        return self.result

    # ------------------------------------------------------------ shared helpers
    @staticmethod
    def _needs_js(page: Page) -> bool:
        soup = BeautifulSoup(page.html or "", "html.parser")
        links = [a for a in soup.find_all("a", href=True)
                 if not a["href"].startswith(("#", "javascript:"))]
        return len(links) < 10 or len(soup.get_text(" ", strip=True)) < 300

    @staticmethod
    def _title(page: Page) -> str:
        soup = BeautifulSoup(page.html or "", "html.parser")
        return soup.title.get_text(" ", strip=True) if soup.title else ""

    def _allowed_host(self, url: str) -> bool:
        host = urlsplit(url).netloc.lower()
        return any(same_host(host, h) for h in self.hosts)

    def _add_pdf(self, pdf: DatasheetLink) -> None:
        if pdf.url in self.result.pdfs:
            return
        self.result.pdfs[pdf.url] = pdf
        self.on_pdf(pdf)

    def _decide(self, node: Node, text: str, context: str, chrome: bool) -> tuple[bool, bool]:
        """(follow?, child is inside the catalogue?) for a link or clickable element.

        Outside the catalogue (e.g. the homepage) an item is followed when it or its group
        is about products ("Sensor" in a footer column titled "Products"). Inside it, all
        content is followed, but header/footer items only when their own text is."""
        if negative_score(text) > 0:
            return False, False
        if node.product and not chrome:
            return True, True
        pos = product_score(text) if node.product else product_score(f"{text} {context}")
        return pos > 0, pos > 0

    def _decide_rendered(self, node: Node, text: str, label: str, chrome: bool) -> tuple[bool, bool]:
        """Like _decide, with the element's section label as its context."""
        return self._decide(node, text, label, chrome)

    def _enqueue(self, node: Node, url: str, text: str, product: bool) -> None:
        url = clean_url(url)
        if url in self.seen or node.depth + 1 > self.max_depth:
            return
        if not url.startswith(("http://", "https://")) or not self._allowed_host(url):
            return
        if path_ext(url) in NON_PAGE_EXT or is_other_language(url):
            return
        if negative_score(unquote(urlsplit(url).path)) > 0:  # /news-details/..., /careers/...
            return
        self.seen.add(url)
        self.queue.append(Node(url, node.depth + 1, product, node.trail + (text[:60],)))

    def _handle_anchor(self, node: Node, href: str, text: str, context: str, chrome: bool,
                       page_url: str, title: str, head_candidates: dict | None = None) -> None:
        link = clean_url(href)
        if not link.startswith(("http://", "https://")):
            return
        label = text or unquote(urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1])  # for the trail
        if path_ext(link) == ".pdf":  # every PDF is collected; is_datasheet() decides later
            self._add_pdf(DatasheetLink(link, text, context, page_url, title,
                                        node.trail + (label[:60],), node.product))
            return
        pagination = node.product and (PAGINATION_RE.search(link) or PAGINATION_TEXT_RE.match(text.strip()))
        follow, product = (True, True) if pagination and not chrome else self._decide(node, text, context, chrome)
        if follow:
            self._enqueue(node, link, label, product)
        elif head_candidates is not None and node.product and path_ext(link) in ("", ".ashx", ".axd") \
                and DOWNLOAD_HINT_RE.search(urlsplit(link).path) and link not in head_candidates:
            head_candidates[link] = DatasheetLink(link, text, context, page_url, title,
                                                  node.trail + (label[:60],), node.product)

    def _check_head_candidates(self, head_candidates: dict) -> None:
        """Extension-less download links (e.g. /download?id=5) are PDFs if the server says so."""
        for checks, (link, candidate) in enumerate(head_candidates.items()):
            if checks >= MAX_HEAD_CHECKS:
                break
            if link in self.result.pdfs:
                continue
            try:
                if self.fetcher.is_pdf(link):
                    self._add_pdf(candidate)
            except FetchError as exc:
                self.log_error("head_check", exc, url=link, http_status=exc.status)

    # ------------------------------------------------------------ static pages
    def _explore_static(self, node: Node, page: Page, head_candidates: dict) -> None:
        if page.is_pdf:
            text = node.trail[-1] if node.trail else ""
            self._add_pdf(DatasheetLink(page.url, text, "", node.url, "", node.trail, node.product))
            return
        if not page.html:
            return
        soup = BeautifulSoup(page.html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if not node.product and node.depth == 0:
            headings = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])[:4])
            node.product = product_score(f"{urlsplit(page.url).path} {title} {headings}") > 0
        for frame in soup.find_all(["iframe", "frame"], src=True):
            src = clean_url(urljoin(page.url, frame["src"]))
            if src.startswith("http") and (node.product or product_score(src)):
                self.hosts.add(urlsplit(src).netloc.lower())  # hosted catalogue widgets
                self._enqueue(node, src, "iframe", node.product)
        for a in soup.find_all(["a", "area"], href=True):
            text = element_text(a)
            self._handle_anchor(node, urljoin(page.url, a["href"]), text, link_context(a),
                                in_chrome_static(a), page.url, title, head_candidates)

    # ------------------------------------------------------------ rendered pages (clicking)
    def _settle(self, page, idle_ms: int = 8000) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=idle_ms)
        except Exception:  # noqa: BLE001 - pages with endless polling never go idle
            pass
        page.wait_for_timeout(700)

    def _wait_stable(self, page, max_ms: int = 12000) -> None:
        """Wait until no frame changes any more (rows of JavaScript tables arrive late)."""
        def size():
            total = []
            for frame in page.frames:
                try:
                    total.append(frame.evaluate(
                        "() => document.body ? [document.body.getElementsByTagName('*').length,"
                        " (document.body.innerText || '').length] : [0, 0]"))
                except Exception:  # noqa: BLE001 - detached frame
                    continue
            return total
        last, stable, waited = None, 0, 0
        while waited < max_ms and stable < 2:
            page.wait_for_timeout(500)
            waited += 500
            now = size()
            stable = stable + 1 if now == last else 0
            last = now

    def _dismiss(self, page) -> list[str]:
        closed = []
        for _ in range(3):
            found = []
            for frame in page.frames:
                try:
                    found += frame.evaluate(DISMISS_JS, {"dismissRe": MODAL_DISMISS_RE.pattern})
                except Exception:  # noqa: BLE001 - detached frame
                    continue
            if not found:
                break
            closed += found
            page.wait_for_timeout(500)
        return closed

    def _load(self, page, url: str) -> None:
        self.fetcher.wait(url)
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if resp is not None and resp.status >= 400:
            raise FetchError(f"HTTP {resp.status}", resp.status)
        self._settle(page)
        self._wait_stable(page)
        page.wait_for_timeout(500)  # late pop-ups (language choosers, cookie banners)
        if self._dismiss(page):
            self._wait_stable(page)
        for _ in range(3):  # trigger lazy loading
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(250)

    def _harvest(self, page) -> list[tuple[int, object, dict]]:
        snaps = []
        args = {"chromeRe": CHROME_RE.pattern, "chromeDepth": CHROME_DEPTH}
        for idx, frame in enumerate(page.frames):
            try:
                snaps.append((idx, frame, frame.evaluate(HARVEST_JS, args)))
            except Exception:  # noqa: BLE001 - detached or blocked frame
                continue
        return snaps

    def _page_next(self, page) -> bool:
        for frame in page.frames:
            try:
                el = frame.locator("[data-pc-next]").first
                if el.count():
                    el.evaluate("e => e.click()")
                    self._settle(page, 5000)
                    self._wait_stable(page)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _goto_state(self, page, url: str, steps: int) -> None:
        """Reload the page and press "next page" `steps` times."""
        self._load(page, url)
        for _ in range(steps):
            self._harvest(page)
            if not self._page_next(page):
                break
        self._harvest(page)

    def _record_snapshot(self, node: Node, page, snaps, cands: dict, steps: int) -> None:
        top_title = ""
        for idx, frame, snap in snaps:
            top_title = top_title or snap["title"]
            for src in snap["frames"]:
                if src.startswith("http") and (node.product or product_score(src)):
                    self.hosts.add(urlsplit(src).netloc.lower())
            for a in snap["anchors"]:
                self._handle_anchor(node, a["href"], a["text"], a["label"], a["chrome"], page.url, top_title)
            occurrences: dict = {}
            for c in snap["cands"]:
                base = (frame.url, c["text"], c["hint"], c["row"])
                occ = occurrences.get(base, 0)
                occurrences[base] = occ + 1
                cands.setdefault(base + (occ,), dict(c, steps=steps, frame_idx=idx))

    def _click_key(self, node: Node, c: dict) -> tuple:
        label = (c["text"] or c["hint"]).lower()
        if not c["text"] or GENERIC_CLICK_RE.match(label):
            return (label, c["row"] or node.url)   # "Datasheet" means something else on every page/row
        return (label,)                            # a named item (category, family, part number)

    def _wanted_click(self, node: Node, c: dict) -> tuple[bool, bool]:
        label = (c["text"] or c["hint"]).strip().rstrip(">›»/|").strip()
        if not label or CLICK_SKIP_RE.match(label) or len(label) > 150 or negative_score(label):
            return False, False
        doc = bool(DOC_HINT_RE.search(f"{c['text']} {c['hint']}"))
        if doc and node.product:
            return True, True
        if not c["text"]:
            return False, False  # an icon that does not say it is a document (share, ECAD, zoom, ...)
        if not c["visible"] and not product_score(f"{label} {c['label']}"):
            return False, False  # hidden element: only menus that are clearly the catalogue
        return self._decide_rendered(node, label, c["label"], c["chrome"])

    def _explore_rendered(self, node: Node) -> None:
        page = self.fetcher.browser_context().new_page()
        try:
            self._load(page, node.url)
            if not page.url.startswith("http"):
                return
            snaps = self._harvest(page)
            if not node.product and node.depth == 0:
                text = " ".join(f"{s['title']} {s['headings']}" for _, _, s in snaps)
                node.product = product_score(f"{urlsplit(page.url).path} {text}") > 0
            cands: dict = {}
            self._record_snapshot(node, page, snaps, cands, 0)

            # page through JavaScript tables/lists so that every row is seen
            steps, last_sig = 0, "\n".join(s["sig"] for _, _, s in snaps)
            while node.product and any(s["next"] for _, _, s in snaps) and steps < PART_MAX_PAGINATION:
                if not self._page_next(page):
                    break
                snaps = self._harvest(page)
                sig = "\n".join(s["sig"] for _, _, s in snaps)
                if sig == last_sig:
                    break
                steps, last_sig = steps + 1, sig
                self._record_snapshot(node, page, snaps, cands, steps)

            todo = []
            for ident, c in cands.items():
                follow, product = self._wanted_click(node, c)
                if not follow:
                    continue
                key = self._click_key(node, c)
                if key in self.click_memo or (not c["visible"] and key + ("hidden",) in self.click_memo):
                    continue
                todo.append((ident, c, key, product))
            # document buttons first (they open tabs/downloads and leave the page as it is),
            # visible before hidden (a hidden menu copy of an item often does nothing)
            todo.sort(key=lambda t: (t[1]["steps"], not DOC_HINT_RE.search(f"{t[1]['text']} {t[1]['hint']}"),
                                     not t[1]["visible"]))
            state = {"steps": steps, "dirty": False}
            for ident, c, key, product in todo[:PART_MAX_CLICKS_PER_PAGE]:
                if key in self.click_memo or (not c["visible"] and key + ("hidden",) in self.click_memo):
                    continue
                # A click that leads nowhere blocks the same label everywhere, except when the
                # element was hidden: then a visible element with that label is still tried.
                self.click_memo[key if c["visible"] else key + ("hidden",)] = None
                self._click(node, page, ident, c, key, product, state)
        finally:
            page.close()

    def _find(self, page, ident) -> tuple[object, int] | None:
        frame_url, text, hint, row, occ = ident
        snaps = self._harvest(page)
        if any(frame.url == frame_url for _, frame, _ in snaps):  # else the frame was reloaded: search all
            snaps = [s for s in snaps if s[1].url == frame_url]
        for _, frame, snap in snaps:
            n = 0
            for c in snap["cands"]:
                if (c["text"], c["hint"], c["row"]) == (text, hint, row):
                    if n == occ:
                        return frame, c["i"]
                    n += 1
        return None

    def _click(self, node: Node, page, ident, c: dict, key, product: bool, state: dict, retry: bool = True) -> None:
        label = (c["text"] or c["hint"])[:60]
        if state["dirty"] or state["steps"] != c["steps"]:
            self._goto_state(page, node.url, c["steps"])
            state.update(steps=c["steps"], dirty=False)
        target = self._find(page, ident)
        if target is None:  # not rendered this time: let a later page try the same item
            self.click_memo.pop(key if c["visible"] else key + ("hidden",), None)
            self.log_error("click", f"element {label!r} not found after reload", url=node.url)
            state["dirty"] = True
            return
        frame, i = target
        if self.verbose:
            print(f"      clicking {label!r} (row {c['row'][:40]!r}, page step {c['steps']})")
        ctx = page.context
        popups, downloads = [], []

        def on_popup(p):
            popups.append(p)

        def on_download(d):
            downloads.append(d)

        ctx.on("page", on_popup)
        page.on("download", on_download)
        before = page.url
        try:
            self.fetcher.wait(node.url)
            self.result.clicks += 1
            frame.locator(f'[data-pc-i="{i}"]').first.evaluate("e => e.click()")
            for _ in range(12):  # up to ~3 s for a tab, a download or a route change
                if popups or downloads or page.url != before:
                    break
                page.wait_for_timeout(250)
            if not (popups or downloads or page.url != before):
                self._settle(page, 5000)  # changed in place: let it finish before looking
                self._wait_stable(page, 6000)
        except Exception as exc:  # noqa: BLE001 - element vanished, navigation raced, ...
            self.log_error("click", exc, url=node.url)
            state["dirty"] = True
            return
        finally:
            ctx.remove_listener("page", on_popup)
            page.remove_listener("download", on_download)

        trail = node.trail + (label,)
        targets = [d.url for d in downloads]
        for d in downloads:
            try:
                d.cancel()
            except Exception:  # noqa: BLE001
                pass
        for p in popups:
            try:
                p.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:  # noqa: BLE001 - a PDF tab or a download never "loads"
                pass
            if p.url and p.url != "about:blank":
                targets.append(p.url)
            p.close()
        if page.url.split("#")[0] != before.split("#")[0]:
            targets.append(page.url)
            state["dirty"] = True
        else:
            closed = self._dismiss(page)
            if closed and DOC_HINT_RE.search(f"{c['text']} {c['hint']}"):
                if retry:  # a notice can also mean "the document list has not loaded yet": once more
                    self._wait_stable(page, 6000)
                    return self._click(node, page, ident, c, key, product, state, retry=False)
                message = closed[-1][:300]
                self.result.unavailable.append((node.url, label, message))
                self.log_error("datasheet_unavailable", f"{label} ({c['row'][:80]}): {message}", url=node.url)
            # content shown by the click (a tab, a dropdown) may hold new links
            for _, frame_, snap in self._harvest(page):
                for a in snap["anchors"]:
                    self._handle_anchor(node, a["href"], a["text"], a["label"], a["chrome"], page.url, snap["title"])

        for url in targets:
            url = clean_url(url)
            self.click_memo[key] = url
            if self._is_pdf_target(url, c):
                self._add_pdf(DatasheetLink(url, c["text"] or c["hint"], c["row"],
                                            node.url, "", trail, node.product))
            else:
                self._enqueue(node, url, label, product)
                if self.verbose:
                    print(f"      click {label!r} -> {url}")

    def _is_pdf_target(self, url: str, c: dict) -> bool:
        if path_ext(url) == ".pdf":
            return True
        if not DOC_HINT_RE.search(f"{c['text']} {c['hint']}"):
            return False
        try:
            return self.fetcher.is_pdf(url)
        except FetchError:
            return False
