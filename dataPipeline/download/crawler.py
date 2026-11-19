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
from collections import Counter, deque
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from .fetcher import FetchError, Fetcher, Page
from .settings import (
    CLICK_SKIP_RE, DATASHEET_KEYWORDS, EN_SEGMENTS, LANG_QUERY_KEYS, OTHER_LANG_SEGMENTS, DOC_HINT_RE, DOWNLOAD_HINT_RE, GENERIC_CLICK_RE,
    MAX_HEAD_CHECKS, MODAL_DISMISS_RE, NON_PAGE_EXT, OTHER_DOC_KEYWORDS,
    PAGINATION_RE, PAGINATION_TEXT_RE, PART_MAX_CLICKS_PER_PAGE, PART_MAX_DEPTH,
    PART_MAX_PAGES, PART_MAX_PAGINATION, PART_NEGATIVE_KEYWORDS, PRODUCT_KEYWORDS, URL_NEGATIVE_PREFIXES,
    URL_NEGATIVE_SEGMENTS,
    keyword_score, whole_word_score,
)

CONTEXT_TAGS = ("tr", "li", "article", "dd", "figure", "section")
MULTI_PART_SUFFIXES = {"co", "com", "org", "net", "gov", "ac", "edu"}

# Classes/ids of page-level header, footer and navigation bars (only checked on
# the first few levels below <body>, so "card-header" inside content is not chrome).
CHROME_RE = re.compile(r"(^|[-_\s])(header|footer|foot|navbar|nav|topbar|top-bar)(?=$|[-_\s\d])", re.I)
CHROME_DEPTH = 5
IDLE_WAIT_MS = 1500  # cap for "network idle"; see _settle
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
    # Everything known about where it was found, kept for datasheets that cannot be
    # downloaded: page title/headings/text, section, table headers, the part's row as
    # {header: value}, and the whole table (see page_details / table_details).
    details: dict = field(default_factory=dict)


@dataclass
class PartCrawlResult:
    start_url: str
    pdfs: dict = field(default_factory=dict)      # url -> DatasheetLink (every PDF seen)
    pages_visited: int = 0
    clicks: int = 0
    skipped_pages: int = 0   # --fast: part pages not visited because a table row covered them
    cached_pages: int = 0    # leaf pages not fetched again (visited by an earlier run)
    replayed: int = 0        # clicks not made again: an earlier run's result was used
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


# ------------------------------------------------------------------ context of a link
MAX_TABLE_ROWS = 300   # rows of a table kept in the context of a missing datasheet
MAX_CELL_CHARS = 200
MAX_PAGE_TEXT = 1500


def _cells(tr) -> list[str]:
    return [" ".join(c.get_text(" ", strip=True).split())[:MAX_CELL_CHARS] for c in tr.find_all(["td", "th"], recursive=False)]


def static_table_details(tag) -> dict:
    """Table headers, this row as {header: value}, and the whole table, for a tag in a <tr>."""
    tr = tag.find_parent("tr")
    table = tr.find_parent("table") if tr is not None else None
    if table is None:
        return {}
    headers = [" ".join(th.get_text(" ", strip=True).split()) for th in table.select("thead th, thead td")]
    rows = [r for r in table.find_all("tr") if r.find("td") and r.get_text(strip=True)]
    if not headers:
        first = table.find("tr")
        if first is not None and first.find("th") is not None:
            headers = _cells(first)
    cells = _cells(tr)
    return table_details(headers, cells, [_cells(r) for r in rows])


def table_details(headers: list, cells: list, rows: list) -> dict:
    row = {(headers[i] if i < len(headers) and headers[i] else f"column {i + 1}"): v
           for i, v in enumerate(cells) if v}
    return {"table_headers": headers, "row": row, "row_cells": cells, "table": rows[:MAX_TABLE_ROWS],
            "table_rows_total": len(rows)}


