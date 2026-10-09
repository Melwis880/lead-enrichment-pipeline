from lead_enrich.robots import RobotsPolicy

UA = "LeadEnrichBot/0.1 (+https://example.org)"


def test_rules_are_obeyed_for_our_user_agent():
    text = "User-agent: LeadEnrichBot\nDisallow: /private\n\nUser-agent: *\nDisallow: /\n"
    policy = RobotsPolicy.from_status(200, text, UA)
    assert policy.allowed("https://a.example/")
    assert policy.allowed("https://a.example/about")
    assert not policy.allowed("https://a.example/private/team")


def test_wildcard_group_applies_when_not_named():
    policy = RobotsPolicy.from_status(200, "User-agent: *\nDisallow: /\n", UA)
    assert not policy.allowed("https://a.example/")


def test_empty_robots_allows_everything():
    assert RobotsPolicy.from_status(200, "", UA).allowed("https://a.example/contact")


def test_4xx_means_unavailable_and_allows_crawling():
    for status in (401, 403, 404, 410):
        policy = RobotsPolicy.from_status(status, "", UA)
        assert policy.mode == "allow_all"
        assert policy.allowed("https://a.example/")


def test_5xx_and_network_error_mean_disallow_all():
    assert not RobotsPolicy.from_status(503, "", UA).allowed("https://a.example/")
    assert not RobotsPolicy.from_status(None, "", UA).allowed("https://a.example/")


def test_crawl_delay_is_read():
    policy = RobotsPolicy.from_status(200, "User-agent: *\nCrawl-delay: 7\n", UA)
    assert policy.crawl_delay() == 7.0
    assert RobotsPolicy.from_status(404, "", UA).crawl_delay() is None
