"""Polite HTTP fetching: robots.txt, rate limit, timeouts, retries, disk cache."""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

from .cache import JsonDiskCache
from .config import FetchSettings
from .robots import RobotsPolicy

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_REDIRECTS = 5
MAX_RETRY_AFTER_SECONDS = 60.0
MAX_CRAWL_DELAY_SECONDS = 60.0
RETRY_AFTER_RE = re.compile(r"[0-9]{1,5}")


class TransportError(Exception):
    """Network-level failure (DNS, connection, timeout)."""


@dataclass
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    text: str


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str], timeout: float) -> HttpResponse: ...


class RequestsTransport:
    """Real network transport. Does not follow redirects; the fetcher does, so every hop is
    checked against robots.txt and rate-limited.

    The body is read with ``read1`` (at most one socket read per call), so both the byte cap and
    the overall deadline are checked between reads. A server that drips one byte at a time can
    hold us for at most ``deadline_seconds`` plus one per-read ``timeout``.
    """

    def __init__(self, max_bytes: int = 2_000_000, deadline_seconds: float = 20.0) -> None:
        self.session = requests.Session()
        self.max_bytes = max_bytes
        self.deadline_seconds = deadline_seconds

    def get(self, url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
        deadline = time.monotonic() + self.deadline_seconds
        try:
            with self.session.get(
                url, headers=headers, timeout=timeout, allow_redirects=False, stream=True
            ) as resp:
                body = bytearray()
                while len(body) < self.max_bytes:
                    if time.monotonic() > deadline:
                        raise TransportError(f"deadline of {self.deadline_seconds:.0f}s exceeded")
                    chunk = resp.raw.read1(
                        min(65536, self.max_bytes - len(body)), decode_content=True
                    )
                    if not chunk:
                        break
                    body += chunk
                return HttpResponse(
                    url,
                    resp.status_code,
                    dict(resp.headers),
                    decode_body(bytes(body[: self.max_bytes]), resp.encoding),
                )
        except requests.Timeout as exc:
            raise TransportError(f"timeout after {timeout}s") from exc
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
            raise TransportError(type(exc).__name__) from exc


def decode_body(body: bytes, encoding: str | None) -> str:
    """Decode with the declared charset; unknown or broken charsets fall back to UTF-8."""
    try:
        return body.decode(encoding or "utf-8", errors="replace")
    except (LookupError, UnicodeError):
        return body.decode("utf-8", errors="replace")


class FixtureTransport:
    """Serves saved HTML from disk: ``<root>/<host>/<path>.html``.

    ``/`` maps to ``index.html``, ``/about/`` to ``about.html``. A sibling file
    ``<path>.status`` can simulate failures: a number (e.g. ``503``) or ``timeout``.
    Missing files return 404, like a real server.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.calls: list[str] = []

    def _base(self, url: str) -> Path:
        parts = urlsplit(url)
        path = parts.path.strip("/") or "index"
        if path == "robots.txt":
            return self.root / parts.netloc / "robots"
        return self.root / parts.netloc / path.removesuffix(".html")

    def get(self, url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
        self.calls.append(url)
        base = self._base(url)
        status_file = base.with_name(base.name + ".status")
        if status_file.exists():
            spec = status_file.read_text(encoding="utf-8").strip()
            if spec == "timeout":
                raise TransportError(f"timeout after {timeout}s")
            return HttpResponse(url, int(spec), {"Content-Type": "text/html"}, "")
        is_robots = base.name == "robots"
        file = base.with_name(base.name + (".txt" if is_robots else ".html"))
        if not file.exists():
            return HttpResponse(url, 404, {"Content-Type": "text/html"}, "Not found")
        ctype = "text/plain" if is_robots else "text/html; charset=utf-8"
        return HttpResponse(url, 200, {"Content-Type": ctype}, file.read_text(encoding="utf-8"))


class UnsafeHostError(Exception):
    """The URL points at a non-public address or scheme (SSRF guard)."""


NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable unicast only. IPv6 forms that embed an IPv4 address (mapped, 6to4,
    NAT64) are judged by the embedded address, so ::ffff:127.0.0.1 is not "public"."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in NAT64_PREFIX:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not is_public_ip(embedded):
            return False
    return ip.is_global and not ip.is_multicast


def check_public_host(url: str, resolver: Callable[..., list] = socket.getaddrinfo) -> None:
    """Raise UnsafeHostError unless every address the host resolves to is public.

    Called before every request (pages, robots.txt, each redirect hop, each retry). Known gap:
    requests resolves the name again when it connects, so a DNS-rebinding server could answer
    differently the second time. See README "Limitations".
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeHostError(f"unsupported scheme {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise UnsafeHostError("URL has no host")
    try:
        addresses = [ipaddress.ip_address(host.split("%", 1)[0])]
    except ValueError:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            infos = resolver(host, port, proto=socket.IPPROTO_TCP)
        except (OSError, UnicodeError) as exc:  # socket.gaierror is an OSError
            raise TransportError(f"DNS lookup failed for {host}") from exc
        addresses = [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]
    for ip in addresses:
        if not is_public_ip(ip):
            raise UnsafeHostError(f"{host} resolves to non-public address {ip}")


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int | None = None
    text: str = ""
    from_cache: bool = False
    error: str | None = None
    skipped: str | None = None  # e.g. "robots_disallowed"

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped is None and self.status == 200


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class PoliteFetcher:
    settings: FetchSettings
    transport: Transport
    cache: JsonDiskCache | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    # SSRF guard, run before every request. Only offline transports (fixtures) may pass None.
    host_check: Callable[[str], None] | None = check_public_host
    _robots: dict[str, RobotsPolicy] = field(default_factory=dict)
    _last_request: dict[str, float] = field(default_factory=dict)
    stats: dict[str, float] = field(
        default_factory=lambda: {
            "requests": 0,
            "cache_hits": 0,
            "retries": 0,
            "robots_blocked": 0,
            "waited_s": 0.0,  # politeness delay (real, or simulated when sleep is a no-op)
        }
    )

    # -- public API ---------------------------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        """Fetch one HTML page, following up to MAX_REDIRECTS redirects."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                self._check_host(current)
            except UnsafeHostError as exc:
                return FetchResult(url, current, error=f"blocked: {exc}")
            except TransportError as exc:
                return FetchResult(url, current, error=f"network error: {exc}")
            policy = self.robots_for(current)
            if not policy.allowed(current):
                self.stats["robots_blocked"] += 1
                return FetchResult(url, current, skipped=f"blocked by {policy.reason}")
            if (delay := policy.crawl_delay()) and delay > MAX_CRAWL_DELAY_SECONDS:
                return FetchResult(url, current, skipped=f"crawl_delay_too_long ({delay:.0f}s)")

            if self.cache is not None and (hit := self.cache.get(f"page:{current}")):
                self.stats["cache_hits"] += 1
                return FetchResult(url, current, hit["status"], hit["text"], from_cache=True)

            try:
                resp = self._get_with_retries(current, policy)
            except TransportError as exc:
                return FetchResult(url, current, error=f"network error: {exc}")

            if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                current = urljoin(current, resp.headers["Location"])
                continue
            if resp.status != 200:
                return FetchResult(url, current, resp.status, error=f"HTTP {resp.status}")
            ctype = resp.headers.get("Content-Type", resp.headers.get("content-type", ""))
            if "html" not in ctype.lower():
                return FetchResult(url, current, resp.status, error=f"not HTML ({ctype or '?'})")
            text = resp.text[: self.settings.max_page_bytes]
            if self.cache is not None:
                self.cache.set(f"page:{current}", {"status": 200, "text": text})
            return FetchResult(url, current, 200, text)
        return FetchResult(url, current, error="too many redirects")

    def robots_for(self, url: str) -> RobotsPolicy:
        origin = origin_of(url)
        if origin in self._robots:
            return self._robots[origin]
        robots_url = f"{origin}/robots.txt"
        ua = self.settings.user_agent
        cached = self.cache.get(f"robots:{origin}") if self.cache is not None else None
        if cached is not None:
            policy = RobotsPolicy.from_status(cached["status"], cached["text"], ua)
        else:
            try:
                # Robots fetch is itself rate-limited, but has no policy to consult yet.
                resp = self._get_with_retries(robots_url, None)
                status, text = resp.status, resp.text
                if 300 <= status < 400:
                    # Keep it simple: one redirect hop for robots.txt (e.g. http -> https).
                    loc = urljoin(robots_url, resp.headers.get("Location", ""))
                    resp = self._get_with_retries(loc, None)
                    status, text = resp.status, resp.text
            except TransportError:
                status, text = None, ""
            except UnsafeHostError as exc:
                # e.g. robots.txt redirecting to an internal address: stay away from the site
                policy = RobotsPolicy(ua, "disallow_all", f"robots.txt redirect blocked ({exc})")
                self._robots[origin] = policy
                return policy
            policy = RobotsPolicy.from_status(status, text, ua)
            if self.cache is not None and status is not None and status < 500:
                self.cache.set(f"robots:{origin}", {"status": status, "text": text})
        self._robots[origin] = policy
        return policy

    # -- internals ----------------------------------------------------------------------

    def _check_host(self, url: str) -> None:
        if self.host_check is not None:
            self.host_check(url)

    def _wait_turn(self, origin: str, policy: RobotsPolicy | None) -> None:
        delay = self.settings.min_delay_seconds
        if policy is not None and (crawl_delay := policy.crawl_delay()):
            delay = max(delay, crawl_delay)
        last = self._last_request.get(origin)
        if last is not None:
            remaining = delay - (self.clock() - last)
            if remaining > 0:
                self.stats["waited_s"] += remaining
                self.sleep(remaining)
        self._last_request[origin] = self.clock()

    def _get_with_retries(self, url: str, policy: RobotsPolicy | None) -> HttpResponse:
        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8",
            "Accept-Language": "en",
        }
        origin = origin_of(url)
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            self._wait_turn(origin, policy)
            self.stats["requests"] += 1
            last_try = attempt == attempts - 1
            try:
                self._check_host(url)  # every attempt: DNS answers can change between tries
                resp = self.transport.get(url, headers, self.settings.timeout_seconds)
            except TransportError:
                if last_try:
                    raise
                self.stats["retries"] += 1
                self.sleep(self._backoff(attempt, None))
                continue
            if resp.status in RETRYABLE_STATUS and not last_try:
                self.stats["retries"] += 1
                self.sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        # Only plain ASCII delay-seconds; HTTP-dates, Unicode digits and junk use normal backoff.
        if retry_after and RETRY_AFTER_RE.fullmatch(retry_after.strip()):
            return min(float(retry_after.strip()), MAX_RETRY_AFTER_SECONDS)
        return self.settings.backoff_base_seconds * (2**attempt)