def static_page_details(soup, page_url: str) -> dict:
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heads = [" ".join(h.get_text(" ", strip=True).split()) for h in
             soup.select("h1, h2, h3, [class*=breadcrumb], [class*=crumb]")[:8]]
    body = soup.find("body") or soup
    text = " ".join(body.get_text(" ", strip=True).split())[:MAX_PAGE_TEXT]
    return {"page_url": page_url, "page_title": title, "headings": [h for h in heads if h], "page_text": text}


# ------------------------------------------------------------------ classification
def product_score(text: str) -> int:
    return keyword_score(text, PRODUCT_KEYWORDS)


def negative_score(text: str) -> int:
    return whole_word_score(text, PART_NEGATIVE_KEYWORDS)


def url_negative(url: str) -> bool:
    path = unquote(urlsplit(url).path)
    if negative_score(path) > 0:
        return True
    if any(seg.lower() in URL_NEGATIVE_SEGMENTS for seg in path.split("/")):
        return True
    return any(word.startswith(URL_NEGATIVE_PREFIXES) for word in re.findall(r"[a-z]+", path.lower()))


def is_datasheet(pdf: DatasheetLink) -> bool:
    """Keep datasheets; drop application notes, certificates, brochures, package drawings, ...

    The PDF's own URL and link/button text decide first, then its table row or list
    item, then the sections it was reached through ("Documents > RoHS Report"), and a
    PDF without any hint is kept when it was found inside the catalogue."""
    own = f"{unquote(pdf.url)} {pdf.text} {pdf.trail[-1] if pdf.trail else ''}"
    positive = keyword_score(own, DATASHEET_KEYWORDS)
    negative = keyword_score(own, OTHER_DOC_KEYWORDS) + bool(APP_NOTE_RE.search(own))
    if positive and positive >= negative:
        return True
    if negative:
        return False
    for around in (pdf.context, " ".join(pdf.trail[:-1])):  # its row, then the sections above it
        positive = keyword_score(around, DATASHEET_KEYWORDS)
        negative = keyword_score(around, OTHER_DOC_KEYWORDS) + bool(APP_NOTE_RE.search(around))
        if positive != negative:
            return positive > negative
    return pdf.on_product_page


def is_site_root(url: str) -> bool:
    """https://x.com/, https://x.com/en/, https://x.com/index.php -> True"""
    parts = [p for p in urlsplit(url).path.lower().split("/") if p]
    if parts and re.fullmatch(r"(index|default|home)(\.\w+)?", parts[-1]):
        parts = parts[:-1]
    return not parts or (len(parts) == 1 and (parts[0] in EN_SEGMENTS or parts[0] in OTHER_LANG_SEGMENTS))


