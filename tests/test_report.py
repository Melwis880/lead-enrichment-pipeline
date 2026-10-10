"""Summary output is posted to Slack (n8n) and rendered as Markdown: escape everything."""

import re
from pathlib import Path

import pytest

from lead_enrich.config import Settings
from lead_enrich.extractors import ExtractionResult
from lead_enrich.fetcher import HttpResponse, PoliteFetcher
from lead_enrich.pipeline import Company, RunStats, enrich_company
from lead_enrich.report import _md, write_summary
from lead_enrich.scoring import load_icp

ROOT = Path(__file__).resolve().parents[1]
EVIL = "<!channel> [win a prize](https://evil.example) `rm -rf` &amp;"
PAGE = "<html><body><p>Welcome</p><a href='/contact'>Contact</a></body></html>"


class HeaderTransport:
    """Serves one HTML page for good.example and a hostile Content-Type for bad.example."""

    def get(self, url, headers, timeout):
        if "bad.example" in url and not url.endswith("robots.txt"):
            return HttpResponse(url, 200, {"Content-Type": f"text/plain{EVIL}"}, "x")
        if url.endswith("robots.txt"):
            return HttpResponse(url, 404, {"Content-Type": "text/plain"}, "")
        return HttpResponse(url, 200, {"Content-Type": "text/html"}, PAGE)


class EvilLLM:
    label = "evil"

    def extract(self, company, domain, document):
        return ExtractionResult(
            data={
                "industry": {"value": "retail", "detail": EVIL, "confidence": 0.9},
                "description": {"value": EVIL, "confidence": 0.9},
                "size_signal": {"value": "2-10", "evidence": EVIL, "confidence": 0.9},
                "location": {"city": EVIL, "region": None, "country_code": "GB", "confidence": 0.9},
                "products_services": {"value": [EVIL], "confidence": 0.9},
                "contact_email": {"value": None, "confidence": 0.9},
                "contact_form_url": {"value": None, "confidence": 0.9},
            }
        )


@pytest.fixture
def summary(tmp_path):
    settings = Settings()
    fetcher = PoliteFetcher(
        settings.fetch, HeaderTransport(), sleep=lambda _s: None, host_check=None
    )
    icp = load_icp(ROOT / "config" / "icp.yaml")
    records = [
        enrich_company(Company(f"Name {EVIL}", d), fetcher, EvilLLM(), settings, icp)
        for d in ("good.example", "bad.example")
    ]
    assert records[1].status == "fetch_failed"  # the hostile header path really ran
    stats = RunStats(
        input_path=f"in {EVIL}.csv", mode=EVIL, model=EVIL, input_warnings=[EVIL], interrupted=EVIL
    )
    out = tmp_path / "summary.md"
    write_summary(records, stats, icp, out)
    return out.read_text()


def active_markup(text: str, chars: str = "[]`") -> list[str]:
    """Markup characters NOT neutralised by an odd number of preceding backslashes."""
    return [
        m.group(0)
        for m in re.finditer(rf"(\\*)([{re.escape(chars)}])", text)
        if len(m.group(1)) % 2 == 0
    ]


def test_no_slack_mentions_or_links_survive(summary):
    assert "<" not in summary and ">" not in summary  # no <!channel>, no <url|label>
    assert "&lt;!channel&gt;" in summary
    assert active_markup(summary) == []  # no live [link](url), no `code`
    assert "win a prize" in summary  # the text itself is still shown


def test_active_markup_helper():
    assert active_markup("a [b") == ["["]
    assert active_markup("a \\[b") == []
    assert active_markup("a \\\\[b") == ["\\\\["]


def test_raw_ampersand_entity_cannot_be_smuggled(summary):
    assert "&amp;amp;" in summary  # an "&amp;" in the input is shown, not decoded


def test_header_text_never_reaches_error_message():
    from lead_enrich.fetcher import safe_media_type

    assert safe_media_type(f"text/plain{EVIL}") == "invalid media type"
    assert safe_media_type("application/pdf; name=<!here>") == "application/pdf"
    assert safe_media_type("") == "missing"
    assert safe_media_type("Text/HTML ; charset=utf-8") == "text/html"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a|b", "a\\|b"),
        ("x\ny", "x y"),
        ("<b>", "&lt;b&gt;"),
        ("[a](b)", "\\[a\\](b)"),
        ("`c`", "\\`c\\`"),
        ("\\[a](b)", "\\\\\\[a\\](b)"),
        (None, ""),
        (0, "0"),
    ],
)
def test_md_escaping(raw, expected):
    assert _md(raw) == expected
