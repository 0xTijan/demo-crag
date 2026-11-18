"""Rules of the datasheet crawler (dataPipeline/download/crawler.py), on synthetic data.

A fake fetcher serves a tiny static catalogue (homepage -> products -> family list
with two pages -> part pages -> PDFs), so there is no network and no browser. The
JavaScript click path needs a real browser and is checked by hand (see
dataPipeline/download/README.md, section 4).
Run: python tests/test_download_crawler.py   (or pytest)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataPipeline.download.fetcher import Page  # noqa: E402
from dataPipeline.download.crawler import (  # noqa: E402
    DatasheetLink, Node, PartCrawler, is_datasheet, same_host,
)

HEADER = """<div class="header"><a href="/index.php">Home</a><a href="/news.php">News</a>
<a href="/product.php">Products</a><a href="http://www.example-semi.com/">中文</a></div>"""
FOOTER = """<div class="foot"><a href="/jobs.php">Careers</a><a href="/about.php">Company profile</a></div>"""


def page(body: str, title: str = "Example Semi") -> str:
    return f"<html><head><title>{title}</title></head><body>{HEADER}<div class='main'>{body}</div>{FOOTER}</body></html>"


SITE = {
    "https://en.example-semi.com/": page("<p>Welcome</p>" + "<p>filler text</p>" * 40),
    "https://en.example-semi.com/product.php": page(
        "<ul><li><a href='prolist.php?id=1'>General Rectifier</a></li>"
        "<li><a href='prolist.php?id=2'>TVS</a></li></ul>"
        "<a href='/files/catalogue_2026.pdf'>Product catalogue</a>"),
    "https://en.example-semi.com/prolist.php?id=1": page(
        "<ul><li><a href='proshow.php?id=10'>1A1</a></li><li><a href='proshow.php?id=11'>1A2</a></li></ul>"
        "<div class='fy'><a href='prolist.php?id=1&page=2'>2</a></div>"),
    "https://en.example-semi.com/prolist.php?id=1&page=2": page(
        "<ul><li><a href='proshow.php?id=12'>1A3</a></li></ul>"),
    "https://en.example-semi.com/prolist.php?id=2": page("<ul><li><a href='proshow.php?id=20'>SMAJ5.0A</a></li></ul>"),
    "https://en.example-semi.com/proshow.php?id=10": page("<a href='specification/1A1 thru 1A7.pdf'><img src='x.png'></a>"),
    "https://en.example-semi.com/proshow.php?id=11": page("<a href='specification/1A1 thru 1A7.pdf'><img src='x.png'></a>"),
    "https://en.example-semi.com/proshow.php?id=12": page("<a href='specification/1A1 thru 1A7.pdf'><img src='x.png'></a>"),
    "https://en.example-semi.com/prolist.php?id=3": page(
        "<h1>Schottky Rectifier</h1><table><thead><tr><th>Part</th><th>VRRM (V)</th><th>Package</th><th>PDF</th></tr></thead>"
        "<tbody><tr><td>SS34</td><td>40</td><td>SMA</td><td><a href='/specification/SS34.pdf'>Datasheet</a></td></tr>"
        "<tr><td>SS36</td><td>60</td><td>SMA</td><td><a href='/specification/SS36.pdf'>Datasheet</a></td></tr>"
        "</tbody></table>", title="Schottky Rectifier - Example Semi"),
    "https://en.example-semi.com/proshow.php?id=20": page(
        "<a href='specification/SMAJ.pdf'>Datasheet</a> <a href='/files/AN-12-0001_TVS_layout.pdf'>Application note</a>"),
}


class FakeFetcher:
    def __init__(self):
        self.requested = []

    def get_page(self, url):
        self.requested.append(url)
        url = url.replace(" ", "%20")
        if url not in SITE:
            from dataPipeline.download.fetcher import FetchError
            raise FetchError("HTTP 404", 404)
        return Page(url, 200, "text/html", SITE[url])

    def allowed(self, url):
        return True

    def is_pdf(self, url):
        return False


def crawl(start):
    fetcher, errors, found = FakeFetcher(), [], []
    crawler = PartCrawler(fetcher, lambda *a, **k: errors.append(a), on_pdf=found.append, js="never")
    result = crawler.crawl(start)
    return result, fetcher, found


def test_static_catalogue_walks_families_pages_and_parts():
    result, fetcher, found = crawl("https://en.example-semi.com/product.php")
    urls = set(result.pdfs)
    assert "https://en.example-semi.com/specification/1A1 thru 1A7.pdf" in urls
    assert "https://en.example-semi.com/specification/SMAJ.pdf" in urls
    assert "https://en.example-semi.com/proshow.php?id=12" in fetcher.requested  # reached through page 2
    assert len(found) == len(urls)  # every PDF reported once, as soon as found


def test_header_footer_and_other_languages_are_not_followed():
    _, fetcher, _ = crawl("https://en.example-semi.com/product.php")
    for url in fetcher.requested:
        assert "news" not in url and "jobs" not in url and "about" not in url
        assert "www.example-semi.com" not in url  # Chinese site on another subdomain


def test_datasheet_filter_drops_catalogues_and_app_notes():
    result, _, _ = crawl("https://en.example-semi.com/product.php")
    kept = {u.rsplit("/", 1)[-1] for u, p in result.pdfs.items() if is_datasheet(p)}
    assert kept == {"1A1 thru 1A7.pdf", "SMAJ.pdf"}


def test_homepage_reaches_catalogue_through_products_link():
    result, _, _ = crawl("https://en.example-semi.com/")
    assert "https://en.example-semi.com/specification/SMAJ.pdf" in result.pdfs


def test_pdf_links_carry_table_and_page_context():
    result, _, _ = crawl("https://en.example-semi.com/prolist.php?id=3")
    d = result.pdfs["https://en.example-semi.com/specification/SS36.pdf"].details
    assert d["table_headers"] == ["Part", "VRRM (V)", "Package", "PDF"]
    assert d["row"] == {"Part": "SS36", "VRRM (V)": "60", "Package": "SMA", "PDF": "Datasheet"}
    assert len(d["table"]) == 2 and d["table"][0][0] == "SS34"
    assert d["page_title"] == "Schottky Rectifier - Example Semi" and "Schottky Rectifier" in d["headings"]
    assert d["page_url"] == "https://en.example-semi.com/prolist.php?id=3" and d["trail"][-1] == "Datasheet"


def test_failed_download_is_recorded_with_context():
    import json
    import tempfile
    from types import SimpleNamespace
    from dataPipeline.download import run as run_mod
    from dataPipeline.download.fetcher import FetchError

    class NotFound:
        def stream(self, url):
            raise FetchError("HTTP 404", 404)

    logged = []
    old_dir = run_mod.DATASHEETS_DIR
    run_mod.DATASHEETS_DIR = Path(tempfile.mkdtemp())
    try:
        args = SimpleNamespace(all_pdfs=False, dry_run=False, force=False, verbose=False)
        saver = run_mod.Saver(NotFound(), None, "examplesemi", args, lambda *a, **k: logged.append((a, k)))
        result, _, _ = crawl("https://en.example-semi.com/prolist.php?id=3")
        saver(result.pdfs["https://en.example-semi.com/specification/SS36.pdf"])
        rows = [json.loads(line) for line in (saver.folder / run_mod.MISSING).read_text().splitlines()]
    finally:
        run_mod.DATASHEETS_DIR = old_dir
    assert saver.stats["missing"] == 1 and saver.stats["failed"] == 1
    r = rows[0]
    assert r["stage"] == "download" and r["http_status"] == 404 and r["url"].endswith("SS36.pdf")
    assert r["row"]["Part"] == "SS36" and r["table_headers"][1] == "VRRM (V)"
    assert logged[0][1]["context"]["row"]["Package"] == "SMA"  # same record goes to datasheet_errors.context


def test_retry_failed_removes_recovered_rows_and_replaces_failing_ones():
    import tempfile
    from types import SimpleNamespace
    from dataPipeline.download import db as db_mod, run as run_mod
    from dataPipeline.download.fetcher import FetchError

    good = b"%PDF-1.7 recovered"

    class Resp:
        status_code, headers = 200, {"content-type": "application/pdf", "content-length": str(len(good))}

        def iter_bytes(self, n):
            yield good

        def close(self):
            pass

    class Fetcher:
        def stream(self, url):
            if "gone" in url:
                raise FetchError("HTTP 404", 404)
            return Resp()

    rows = [  # two runs logged the same failure; one URL is really gone
        {"error_id": 1, "manufacturer": "m", "url": "https://x/ok.pdf", "stage": "download", "error": "HTTP 503",
         "http_status": 503, "context": {"page_url": "https://x/p", "row": {"Part": "A1"}, "trail": ["A1"]}},
        {"error_id": 2, "manufacturer": "m", "url": "https://x/ok.pdf", "stage": "download", "error": "HTTP 503",
         "http_status": 503, "context": {"page_url": "https://x/p", "row": {"Part": "A1"}, "trail": ["A1"]}},
        {"error_id": 3, "manufacturer": "m", "url": "https://x/gone.pdf", "stage": "download", "error": "HTTP 404",
         "http_status": 404, "context": {"page_url": "https://x/p", "row": {"Part": "B2"}}},
    ]
    deleted, logged = [], []
    saved = dict(failed=db_mod.failed_datasheets, delete=db_mod.delete_errors, known=db_mod.known_datasheets,
                 record=db_mod.record_datasheet, log=db_mod.ErrorLog, folder=run_mod.DATASHEETS_DIR)
    db_mod.failed_datasheets = lambda engine, names: rows
    db_mod.delete_errors = lambda engine, ids: deleted.extend(ids)
    db_mod.known_datasheets = lambda engine, name: ({}, {})
    db_mod.record_datasheet = lambda engine, row: logged.append(("file", row["source_url"]))
    db_mod.ErrorLog = lambda engine, name: (lambda *a, **k: logged.append(("error", a[0], k.get("url"))))
    run_mod.DATASHEETS_DIR = Path(tempfile.mkdtemp())
    try:
        args = SimpleNamespace(all_pdfs=True, dry_run=False, force=False, verbose=False)
        totals = run_mod.retry_failed(object(), Fetcher(), None, args)
    finally:
        db_mod.failed_datasheets, db_mod.delete_errors = saved["failed"], saved["delete"]
        db_mod.known_datasheets, db_mod.record_datasheet, db_mod.ErrorLog = saved["known"], saved["record"], saved["log"]
        run_mod.DATASHEETS_DIR = saved["folder"]
    assert totals["recovered"] == 1 and totals["still_failing"] == 1
    assert ("file", "https://x/ok.pdf") in logged                       # saved like any download
    assert ("error", "download", "https://x/gone.pdf") in logged         # a fresh row for the one still failing ...
    assert sorted(deleted) == [1, 2, 3]                                  # ... replaces its old row; recovered rows go


def test_is_datasheet_rules():
    def pdf(url, text="", context="", trail=("",), product=True):
        return DatasheetLink(url, text, context, "https://x/p", "", trail, product)
    assert is_datasheet(pdf("https://x/en/datasheet/NSM4101/NSM41xx_Rev1.2.pdf", "Datasheet"))
    assert is_datasheet(pdf("https://x/files/abc.pdf", "", "1A1 1A2 | Datasheet"))      # row says so
    assert is_datasheet(pdf("https://x/files/abc.pdf"))                                   # no hint, catalogue page
    assert not is_datasheet(pdf("https://x/files/abc.pdf", product=False))
    assert not is_datasheet(pdf("https://x/docs/AN-13-0012_RS485_Reference.pdf"))
    assert not is_datasheet(pdf("https://x/docs/NSI1150_User_Guide.pdf"))
    assert not is_datasheet(pdf("https://x/docs/quality/ISO9001_certificate.pdf"))
    # sections it was reached through, and Chinese file names (no spaces between words)
    assert not is_datasheet(pdf("https://x/pdf/x123.pdf", trail=("Documents", "RoHS Report", "x123.pdf")))
    assert not is_datasheet(pdf("https://x/pdf/苏州固锝：2023年社会责任报告.pdf", trail=("Documents", "Other", "")))
    assert is_datasheet(pdf("https://x/pdf/SS34_规格书.pdf"))


def test_rendered_click_decisions():
    crawler = PartCrawler(FakeFetcher(), lambda *a, **k: None)
    home, catalogue = Node("https://x/", 0, False), Node("https://x/sensor", 1, True)

    def cand(text, label="", chrome=False, hint="", visible=True):
        return {"text": text, "hint": hint, "label": label, "row": "", "context": label,
                "chrome": chrome, "visible": visible}
    # homepage: a footer item counts when its section is "Products", not "Applications"
    assert crawler._wanted_click(home, cand("Sensor", "Products", chrome=True)) == (True, True)
    assert crawler._wanted_click(home, cand("Automotive", "Applications", chrome=True))[0] is False
    # inside the catalogue: content is clicked, header items only when they are about products
    assert crawler._wanted_click(catalogue, cand("Magnetic Sensor")) == (True, True)
    assert crawler._wanted_click(catalogue, cand("Support", chrome=True))[0] is False
    assert crawler._wanted_click(catalogue, cand("", hint="dataSheet")) == (True, True)
    # never: controls, languages, sample/buy/login
    for text in ("Reset Table", "Export", "English", "Sample Application", "Buy Now", "Login/Register", "10 / page"):
        assert crawler._wanted_click(catalogue, cand(text))[0] is False, text


def test_negative_keywords_are_whole_words():
    from dataPipeline.download.crawler import negative_score
    for product in ("Pressure Sensor", "Sampling ADC", "Video Amplifiers", "Contactless Sensors", "Cartridge Heater"):
        assert negative_score(product) == 0, product
    for nav in ("Careers", "News & Events", "Sample Application", "Buy Now", "/en/news-details/123",
                "/en/documents-download", "Investor Relations"):
        assert negative_score(nav) > 0, nav
    from dataPipeline.download.crawler import url_negative
    assert url_negative("https://x.com/newsshow.php?id=7") and url_negative("https://x.com/en/news-details/1")
    assert not url_negative("https://x.com/en/pressure-sensor") and not url_negative("https://x.com/sampling-adc")


def test_click_memo_keys():
    crawler = PartCrawler(FakeFetcher(), lambda *a, **k: None)
    node = Node("https://x/product/A", 2, True)
    row = {"text": "", "hint": "dataSheet", "row": "NSM4101-Q0TOMR Active MSL1"}
    assert crawler._click_key(node, row) == ("datasheet", "NSM4101-Q0TOMR Active MSL1")       # per row
    assert crawler._click_key(node, {"text": "Datasheet", "hint": "", "row": ""}) == ("datasheet", node.url)
    assert crawler._click_key(node, {"text": "NSM4102-Q0TOMR", "hint": "", "row": "x"}) == ("nsm4102-q0tomr",)


def test_download_retries_broken_transfers(tmp_path=None):
    import tempfile
    from dataPipeline.download.downloader import DownloadError, download
    good = b"%PDF-1.7 datasheet body"

    class Resp:
        def __init__(self, body, length=None):
            self.body, self.status_code = body, 200
            self.headers = {"content-type": "application/pdf", "content-length": str(length or len(body))}

        def iter_bytes(self, n):
            yield self.body

        def close(self):
            pass

    class Flaky:
        def __init__(self, answers):
            self.answers, self.reconnects = list(answers), 0

        def stream(self, url):
            return self.answers.pop(0)

        def reconnect(self):
            self.reconnects += 1

    base = Path(tmp_path or tempfile.mkdtemp())
    pdf = DatasheetLink("https://x/datasheet/A1/A1_Rev2.pdf", "Datasheet", "", "p", "")
    # garbage from the middle of a file, then a cut-off transfer, then the real file
    fetcher = Flaky([Resp(b"e\x00\xf0\xd5garbage"), Resp(good[:5], length=len(good)), Resp(good)])
    info = download(fetcher, pdf, base / "maker", {}, base)
    assert fetcher.reconnects == 2 and info["file_path"].startswith("maker/a1-rev2_")
    assert (base / info["file_path"]).read_bytes() == good
    # an HTML answer is not retried
    html = Resp(b"<html>login</html>")
    html.headers["content-type"] = "text/html"
    fetcher = Flaky([html])
    try:
        download(fetcher, DatasheetLink("https://x/b.pdf", "", "", "p", ""), base / "maker", {}, base)
        raise AssertionError("expected DownloadError")
    except DownloadError as exc:
        assert exc.stage == "verify" and fetcher.reconnects == 0


def test_item_token_and_covered_parts():
    from dataPipeline.download.crawler import item_token
    assert item_token("https://x/en/product/NSR33001-QHMSR") == "NSR33001-QHMSR"
    assert item_token("https://x/en/magnetic-sensor") == "" and item_token("https://x/proshow.php?id=5") == ""
    rows = {"NSR33001-QHMSR Active MSL3 HMSOP8 Automotive"}
    assert PartCrawler._part_covered("https://x/en/product/NSR33001-QHMSR", rows)
    assert not PartCrawler._part_covered("https://x/en/product/NSR33002-QHMSR", rows)


def test_same_pdf_under_another_url_is_not_downloaded_again():
    import json
    import tempfile
    from types import SimpleNamespace
    from dataPipeline.download import run as run_mod

    class Fetcher:
        downloads = 0

        def head_info(self, url):
            return {"size": 1234, "content_type": "application/pdf", "disposition": ""}

        def stream(self, url):
            raise AssertionError("must not download")

    folder = Path(tempfile.mkdtemp())
    (folder / "m").mkdir()
    (folder / "m" / "x-datasheet_aaaa.pdf").write_bytes(b"%PDF" + b"0" * 1230)
    (folder / "m" / "manifest.jsonl").write_text(json.dumps({
        "source_url": "https://x/datasheet/PART-A/X_Datasheet.pdf", "file_path": "m/x-datasheet_aaaa.pdf",
        "file_hash": "a" * 64, "file_size": 1234}) + "\n")
    old = run_mod.DATASHEETS_DIR
    run_mod.DATASHEETS_DIR = folder
    try:
        saver = run_mod.Saver(Fetcher(), None, "m", SimpleNamespace(all_pdfs=True, dry_run=False, force=False,
                                                                     verbose=False), lambda *a, **k: None)
        outcome = saver(DatasheetLink("https://x/datasheet/PART-B/X_Datasheet.pdf", "dataSheet", "", "p", ""))
    finally:
        run_mod.DATASHEETS_DIR = old
    assert outcome == "duplicate" and saver.manifest["https://x/datasheet/PART-B/X_Datasheet.pdf"]["file_hash"] == "a" * 64


def test_same_host():
    assert same_host("www.novosns.com", "novosns.com")
    assert not same_host("en.goodark.com", "www.goodark.com")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
