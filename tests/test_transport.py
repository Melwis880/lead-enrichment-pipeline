"""RequestsTransport against a local HTTP server on 127.0.0.1 (no outside network)."""

import gzip
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lead_enrich.config import FetchSettings, Settings
from lead_enrich.extractors import MockExtractor
from lead_enrich.fetcher import PoliteFetcher, RequestsTransport, TransportError
from lead_enrich.pipeline import Company, enrich_company
from lead_enrich.scoring import ICP

HTML = "<html><body><p>Café crème</p></body></html>".encode()
META_UTF8 = '<html><head><meta charset="utf-8"></head><body><p>Café crème</p></body></html>'
META_CP1252 = (
    '<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
    "</head><body><p>Price €5 – it’s open</p></body></html>"
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # body ends when the connection closes

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, headers: dict[str, str], body: bytes = b"") -> None:
        self.send_response(200)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        try:
            if self.path == "/robots.txt":
                self.send_response(404)
                self.end_headers()
            elif self.path == "/lowercase":
                self._send({"content-type": "text/html; charset=utf-8"}, HTML)
            elif self.path == "/lowercase-redirect":
                self.send_response(301)
                self.send_header("location", "/lowercase")
                self.end_headers()
            elif self.path == "/no-charset-meta-utf8":
                self._send({"Content-Type": "text/html"}, META_UTF8.encode("utf-8"))
            elif self.path == "/no-charset-no-meta":
                self._send({"Content-Type": "text/html"}, HTML)
            elif self.path == "/no-charset-meta-cp1252":
                self._send({"Content-Type": "text/html"}, META_CP1252.encode("cp1252"))
            elif self.path == "/utf8mb4":
                self._send({"Content-Type": "text/html; charset=utf8mb4"}, HTML)
            elif self.path == "/no-ctype":
                self._send({}, HTML)
            elif self.path == "/big-no-ctype":
                self._send({})
                end = time.monotonic() + 15  # endless fast stream, no Content-Type, no length
                while time.monotonic() < end:
                    self.wfile.write(b"a" * 65536)
            elif self.path == "/gzip":
                self._send(
                    {"Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"},
                    gzip.compress(HTML),
                )
            elif self.path == "/gzip-bomb":
                bomb = gzip.compress(b"0" * 50_000_000)  # ~50 KB on the wire, 50 MB unpacked
                self._send({"Content-Type": "text/html", "Content-Encoding": "gzip"}, bomb)
            elif self.path == "/drip":
                self._send({"Content-Type": "text/html"})
                for _ in range(100):  # 1 byte every 0.2 s for 20 s
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def blackhole_port():
    """A port whose listen queue is full, so connect() never completes: a real connect timeout
    (what a firewalled or dead host looks like), without leaving 127.0.0.1."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(0)
    port = srv.getsockname()[1]
    fillers = []
    for _ in range(3):
        filler = socket.socket()
        filler.setblocking(False)
        try:
            filler.connect(("127.0.0.1", port))
        except BlockingIOError:
            pass
        fillers.append(filler)
    time.sleep(0.2)
    yield port
    for sock in (srv, *fillers):
        sock.close()


@pytest.fixture
def refused_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here any more
    return port


def quick_fetcher(**transport_kwargs):
    """Real transport, short read timeout, no politeness sleeping."""
    return PoliteFetcher(
        FetchSettings(timeout_seconds=0.5, max_retries=2),
        RequestsTransport(**transport_kwargs),
        sleep=lambda _s: None,
        host_check=None,
    )


def get(server, path, **kwargs):
    transport = RequestsTransport(**kwargs)
    return transport.get(server + path, {"User-Agent": "test"}, timeout=2.0)


def test_unknown_charset_falls_back_to_utf8(server):
    resp = get(server, "/utf8mb4")
    assert "Café crème" in resp.text


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/no-charset-meta-utf8", "Café crème"),  # charset only in <meta charset>
        ("/no-charset-no-meta", "Café crème"),  # declared nowhere: UTF-8, not ISO-8859-1
        ("/no-charset-meta-cp1252", "Price €5 – it’s open"),  # <meta http-equiv> form
    ],
)
def test_charset_missing_from_header_is_taken_from_the_page(server, path, expected):
    assert expected in get(server, path).text


def test_missing_content_type_does_not_crash(server):
    resp = get(server, "/no-ctype")
    assert resp.status == 200 and "Café crème" in resp.text


def test_oversized_body_without_content_type_is_capped(server):
    started = time.monotonic()
    resp = get(server, "/big-no-ctype", max_bytes=100_000)
    assert len(resp.text) == 100_000
    assert time.monotonic() - started < 3  # stopped reading at the cap, did not drain the stream


def test_slow_drip_hits_overall_deadline(server):
    started = time.monotonic()
    with pytest.raises(TransportError, match="deadline") as info:
        get(server, "/drip", deadline_seconds=1.5)
    # deadline + at most one per-read timeout (2 s), far below the 20 s the server would drip
    assert time.monotonic() - started < 4.5
    assert info.value.kind == "timeout" and info.value.retryable is False


def test_connect_timeout_is_a_non_retryable_timeout(blackhole_port):
    started = time.monotonic()
    with pytest.raises(TransportError, match="connect timeout") as info:
        RequestsTransport(connect_timeout_seconds=0.5).get(
            f"http://127.0.0.1:{blackhole_port}/", {}, timeout=2.0
        )
    assert time.monotonic() - started < 1.5  # the connect timeout, not the 2 s read timeout
    assert info.value.kind == "timeout" and info.value.retryable is False


def test_refused_connection_is_recognised(refused_port):
    with pytest.raises(TransportError) as info:
        RequestsTransport().get(f"http://127.0.0.1:{refused_port}/", {}, timeout=2.0)
    assert info.value.kind == "refused" and info.value.retryable is True


def test_dead_host_is_tried_once_not_three_times(blackhole_port):
    fetcher = quick_fetcher()
    result = fetcher.fetch(f"http://127.0.0.1:{blackhole_port}/")
    assert result.error and result.error.startswith("network error")
    assert fetcher.stats["requests"] == 1 and fetcher.stats["retries"] == 0


def test_body_past_the_deadline_is_not_fetched_again(server):
    fetcher = quick_fetcher(deadline_seconds=1.0)
    result = fetcher.fetch(server + "/drip")
    assert result.error and "deadline" in result.error
    assert fetcher.stats["requests"] == 2  # robots.txt + one try of the page
    assert fetcher.stats["retries"] == 0


def test_dead_host_is_not_retried_over_plain_http(blackhole_port, tmp_path):
    # https:// and http:// both point at the dead port; only the first is tried.
    fetcher = quick_fetcher()
    started = time.monotonic()
    record = enrich_company(
        Company("Dead", f"127.0.0.1:{blackhole_port}"),
        fetcher,
        MockExtractor(tmp_path),
        Settings(),
        ICP(),
    )
    assert record.status == "fetch_failed"
    assert fetcher.stats["requests"] == 1
    assert time.monotonic() - started < 1.5


def test_lowercase_header_names_are_understood(server):
    # Some proxies send "content-type:" / "location:"; header names are case-insensitive.
    fetcher = PoliteFetcher(
        FetchSettings(), RequestsTransport(), sleep=lambda _s: None, host_check=None
    )
    result = fetcher.fetch(server + "/lowercase-redirect")
    assert result.ok, result.error
    assert result.final_url == server + "/lowercase"
    assert "Café crème" in result.text


def test_gzip_body_is_decoded(server):
    assert "Café crème" in get(server, "/gzip").text


def test_gzip_bomb_is_capped_after_decompression(server):
    assert len(get(server, "/gzip-bomb", max_bytes=100_000).text) == 100_000
