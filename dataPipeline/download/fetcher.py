"""HTTP and browser access for the crawler.

Responsibilities: polite rate limiting per host, `robots.txt`, retries with
backoff, and Playwright rendering for JavaScript-heavy sites.
"""
import os
import socket
import ssl
import time
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

from .settings import (
    CHROMIUM_PATH, COOKIE_ACCEPT_RE, LOAD_MORE_RE, MAX_HTML_BYTES, MAX_RETRIES,
    REQUEST_DELAY, REQUEST_TIMEOUT, USER_AGENT,
)

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Page:
    def __init__(self, url, status, content_type, html=None):
        self.url = url
        self.status = status
        self.content_type = content_type
        self.html = html

    @property
    def is_pdf(self) -> bool:
        return "pdf" in (self.content_type or "")


class Fetcher:
    def __init__(self, delay: float = REQUEST_DELAY):
        self.delay = delay
        self._extra_ca_pem: list[str] = []  # intermediates fetched for servers with incomplete chains
        self._chain_repaired: set[str] = set()
        self.client = self._new_client()
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._pw = None
        self._browser = None
        self._context = None

    def _new_client(self) -> httpx.Client:
        ctx = ssl.create_default_context()  # honours SSL_CERT_FILE (e.g. a proxy CA bundle)
        for pem in self._extra_ca_pem:
            ctx.load_verify_locations(cadata=pem)
        return httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-GB,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT,
            verify=ctx,
        )

    # ------------------------------------------------------------ incomplete TLS chains
    def _server_certificate(self, host: str, port: int = 443):
        """Fetch a server's leaf certificate without verifying it (only to read where its issuer is)."""
        from cryptography import x509

        proxy = urlsplit(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "")
        if proxy.hostname:
            sock = socket.create_connection((proxy.hostname, proxy.port or 80), timeout=REQUEST_TIMEOUT)
            sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            reply = b""
            while b"\r\n\r\n" not in reply:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
            if b" 200" not in reply.split(b"\r\n", 1)[0]:
                sock.close()
                raise FetchError(f"proxy refused CONNECT to {host}")
        else:
            sock = socket.create_connection((host, port), timeout=REQUEST_TIMEOUT)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))

    def _repair_chain(self, host: str) -> bool:
        """Some sites send only their own certificate and leave out the intermediate CA, which
        browsers fetch themselves. Do the same: follow the certificate's "CA Issuers" link,
        trust the intermediate it returns, and keep verification switched on."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

        if host in self._chain_repaired:
            return False
        self._chain_repaired.add(host)
        try:
            cert = self._server_certificate(host)
            for _ in range(4):  # follow the chain upwards a few levels
                try:
                    aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
                except x509.ExtensionNotFound:
                    break  # reached a root certificate
                urls = [d.access_location.value for d in aia
                        if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
                if not urls:
                    break
                resp = self.client.get(urls[0].replace("http://", "https://", 1))
                data = resp.content
                issuer = (x509.load_pem_x509_certificate(data) if b"BEGIN CERTIFICATE" in data[:64]
                          else x509.load_der_x509_certificate(data))
                self._extra_ca_pem.append(issuer.public_bytes(serialization.Encoding.PEM).decode())
                cert = issuer
        except Exception:  # noqa: BLE001 - not repairable: the normal TLS error is reported
            return False
        if not self._extra_ca_pem:
            return False
        old, self.client = self.client, self._new_client()
        old.close()
        return True

    # ------------------------------------------------------------ politeness
    def _wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = None
            try:
                self._wait(origin)
                resp = self.client.get(origin + "/robots.txt")
                if resp.status_code == 200 and "html" not in resp.headers.get("content-type", ""):
                    parser = robotparser.RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None  # no robots.txt reachable: everything allowed
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    # ------------------------------------------------------------ HTTP
    def _request(self, method: str, url: str, stream: bool = False):
        """Send a request with retries. The caller must close a streamed response."""
        last_exc = None
        for attempt in range(MAX_RETRIES):
            self._wait(url)
            try:
                req = self.client.build_request(method, url)
                resp = self.client.send(req, stream=stream)
            except httpx.TransportError as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc) and self._repair_chain(urlsplit(url).hostname or ""):
                    continue  # retry at once with the missing intermediate certificate trusted
                last_exc = FetchError(f"{exc.__class__.__name__}: {exc}")
            else:
                if resp.status_code not in RETRY_STATUS:
                    return resp
                last_exc = FetchError(f"HTTP {resp.status_code}", resp.status_code)
                if stream:
                    resp.close()
            time.sleep(2 ** (attempt + 1))
        raise last_exc

    def get_page(self, url: str) -> Page:
        """GET a URL and read the body only if it is HTML."""
        resp = self._request("GET", url, stream=True)
        try:
            ctype = resp.headers.get("content-type", "").lower()
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code}", resp.status_code)
            html = None
            if "html" in ctype or not ctype:
                size = int(resp.headers.get("content-length") or 0)
                if size > MAX_HTML_BYTES:
                    raise FetchError(f"HTML page too large ({size} bytes)")
                body = resp.read()[:MAX_HTML_BYTES]
                html = body.decode(resp.encoding or "utf-8", errors="replace")
            return Page(str(resp.url), resp.status_code, ctype, html)
        finally:
            resp.close()

    def is_pdf(self, url: str) -> bool:
        """Check content-type with HEAD. Servers that reject HEAD get a streamed GET."""
        resp = self._request("HEAD", url)
        if resp.status_code in (403, 404, 405, 501) or not resp.headers.get("content-type"):
            resp = self._request("GET", url, stream=True)
            resp.close()
        ctype = resp.headers.get("content-type", "").lower()
        disposition = resp.headers.get("content-disposition", "").lower()
        return resp.status_code < 400 and ("pdf" in ctype or ".pdf" in disposition)

    def stream(self, url: str):
        resp = self._request("GET", url, stream=True)
        if resp.status_code >= 400:
            resp.close()
            raise FetchError(f"HTTP {resp.status_code}", resp.status_code)
        return resp

    # ------------------------------------------------------------ browser
    def _browser_context(self):
        if self._context is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            try:
                self._browser = self._pw.chromium.launch()
            except Exception:  # noqa: BLE001 - bundled browser missing, use the system one
                self._browser = self._pw.chromium.launch(
                    executable_path=CHROMIUM_PATH, args=["--no-sandbox"], headless=True
                )
            self._context = self._browser.new_context(
                user_agent=USER_AGENT, locale="en-GB", accept_downloads=False,
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
            )
        return self._context

    def render(self, url: str, scrolls: int = 6, load_more_clicks: int = 8) -> Page:
        """Render a page in Chromium. Scrolls and presses "load more" buttons first."""
        self._wait(url)
        page = self._browser_context().new_page()
        try:
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
            except Exception as exc:  # noqa: BLE001
                raise FetchError(f"render failed: {exc}") from exc
            status = resp.status if resp else None
            if status and status >= 400:
                raise FetchError(f"HTTP {status}", status)
            ctype = (resp.headers.get("content-type", "") if resp else "").lower()
            if "pdf" in ctype:
                return Page(page.url, status, ctype)
            self._settle(page)
            self._accept_cookies(page)
            for _ in range(scrolls):
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(300)
            start_url = page.url
            clickable = "button, [role=button], a[href^='#'], a[href^='javascript'], a:not([href])"
            for _ in range(load_more_clicks):
                button = page.locator(clickable).filter(has_text=LOAD_MORE_RE).first
                try:
                    if button.count() == 0 or not button.is_visible():
                        break
                    button.click(timeout=3000)
                except Exception:  # noqa: BLE001
                    break
                self._settle(page)
                if page.url != start_url:
                    break
            return Page(page.url, status, ctype or "text/html", page.content())
        finally:
            page.close()

    @staticmethod
    def _settle(page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(1000)

    @staticmethod
    def _accept_cookies(page) -> None:
        try:
            button = page.locator("button, a, [role=button]").filter(has_text=COOKIE_ACCEPT_RE).first
            if button.count() and button.is_visible():
                button.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self.client.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
