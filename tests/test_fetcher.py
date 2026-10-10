import pytest

from lead_enrich.cache import JsonDiskCache
from lead_enrich.fetcher import HttpResponse, PoliteFetcher, TransportError


class ScriptedTransport:
    """Returns queued responses per URL; a queued Exception is raised instead."""

    def __init__(self, script: dict[str, list]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers, timeout):
        self.calls.append((url, headers))
        queue = self.script.get(url)
        if not queue:
            return HttpResponse(url, 404, {"Content-Type": "text/html"}, "")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        status, body, *extra = item
        headers_out = {"Content-Type": "text/html"}
        if extra:
            headers_out.update(extra[0])
        return HttpResponse(url, status, headers_out, body)


def make(fetch_settings, clock, script, cache=None):
    transport = ScriptedTransport(script)
    fetcher = PoliteFetcher(
        fetch_settings, transport, cache, clock=clock, sleep=clock.sleep, host_check=None
    )
    return fetcher, transport


def test_sends_clear_user_agent(fetch_settings, clock):
    fetcher, transport = make(fetch_settings, clock, {"https://a.example/": [(200, "<p>hi</p>")]})
    result = fetcher.fetch("https://a.example/")
    assert result.ok and "hi" in result.text
    assert all("LeadEnrichBot" in h["User-Agent"] for _, h in transport.calls)


def test_robots_disallow_skips_page_without_requesting_it(fetch_settings, clock):
    script = {"https://a.example/robots.txt": [(200, "User-agent: *\nDisallow: /\n")]}
    fetcher, transport = make(fetch_settings, clock, script)
    result = fetcher.fetch("https://a.example/about")
    assert result.skipped == "blocked by robots.txt rules"
    assert [u for u, _ in transport.calls] == ["https://a.example/robots.txt"]


def test_robots_server_error_blocks_site(fetch_settings, clock):
    script = {"https://a.example/robots.txt": [(503, "")]}
    fetcher, transport = make(fetch_settings, clock, script)
    assert fetcher.fetch("https://a.example/").skipped
    # robots.txt was retried, the page itself never requested
    assert {u for u, _ in transport.calls} == {"https://a.example/robots.txt"}


def test_rate_limit_waits_between_requests_to_same_domain(fetch_settings, clock):
    script = {"https://a.example/": [(200, "x")], "https://a.example/about": [(200, "y")]}
    fetcher, _ = make(fetch_settings, clock, script)
    fetcher.fetch("https://a.example/")
    fetcher.fetch("https://a.example/about")
    # robots, /, /about -> two waits of the configured 2s
    assert clock.sleeps == [2.0, 2.0]


def test_crawl_delay_overrides_smaller_min_delay(fetch_settings, clock):
    script = {
        "https://a.example/robots.txt": [(200, "User-agent: *\nCrawl-delay: 5\n")],
        "https://a.example/": [(200, "x")],
        "https://a.example/about": [(200, "y")],
    }
    fetcher, _ = make(fetch_settings, clock, script)
    fetcher.fetch("https://a.example/")
    fetcher.fetch("https://a.example/about")
    assert clock.sleeps[-1] == 5.0


def test_huge_crawl_delay_skips_site(fetch_settings, clock):
    script = {"https://a.example/robots.txt": [(200, "User-agent: *\nCrawl-delay: 3600\n")]}
    fetcher, _ = make(fetch_settings, clock, script)
    assert fetcher.fetch("https://a.example/").skipped.startswith("crawl_delay_too_long")


def test_retries_on_503_then_succeeds(fetch_settings, clock):
    script = {"https://a.example/": [(503, ""), (503, ""), (200, "ok")]}
    fetcher, _ = make(fetch_settings, clock, script)
    result = fetcher.fetch("https://a.example/")
    assert result.ok and result.text == "ok"
    assert fetcher.stats["retries"] == 2


def test_honours_retry_after_header(fetch_settings, clock):
    script = {"https://a.example/": [(429, "", {"Retry-After": "9"}), (200, "ok")]}
    fetcher, _ = make(fetch_settings, clock, script)
    assert fetcher.fetch("https://a.example/").ok
    assert 9.0 in clock.sleeps


def test_lowercase_retry_after_header_is_honoured(fetch_settings, clock):
    script = {"https://a.example/": [(429, "", {"retry-after": "9"}), (200, "ok")]}
    fetcher, _ = make(fetch_settings, clock, script)
    assert fetcher.fetch("https://a.example/").ok
    assert 9.0 in clock.sleeps


def test_gives_up_after_max_retries(fetch_settings, clock):
    script = {"https://a.example/": [(500, "")]}
    fetcher, transport = make(fetch_settings, clock, script)
    result = fetcher.fetch("https://a.example/")
    assert not result.ok and result.error == "HTTP 500"
    page_calls = [u for u, _ in transport.calls if u.endswith("/")]
    assert len(page_calls) == 3  # first try + 2 retries


