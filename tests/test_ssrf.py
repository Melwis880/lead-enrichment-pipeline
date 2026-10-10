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