def item_token(url: str) -> str:
    """The name a detail page is called after, if it looks like a part number: the last path
    segment with letters and digits, e.g. NSR33001-QHMSR in /en/product/NSR33001-QHMSR."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    last = unquote(parts[-1]) if parts else ""
    last = re.sub(r"\.(html?|php|aspx?)$", "", last, flags=re.I)
    return last if len(last) >= 4 and re.search(r"\d", last) and re.search(r"[A-Za-z]", last) else ""


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

  // Tables: headers (also from a separate fixed-header table), rows, and each element's row
  const tables = [], tableIndex = new Map();
  const cellsOf = tr => [...tr.children].map(c => textOf(c).slice(0, 200));
  const headersOf = table => {
    let ths = [...table.querySelectorAll('thead th, thead td')];
    if (!ths.length) {
      const first = table.querySelector('tr');
      if (first && first.querySelector('th')) ths = [...first.children];
    }
    for (let p = table.parentElement, n = 0; !ths.length && p && n < 5; p = p.parentElement, n++) {
      ths = [...p.querySelectorAll('thead th')];
    }
    return ths.map(textOf);
  };
  const tableOf = el => {
    const tr = el.closest('tr');
    const table = tr && tr.closest('table');
    if (!table) return null;
    if (!tableIndex.has(table)) {
      const rows = [...table.querySelectorAll('tr')].filter(r => r.querySelector('td') && clean(r.innerText))
                   .map(cellsOf);
      tableIndex.set(table, tables.length);
      tables.push({headers: headersOf(table), rows: rows.slice(0, args.maxRows), total: rows.length});
    }
    return {table: tableIndex.get(table), cells: cellsOf(tr)};
  };

  const out = {title: document.title || '', headings: '', anchors: [], frames: [], cands: [], next: false, sig: '',
               tables, text: document.body ? clean(document.body.innerText).slice(0, args.maxText) : ''};
  out.headings = clean([...document.querySelectorAll('h1, h2, [class*=breadcrumb], [class*=crumb]')]
                       .slice(0, 6).map(textOf).join(' | ')).slice(0, 400);
  for (const a of document.querySelectorAll('a[href], area[href]')) {
    if (!realHref(a)) continue;
    const own = textOf(a) || hintOf(a);
    const label = labelOf(a);
    const isPdf = /\.pdf($|[?#])/i.test(a.href);
    out.anchors.push({href: a.href, text: own, label, context: clean(label + ' | ' + (rowOf(a) || groupOf(a, own))),
                      chrome: inChrome(a), tbl: isPdf ? tableOf(a) : null});
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
    out.cands.push({i: i++, text, hint, row: rowOf(el), label, tbl: tableOf(el),
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
                 max_depth=PART_MAX_DEPTH, js="auto", verbose=False, on_missing=None, fast=False,
                 page_cache=None, on_leaf=None, click_cache=None, on_click=None):
        self.fetcher = fetcher
        self.log_error = log_error
        self.on_pdf = on_pdf or (lambda pdf: None)
        # on_missing(stage, message, url, details): a datasheet the site shows but does not
        # give (e.g. "not publicly available"), with everything known about the part.
        self.on_missing = on_missing or (lambda stage, message, url, details:
                                         self.log_error(stage, message, url=url))
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.js = js  # auto | always | never
        self.verbose = verbose
        self.hosts: set[str] = set()
        self.seen: set[str] = set()
        self.queue: deque[Node] = deque()
        self.click_memo: dict = {}
        # fast: once a table row's document button gave a PDF (or a "not available" notice), the
        # row's other items (part number -> part page) are not clicked
        self.fast = fast
        # Leaf pages from earlier runs (url -> PDF URLs): pages that gave datasheet links and led
        # nowhere new. They are not fetched again; on_leaf(url, pdf_urls) reports new ones.
        self.page_cache = page_cache or {}
        self.on_leaf = on_leaf or (lambda url, pdfs: None)
        self._page_pdfs: list[str] = []
        self._page_notices = 0
        # Clicks of earlier runs (JavaScript sites): click key -> (kind, url), kind = nav | pdf |
        # notice | none. A known click is replayed instead of clicked; on_click reports new ones.
        self.click_cache = click_cache or {}
        self.on_click = on_click or (lambda key, kind, url: None)
        self.rows_with_doc: set[str] = set()   # table rows whose document button gave a PDF or a notice
        self.rows_with_pdf: set[str] = set()   # ... that gave a PDF
        self.later: deque[Node] = deque()      # --fast: part pages, visited after everything else
        self.dead_labels: Counter = Counter()  # labels whose clicks did nothing
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
        started = last_report = time.monotonic()
        while (self.queue or self.later) and self.result.pages_visited < self.max_pages:
            if self.queue:
                node = self.queue.popleft()
            else:  # --fast: part pages last, and only for parts no table row has covered
                node = self.later.popleft()
                if self._part_covered(node.url, self.rows_with_doc):
                    self.result.skipped_pages += 1
                    if self.verbose:
                        print(f"      skip part page {node.url}: its table row already gave its datasheet (--fast)")
                    continue
            if node.depth > 0 and node.url in self.page_cache:
                self.result.cached_pages += 1  # visited by an earlier run: its datasheets are known
                if self.verbose:
                    print(f"      cached {node.url} ({len(self.page_cache[node.url])} PDF links)")
                continue
            if not self.fetcher.allowed(node.url):
                self.log_error("robots", "disallowed by robots.txt", url=node.url)
                continue
            self._page_pdfs, self._page_notices, links_before = [], 0, len(self.seen)
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
            if (self._page_pdfs or self._page_notices) and len(self.seen) == links_before and node.depth > 0:
                # gave datasheets (or "not available" notices) and led nowhere new
                self.on_leaf(node.url, sorted(set(self._page_pdfs)))
            # every page with -v; otherwise every 25 pages, and at least once a minute
            # (a JavaScript catalogue page can take a minute or two)
            if self.verbose or self.result.pages_visited % 25 == 0 or time.monotonic() - last_report >= 60:
                last_report = time.monotonic()
                print(f"    .. {self.result.pages_visited} pages, {self.result.cached_pages} cached, "
                      f"{len(self.queue)} queued, {self.result.replayed} clicks replayed, "
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

    @staticmethod
    def _part_covered(url: str, rows) -> bool:
        """True if the part a page is named after (/product/NSR33001-QHMSR) appears in one of `rows`."""
        token = item_token(url)
        return bool(token) and any(token.lower() in r.lower() for r in rows)

    def _enqueue(self, node: Node, url: str, text: str, product: bool, later: bool = False) -> None:
        url = clean_url(url)
        if url in self.seen or node.depth + 1 > self.max_depth:
            return
        if not url.startswith(("http://", "https://")) or not self._allowed_host(url):
            return
        if path_ext(url) in NON_PAGE_EXT or is_other_language(url):
            return
        if url_negative(url):  # /news-details/..., /newsshow.php, /careers/...
            return
        self.seen.add(url)
        (self.later if later else self.queue).append(Node(url, node.depth + 1, product, node.trail + (text[:60],)))

    def _handle_anchor(self, node: Node, href: str, text: str, context: str, chrome: bool,
                       page_url: str, title: str, head_candidates: dict | None = None,
                       details=None) -> None:
        """`details`: a function returning the context dict of this link (called only for PDFs)."""
        link = clean_url(href)
        if not link.startswith(("http://", "https://")):
            return
        label = text or unquote(urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1])  # for the trail
        if path_ext(link) == ".pdf":  # every PDF is collected; is_datasheet() decides later
            self._page_pdfs.append(link)
            if link not in self.result.pdfs:
                self._add_pdf(DatasheetLink(link, text, context, page_url, title, node.trail + (label[:60],),
                                            node.product, details=self._link_details(node, label, details)))
            return
        pagination = node.product and (PAGINATION_RE.search(link) or PAGINATION_TEXT_RE.match(text.strip()))
        follow, product = (True, True) if pagination and not chrome else self._decide(node, text, context, chrome)
        if follow:
            self._enqueue(node, link, label, product)
        elif head_candidates is not None and node.product and path_ext(link) in ("", ".ashx", ".axd") \
                and DOWNLOAD_HINT_RE.search(urlsplit(link).path) and link not in head_candidates:
            head_candidates[link] = DatasheetLink(link, text, context, page_url, title, node.trail + (label[:60],),
                                                  node.product, details=self._link_details(node, label, details))

    @staticmethod
    def _link_details(node: Node, label: str, details) -> dict:
        d = dict(details() if callable(details) else (details or {}))
        d.setdefault("element", label)
        d["trail"] = list(node.trail) + [label]
        return d

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
        page_info = None

        def details_of(a):
            def build():
                nonlocal page_info
                if page_info is None:
                    page_info = static_page_details(soup, page.url)
                return {**page_info, "section": link_context(a), "element": element_text(a),
                        **static_table_details(a)}
            return build

        for a in soup.find_all(["a", "area"], href=True):
            text = element_text(a)
            self._handle_anchor(node, urljoin(page.url, a["href"]), text, link_context(a),
                                in_chrome_static(a), page.url, title, head_candidates, details=details_of(a))

    # ------------------------------------------------------------ rendered pages (clicking)
    def _settle(self, page, idle_ms: int = IDLE_WAIT_MS) -> None:
        """A short wait for the network to go quiet. Sites with analytics that poll never go idle,
        so this is capped low; _wait_stable (no frame changes any more) does the real waiting."""
        try:
            page.wait_for_load_state("networkidle", timeout=idle_ms)
        except Exception:  # noqa: BLE001 - pages with endless polling never go idle
            pass
        page.wait_for_timeout(300)

    def _new_page(self):
        """A browser tab that keeps count of its unfinished page/data requests (see _wait_stable)."""
        page = self.fetcher.browser_context().new_page()
        pending: dict = {}
        page._pc_pending = pending

        def started(req):
            if req.resource_type in ("document", "xhr", "fetch"):
                pending[req] = time.monotonic()

        def ended(req):
            pending.pop(req, None)
        page.on("request", started)
        page.on("requestfinished", ended)
        page.on("requestfailed", ended)
        return page

    @staticmethod
    def _requests_in_flight(page) -> int:
        """Unfinished page/data requests, ignoring ones open > 10 s (long polling, analytics)."""
        now = time.monotonic()
        return sum(1 for t in getattr(page, "_pc_pending", {}).values() if now - t < 10)

    def _wait_stable(self, page, max_ms: int = 20000) -> None:
        """Wait until no frame changes any more, no page/data request is in flight, and every
        embedded frame has content (catalogue tables are often in an iframe that is created
        and filled seconds after the page has "loaded")."""
        def measure():
            sizes, empty = [], 0
            for frame in page.frames:
                try:
                    n, chars = frame.evaluate(
                        "() => document.body ? [document.body.getElementsByTagName('*').length,"
                        " (document.body.innerText || '').length] : [0, 0]")
                except Exception:  # noqa: BLE001 - detached frame
                    continue
                sizes.append((n, chars))
                if frame is not page.main_frame and frame.url.startswith("http") and (n < 10 or chars < 20):
                    empty += 1  # an embedded frame without text is still loading its content
            return sizes, empty
        last, stable, waited = None, 0, 0
        while waited < max_ms:
            page.wait_for_timeout(500)
            waited += 500
            now, empty = measure()
            stable = stable + 1 if now == last else 0
            last = now
            if stable >= 2 and not empty and not self._requests_in_flight(page):
                break

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
        for attempt in range(1, 4):  # connection resets, timeouts, iframes that failed: wait, try again
            self.fetcher.wait(url)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001
                if attempt == 3 or not re.search(r"net::ERR_|Timeout", str(exc)):
                    raise
                page.wait_for_timeout(5000 * attempt)
                continue
            if resp is not None and resp.status >= 400:
                raise FetchError(f"HTTP {resp.status}", resp.status)
            self._settle(page)
            broken = [f.url for f in page.frames if f.url.startswith("chrome-error://")]
            if not broken:
                break
            if attempt == 3:
                self.log_error("render", f"{len(broken)} frame(s) failed to load after 3 tries", url=url)
            else:
                page.wait_for_timeout(5000 * attempt)
        self._wait_stable(page)
        page.wait_for_timeout(500)  # late pop-ups (language choosers, cookie banners)
        if self._dismiss(page):
            self._wait_stable(page)
        for _ in range(3):  # trigger lazy loading
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(250)

    def _harvest(self, page) -> list[tuple[int, object, dict]]:
        snaps = []
        args = {"chromeRe": CHROME_RE.pattern, "chromeDepth": CHROME_DEPTH,
                "maxRows": MAX_TABLE_ROWS, "maxText": MAX_PAGE_TEXT}
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
                    self._settle(page)
                    self._wait_stable(page)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _go_back(self, page, url: str) -> bool:
        """Return to `url` with the browser's Back button; False if that did not get there."""
        try:
            page.go_back(wait_until="commit", timeout=15000)
        except Exception:  # noqa: BLE001
            return False
        if page.url.split("#")[0] != url.split("#")[0]:
            return False
        self._settle(page)
        self._wait_stable(page)
        self._dismiss(page)
        return True

    def _goto_state(self, page, url: str, steps: int) -> None:
        """Reload the page and press "next page" `steps` times."""
        self._load(page, url)
        for _ in range(steps):
            self._harvest(page)
            if not self._page_next(page):
                break
        self._harvest(page)

    @staticmethod
    def _rendered_details(page_url: str, snaps, snap: dict, item: dict, label: str) -> dict:
        """Context of a link/element of a rendered page: page, section, table row, whole table."""
        heads = [h for _, _, s in snaps for h in s["headings"].split(" | ") if h]
        d = {"page_url": page_url, "page_title": next((s["title"] for _, _, s in snaps if s["title"]), ""),
             "headings": heads[:12], "page_text": snap["text"] or next((s["text"] for _, _, s in snaps), ""),
             "section": item.get("label", ""), "element": label}
        if item.get("row"):
            d["row_text"] = item["row"]
        tbl = item.get("tbl")
        if tbl is not None:
            t = snap["tables"][tbl["table"]]
            d.update(table_details(t["headers"], tbl["cells"], t["rows"]))
            d["table_rows_total"] = t["total"]
        return d

    def _record_snapshot(self, node: Node, page, snaps, cands: dict, steps: int) -> None:
        top_title = ""
        for idx, frame, snap in snaps:
            top_title = top_title or snap["title"]
            for src in snap["frames"]:
                if src.startswith("http") and (node.product or product_score(src)):
                    self.hosts.add(urlsplit(src).netloc.lower())
            for a in snap["anchors"]:
                self._handle_anchor(node, a["href"], a["text"], a["label"], a["chrome"], page.url, top_title,
                                    details=lambda a=a, snap=snap: self._rendered_details(
                                        page.url, snaps, snap, a, a["text"]))
            occurrences: dict = {}
            for c in snap["cands"]:
                base = (frame.url, c["text"], c["hint"], c["row"])
                occ = occurrences.get(base, 0)
                occurrences[base] = occ + 1
                if base + (occ,) not in cands:
                    details = self._rendered_details(page.url, snaps, snap, c, c["text"] or c["hint"])
                    cands[base + (occ,)] = dict(c, steps=steps, frame_idx=idx, details=details)

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
        page = self._new_page()
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
            # (and a table row's document button before the page's own one)
            todo.sort(key=lambda t: (t[1]["steps"], not DOC_HINT_RE.search(f"{t[1]['text']} {t[1]['hint']}"),
                                     not t[1]["row"], not t[1]["visible"]))
            state = {"steps": steps, "dirty": False}
            for ident, c, key, product in todo[:PART_MAX_CLICKS_PER_PAGE]:
                if key in self.click_memo or (not c["visible"] and key + ("hidden",) in self.click_memo):
                    continue
                if not DOC_HINT_RE.search(f"{c['text']} {c['hint']}") \
                        and self.dead_labels[(c["text"] or c["hint"])[:60].lower()] >= 3:
                    continue  # this label never did anything on this site
                if self.fast and c["row"] in self.rows_with_doc \
                        and not DOC_HINT_RE.search(f"{c['text']} {c['hint']}"):
                    if self.verbose:
                        print(f"      skip {(c['text'] or c['hint'])[:40]!r}: its row already gave its datasheet (--fast)")
                    continue
                if key in self.click_cache:  # clicked by an earlier run: use what it led to
                    self._replay(node, c, key, product, *self.click_cache[key])
                    continue
                # A click that leads nowhere blocks the same label everywhere, except when the
                # element was hidden: then a visible element with that label is still tried.
                self.click_memo[key if c["visible"] else key + ("hidden",)] = None
                self._click(node, page, ident, c, key, product, state)
        finally:
            page.close()

    def _replay(self, node: Node, c: dict, key, product: bool, kind: str, url: str | None) -> None:
        """Do what an earlier run's click of this element did, without clicking."""
        label = (c["text"] or c["hint"])[:60]
        self.click_memo[key] = url
        self.result.replayed += 1
        if kind == "pdf" and url:
            self._page_pdfs.append(url)
            if c["row"]:
                self.rows_with_doc.add(c["row"])
                self.rows_with_pdf.add(c["row"])
            if url not in self.result.pdfs:  # normally known; downloaded if an earlier run was cut short
                details = dict(c.get("details") or {}, trail=list(node.trail) + [label])
                self._add_pdf(DatasheetLink(url, c["text"] or c["hint"], c["row"], node.url,
                                            details.get("page_title", ""), node.trail + (label,), node.product,
                                            details=details))
        elif kind == "notice":
            self._page_notices += 1
            if c["row"]:
                self.rows_with_doc.add(c["row"])
        elif kind == "nav" and url:
            token = item_token(url)
            later = self.fast and token and token.lower() in label.lower()
            self._enqueue(node, url, label, product, later=later)
        if self.verbose:
            print(f"      cached click {label!r} -> {kind}{' ' + url if url else ''}")

    def retry_click(self, page_url: str, element: str, row_text: str = "", trail=()) -> list:
        """Open `page_url`, find the clickable element with text/icon text `element` (in the table
        row `row_text`, paging through JavaScript tables if needed), click it once more and return
        the PDFs that click produced. Used by run.py --retry-failed for "not available" notices."""
        if self.result is None:
            self.result = PartCrawlResult(start_url=page_url)
        self.hosts.add(urlsplit(page_url).netloc.lower())
        node = Node(page_url, 0, True, tuple(trail)[:-1])
        found_before, on_pdf = set(self.result.pdfs), self.on_pdf
        self.on_pdf = lambda pdf: None  # links merely seen on the page are not part of the retry
        page = self._new_page()
        try:
            self._load(page, page_url)
            for steps in range(PART_MAX_PAGINATION + 1):
                snaps = self._harvest(page)
                cands: dict = {}
                self._record_snapshot(node, page, snaps, cands, steps)
                match = next(((ident, c) for ident, c in cands.items()
                              if (c["text"] or c["hint"]) == element and (not row_text or c["row"] == row_text)),
                             None)
                if match is not None:
                    break
                if not any(s["next"] for _, _, s in snaps) or not self._page_next(page):
                    self.log_error("click", f"element {element!r} (row {row_text[:60]!r}) not found", url=page_url)
                    return []
            else:
                return []
            ident, c = match
            seen_before = set(self.result.pdfs) - found_before  # PDFs on the page itself
            self._click(node, page, ident, c, self._click_key(node, c), True, {"steps": steps, "dirty": False})
        finally:
            page.close()
            self.on_pdf = on_pdf
        return [p for u, p in self.result.pdfs.items() if u not in found_before and u not in seen_before]

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
        went_back = False
        if state["dirty"] and c["steps"] == 0 and state.get("back_ok", True):
            went_back = self._go_back(page, node.url)  # much faster than a reload in single-page apps
            if went_back:
                state.update(steps=0, dirty=False)
        if state["dirty"] or state["steps"] != c["steps"]:
            self._goto_state(page, node.url, c["steps"])
            state.update(steps=c["steps"], dirty=False)
        target = self._find(page, ident)
        if target is None and went_back:  # "back" did not restore the page: reload, and stop using it here
            state["back_ok"] = False
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
                self._settle(page)  # changed in place: let it finish before looking
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
            # the tab's URL is known as soon as it starts navigating; a tab showing a PDF never
            # finishes "loading", so waiting for that only costs time
            for _ in range(20):
                if p.url and p.url != "about:blank":
                    break
                p.wait_for_timeout(250)
            if p.url and p.url != "about:blank":
                targets.append(p.url)
            p.close()
        if page.url.split("#")[0] != before.split("#")[0]:
            targets.append(page.url)
            state["dirty"] = True
        else:
            closed = self._dismiss(page)
            if self.verbose and not targets:
                print(f"      click {label!r} -> " + (f"notice: {closed[-1][:100]!r}" if closed else
                                                       "no new page, tab or download"))
            is_doc = bool(DOC_HINT_RE.search(f"{c['text']} {c['hint']}"))
            if not targets and not closed and not is_doc:
                self.dead_labels[label.lower()] += 1  # e.g. "More Products": stop after 3 tries
                self.on_click(key, "none", None)
            if closed and is_doc and not c["row"] and self._part_covered(node.url, self.rows_with_pdf):
                # the page's own "Datasheet" button says "not available", but the part's table row
                # already gave the PDF: not a missing datasheet
                if self.verbose:
                    print("      (ignored: this part's datasheet was already obtained from its table row)")
                closed = []
            if closed and is_doc:
                if retry and not state.get("doc_ok"):
                    # a notice can also mean "the document list has not loaded yet": once more (not
                    # needed once another document button on this page has given a PDF)
                    self._wait_stable(page, 6000)
                    return self._click(node, page, ident, c, key, product, state, retry=False)
                message = closed[-1][:300]
                if c["row"]:
                    self.rows_with_doc.add(c["row"])
                self.result.unavailable.append((node.url, label, message))
                self._page_notices += 1
                self.on_click(key, "notice", None)
                details = dict(c.get("details") or {}, trail=list(node.trail) + [label], message=message)
                self.on_missing("datasheet_unavailable", f"{label} ({c['row'][:80]}): {message}", node.url, details)
            # content shown by the click (a tab, a dropdown) may hold new links
            snaps = self._harvest(page)
            for _, frame_, snap in snaps:
                for a in snap["anchors"]:
                    self._handle_anchor(node, a["href"], a["text"], a["label"], a["chrome"], page.url, snap["title"],
                                        details=lambda a=a, snap=snap: self._rendered_details(
                                            page.url, snaps, snap, a, a["text"]))

        for url in targets:
            url = clean_url(url)
            self.click_memo[key] = url
            if self._is_pdf_target(url, c):
                state["doc_ok"] = True
                self._page_pdfs.append(url)
                self.on_click(key, "pdf", url)
                if self.verbose:
                    print(f"      click {label!r} -> PDF {url}")
                if c["row"]:
                    self.rows_with_doc.add(c["row"])
                    self.rows_with_pdf.add(c["row"])
                details = dict(c.get("details") or {}, trail=list(trail))
                self._add_pdf(DatasheetLink(url, c["text"] or c["hint"], c["row"], node.url,
                                            details.get("page_title", ""), trail, node.product, details=details))
            else:
                token = item_token(url)
                later = self.fast and token and token.lower() in label.lower()  # a part's own page
                self._enqueue(node, url, label, product, later=later)
                if url.startswith(("http://", "https://")):
                    self.on_click(key, "nav", url)
                if self.verbose:
                    print(f"      click {label!r} -> {url}" + ("  (part page: later, if still needed)" if later else ""))

    def _is_pdf_target(self, url: str, c: dict) -> bool:
        if path_ext(url) == ".pdf":
            return True
        if not DOC_HINT_RE.search(f"{c['text']} {c['hint']}"):
            return False
        try:
            return self.fetcher.is_pdf(url)
        except FetchError:
            return False
