"""RequestsTransport against a local HTTP server on 127.0.0.1 (no outside network)."""

import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lead_enrich.fetcher import RequestsTransport, TransportError

HTML = "<html><body><p>Café crème</p></body></html>".encode()


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
            if self.path == "/utf8mb4":
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


def get(server, path, **kwargs):
    transport = RequestsTransport(**kwargs)
    return transport.get(server + path, {"User-Agent": "test"}, timeout=2.0)


def test_unknown_charset_falls_back_to_utf8(server):
    resp = get(server, "/utf8mb4")
    assert "Café crème" in resp.text


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
    with pytest.raises(TransportError, match="deadline"):
        get(server, "/drip", deadline_seconds=1.5)
    # deadline + at most one per-read timeout (2 s), far below the 20 s the server would drip
    assert time.monotonic() - started < 4.5


def test_gzip_body_is_decoded(server):
    assert "Café crème" in get(server, "/gzip").text


def test_gzip_bomb_is_capped_after_decompression(server):
    assert len(get(server, "/gzip-bomb", max_bytes=100_000).text) == 100_000
