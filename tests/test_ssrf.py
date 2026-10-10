"""SSRF guard: never connect to loopback, private, link-local or other non-public addresses."""

import socket

import pytest

from lead_enrich.fetcher import (
    HttpResponse,
    PoliteFetcher,
    UnsafeHostError,
    check_public_host,
)
from lead_enrich.pipeline import read_companies


def fake_resolver(table):
    def resolve(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("not found")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, 1, 6, "", (ip, port))
            for ip in table[host]
        ]

    return resolve


PUBLIC = {"shop.example": ["93.184.216.34"], "v6.example": ["2606:4700::1111"]}


@pytest.mark.parametrize("url", ["https://shop.example/", "https://v6.example/about"])
def test_public_hosts_pass(url):
    check_public_host(url, resolver=fake_resolver(PUBLIC))


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1%eth0",
        "fc00::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
    ],
)
def test_non_public_resolution_is_rejected(ip):
    resolver = fake_resolver({"evil.example": [ip]})
    with pytest.raises(UnsafeHostError):
        check_public_host("https://evil.example/", resolver=resolver)


def test_one_private_address_among_public_ones_is_rejected():
    resolver = fake_resolver({"mixed.example": ["93.184.216.34", "10.0.0.1"]})
    with pytest.raises(UnsafeHostError):
        check_public_host("https://mixed.example/", resolver=resolver)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://0x7f.1/",
        "http://2130706433/",
        "http://localhost/",
    ],
)
def test_literal_and_shorthand_loopback_rejected_with_real_resolver(url):
    # These resolve locally (no network): numeric shorthands and localhost.
    with pytest.raises(UnsafeHostError):
        check_public_host(url)


class Recorder:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append(url)
        status, body, headers_out = self.responses.get(url, (404, "", {}))
        return HttpResponse(url, status, {"Content-Type": "text/html", **headers_out}, body)


def guarded_fetcher(transport, table):
    from lead_enrich.config import FetchSettings

    resolver = fake_resolver(table)
    return PoliteFetcher(
        FetchSettings(),
        transport,
        sleep=lambda _s: None,
        host_check=lambda url: check_public_host(url, resolver=resolver),
    )


def test_redirect_to_metadata_service_is_blocked_before_any_request():
    transport = Recorder(
        {"https://shop.example/": (302, "", {"Location": "http://169.254.169.254/latest/"})}
    )
    table = {**PUBLIC, "169.254.169.254": ["169.254.169.254"]}
    result = guarded_fetcher(transport, table).fetch("https://shop.example/")
    assert result.error and "non-public" in result.error
    assert not any("169.254" in c for c in transport.calls)


def test_redirect_to_internal_hostname_is_blocked():
    transport = Recorder(
        {"https://shop.example/": (301, "", {"Location": "http://intranet.corp/admin"})}
    )
    table = {**PUBLIC, "intranet.corp": ["10.1.2.3"]}
    result = guarded_fetcher(transport, table).fetch("https://shop.example/")
    assert "non-public" in result.error
    assert transport.calls == ["https://shop.example/robots.txt", "https://shop.example/"]


def test_robots_redirect_to_internal_host_is_never_requested():
    transport = Recorder(
        {"https://shop.example/robots.txt": (302, "", {"Location": "http://10.0.0.1/robots.txt"})}
    )
    table = {**PUBLIC, "10.0.0.1": ["10.0.0.1"]}
    result = guarded_fetcher(transport, table).fetch("https://shop.example/")
    assert not result.ok
    assert not any("10.0.0.1" in c for c in transport.calls)


def test_host_resolving_to_private_address_is_never_requested():
    transport = Recorder({})
    result = guarded_fetcher(transport, {"evil.example": ["192.168.0.10"]}).fetch(
        "https://evil.example/"
    )
    assert "non-public" in result.error and transport.calls == []


def test_input_rejects_ip_literals(tmp_path):
    f = tmp_path / "in.csv"
    f.write_text(
        "name,website\nA,127.0.0.1\nB,http://[::1]/\nC,192.168.1.1:8080\n"
        "D,http://10.0.0.1/admin\nE,shop.example\n"
    )
    companies, warnings = read_companies(f)
    assert [c.domain for c in companies] == ["shop.example"]
    assert sum("IP address" in w for w in warnings) == 4


def test_dns_answer_changing_between_checks_is_still_a_clean_block():
    from lead_enrich.config import FetchSettings

    answers = iter(["93.184.216.34", "93.184.216.34", "10.0.0.1"])  # rebinding on 3rd lookup

    def flipping(url):
        ip = next(answers, "10.0.0.1")
        check_public_host(url, resolver=fake_resolver({"shop.example": [ip]}))

    transport = Recorder({"https://shop.example/robots.txt": (404, "", {})})
    fetcher = PoliteFetcher(FetchSettings(), transport, sleep=lambda _s: None, host_check=flipping)
    result = fetcher.fetch("https://shop.example/")  # must not raise
    assert result.error and "non-public" in result.error
    assert "https://shop.example/" not in transport.calls


