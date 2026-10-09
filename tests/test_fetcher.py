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
    fetcher = PoliteFetcher(fetch_settings, transport, cache, clock=clock, sleep=clock.sleep)
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


def test_disk_cache_avoids_second_request(fetch_settings, clock, tmp_path):
    script = {"https://a.example/": [(200, "cached body")]}
    cache = JsonDiskCache(tmp_path)
    fetcher, transport = make(fetch_settings, clock, script, cache)
    fetcher.fetch("https://a.example/")
    n_calls = len(transport.calls)

    fetcher2, transport2 = make(fetch_settings, clock, script, cache)
    result = fetcher2.fetch("https://a.example/")
    assert result.from_cache and result.text == "cached body"
    assert transport2.calls == []  # robots.txt also came from the cache
    assert n_calls == 2


def test_errors_are_not_cached(fetch_settings, clock, tmp_path):
    cache = JsonDiskCache(tmp_path)
    fetcher, _ = make(fetch_settings, clock, {"https://a.example/": [(500, "")]}, cache)
    fetcher.fetch("https://a.example/")
    assert cache.get("page:https://a.example/") is None


@pytest.mark.parametrize("ttl,expect_hit", [(None, True), (0, False)])
def test_cache_ttl(tmp_path, ttl, expect_hit):
    cache = JsonDiskCache(tmp_path, ttl_hours=ttl)
    cache.set("k", {"v": 1})
    assert (cache.get("k") is not None) == expect_hit