def test_timeout_is_reported_not_raised(fetch_settings, clock):
    script = {"https://a.example/": [TransportError("timeout after 10s")]}
    fetcher, _ = make(fetch_settings, clock, script)
    result = fetcher.fetch("https://a.example/")
    assert result.error and "timeout" in result.error


def test_404_is_not_retried(fetch_settings, clock):
    fetcher, transport = make(fetch_settings, clock, {})
    result = fetcher.fetch("https://a.example/missing")
    assert result.error == "HTTP 404"
    assert fetcher.stats["retries"] == 0


def test_non_html_is_rejected(fetch_settings, clock):
    script = {"https://a.example/": [(200, "%PDF", {"Content-Type": "application/pdf"})]}
    fetcher, _ = make(fetch_settings, clock, script)
    assert "not HTML" in fetcher.fetch("https://a.example/").error


def test_redirect_is_followed_and_checked_against_target_robots(fetch_settings, clock):
    script = {
        "https://a.example/": [(301, "", {"Location": "https://www.a.example/"})],
        "https://www.a.example/robots.txt": [(200, "User-agent: *\nDisallow: /\n")],
    }
    fetcher, _ = make(fetch_settings, clock, script)
    result = fetcher.fetch("https://a.example/")
    assert result.skipped and result.final_url == "https://www.a.example/"


def test_page_cache_stores_parsed_redacted_page_and_skips_second_request(
    fetch_settings, clock, tmp_path
):
    from lead_enrich.pipeline import PageSource

    html = "<html><body><p>Hi, write to jane.doe@a.example</p></body></html>"
    script = {"https://a.example/": [(200, html)]}
    cache = JsonDiskCache(tmp_path)
    fetcher, transport = make(fetch_settings, clock, script, cache)
    _, page = PageSource(fetcher, cache).get("https://a.example/")
    assert page is not None and len(transport.calls) == 2  # robots.txt + page

    fetcher2, transport2 = make(fetch_settings, clock, script, cache)
    result, page2 = PageSource(fetcher2, cache).get("https://a.example/")
    assert result.from_cache and transport2.calls == []
    assert page2.text == page.text and "jane.doe" not in page2.text
    on_disk = "".join(f.read_text() for f in tmp_path.rglob("*.json"))
    assert "jane.doe" not in on_disk and "<html" not in on_disk


def test_fetcher_caches_robots_but_not_pages(fetch_settings, clock, tmp_path):
    script = {"https://a.example/": [(200, "<p>body</p>")]}
    cache = JsonDiskCache(tmp_path)
    fetcher, _ = make(fetch_settings, clock, script, cache)
    fetcher.fetch("https://a.example/")
    fetcher2, transport2 = make(fetch_settings, clock, script, cache)
    assert not fetcher2.fetch("https://a.example/").from_cache
    assert [u for u, _ in transport2.calls] == ["https://a.example/"]  # robots from cache


def test_errors_are_not_cached(fetch_settings, clock, tmp_path):
    from lead_enrich.pipeline import PageSource

    cache = JsonDiskCache(tmp_path)
    fetcher, _ = make(fetch_settings, clock, {"https://a.example/": [(500, "")]}, cache)
    PageSource(fetcher, cache).get("https://a.example/")
    assert not any("parsed" in f.read_text() for f in tmp_path.rglob("*.json"))


@pytest.mark.parametrize("ttl,expect_hit", [(None, True), (0, False)])
def test_cache_ttl(tmp_path, ttl, expect_hit):
    cache = JsonDiskCache(tmp_path, ttl_hours=ttl)
    cache.set("k", {"v": 1})
    assert (cache.get("k") is not None) == expect_hit


@pytest.mark.parametrize("value", ["²", "٣", "1e3", "-5", "123456", " 9x"])
def test_odd_retry_after_values_fall_back_to_backoff(fetch_settings, clock, value):
    script = {"https://a.example/": [(503, "", {"Retry-After": value}), (200, "ok")]}
    fetcher, _ = make(fetch_settings, clock, script)
    assert fetcher.fetch("https://a.example/").ok
    assert clock.sleeps[-1] == 1.0  # normal backoff, header ignored


def test_unreachable_site_is_a_network_error_not_a_robots_block(fetch_settings, clock):
    script = {
        "https://a.example/robots.txt": [TransportError("connection refused")],
        "https://a.example/": [TransportError("connection refused")],
    }
    fetcher, transport = make(fetch_settings, clock, script)
    result = fetcher.fetch("https://a.example/")
    assert result.skipped is None
    assert result.error.startswith("network error")
    assert "https://a.example/" not in [u for u, _ in transport.calls]  # still never fetched