# urlsplit and urllib3 read different hosts from these URLs; the guard must see urllib3's.
BACKSLASH_REDIRECTS = [
    "http://127.0.0.1:8765\\@shop.example/",
    "http://169.254.169.254\\@shop.example/../latest/meta-data/iam/",
]


@pytest.mark.parametrize("location", BACKSLASH_REDIRECTS)
def test_backslash_host_trick_in_a_redirect_is_blocked(location):
    transport = Recorder({"https://shop.example/": (302, "", {"Location": location})})
    table = {**PUBLIC, "127.0.0.1": ["127.0.0.1"], "169.254.169.254": ["169.254.169.254"]}
    result = guarded_fetcher(transport, table).fetch("https://shop.example/")
    assert result.error and "non-public" in result.error
    assert transport.calls == ["https://shop.example/robots.txt", "https://shop.example/"]


@pytest.mark.parametrize("url", BACKSLASH_REDIRECTS)
def test_backslash_host_trick_as_start_url_is_blocked(url):
    transport = Recorder({})
    table = {**PUBLIC, "127.0.0.1": ["127.0.0.1"], "169.254.169.254": ["169.254.169.254"]}
    result = guarded_fetcher(transport, table).fetch(url)
    assert result.error and "non-public" in result.error and transport.calls == []


def test_idna_hostname_is_checked_in_the_form_that_is_connected_to():
    # socket.getaddrinfo encodes with IDNA 2003 (straße -> strasse); requests connects to the
    # UTS 46 form (xn--strae-oqa). An attacker controlling DNS can point them apart.
    def idna2003_resolver(host, port, *args, **kwargs):
        table = {
            "strasse.attacker.example": ["93.184.216.34"],
            "xn--strae-oqa.attacker.example": ["127.0.0.1"],
        }
        name = host.encode("idna").decode("ascii")
        if name not in table:
            raise socket.gaierror("not found")
        return [(socket.AF_INET, 1, 6, "", (ip, port)) for ip in table[name]]

    from lead_enrich.config import FetchSettings

    transport = Recorder({})
    fetcher = PoliteFetcher(
        FetchSettings(),
        transport,
        sleep=lambda _s: None,
        host_check=lambda url: check_public_host(url, resolver=idna2003_resolver),
    )
    result = fetcher.fetch("https://straße.attacker.example/")
    assert result.error and "non-public" in result.error and transport.calls == []


def test_url_with_control_characters_is_rejected():
    transport = Recorder(
        {"https://shop.example/": (302, "", {"Location": "http://\x1bc\x1b7x.example/"})}
    )
    result = guarded_fetcher(transport, PUBLIC).fetch("https://shop.example/")
    assert result.error == "blocked: invalid URL"
    assert "\x1b" not in "".join(transport.calls)


def test_backslash_host_link_is_not_treated_as_a_same_site_page():
    from lead_enrich.parser import discover_pages, parse_html

    html = (
        '<a href="http://169.254.169.254\\@shop.example/../latest/meta-data/iam/">About us</a>'
        '<a href="/contact">Contact</a>'
    )
    page = parse_html(html, "https://shop.example/")
    hosts = {link.url.split("/")[2] for link in page.links}
    assert "169.254.169.254" in hosts and "shop.example" in hosts
    picked = discover_pages(page, "shop.example", 3)
    assert picked == ["https://shop.example/contact"]


def test_robots_txt_is_checked_for_the_path_that_is_really_requested():
    # /x/%2e%2e/private/ prepares to /x/../private/ and, prepared again on send, to /private/.
    robots = "User-agent: *\nDisallow: /private/\n"
    transport = Recorder(
        {"https://shop.example/robots.txt": (200, robots, {"Content-Type": "text/plain"})}
    )
    result = guarded_fetcher(transport, PUBLIC).fetch("https://shop.example/x/%2e%2e/private/s")
    assert result.skipped and "blocked by" in result.skipped
    assert not any("private" in c for c in transport.calls)


TRICKY_URLS = [
    "https://shop.example/x/%2e%2e/private/secret",
    "https://shop.example/a/./b/../../c",
    "http://a%20b@[fe80::1%25eth0]:80/",
    "http://u%25ser@shop.example/%252e%252e/",
    "https://straße.example/über uns",
    "http://169.254.169.254\\@shop.example/../latest/",
    "HTTP://Shop.Example:443/A?b=%2F#c",
]


@pytest.mark.parametrize("url", TRICKY_URLS)
def test_canonical_url_is_a_fixed_point_or_rejected(url):
    import requests

    from lead_enrich.urls import InvalidURLError, canonical_url

    try:
        once = canonical_url(url)
    except InvalidURLError:
        return
    assert canonical_url(once) == once
    assert requests.Request("GET", once).prepare().url == once  # what requests really sends
