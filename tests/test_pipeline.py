"""End-to-end and failure-path tests on the saved demo fixtures. No network, no API."""

import csv
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lead_enrich import extractors
from lead_enrich.cli import main
from lead_enrich.config import Settings
from lead_enrich.extractors import ExtractionResult, MockExtractor
from lead_enrich.fetcher import FixtureTransport, PoliteFetcher
from lead_enrich.pipeline import Company, InputError, enrich_company, read_companies
from lead_enrich.report import safe_cell
from lead_enrich.scoring import load_icp

ROOT = Path(__file__).resolve().parents[1]
SITES = ROOT / "fixtures" / "sites"
INPUT = ROOT / "input" / "companies.csv"


def cli_args(tmp_path, *extra):
    return [
        "run",
        str(INPUT),
        "--config",
        str(ROOT / "config" / "settings.yaml"),
        "--icp",
        str(ROOT / "config" / "icp.yaml"),
        "--output-dir",
        str(tmp_path / "out"),
        "--html-fixtures",
        str(SITES),
        *extra,
    ]


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # caches land in tmp, not in the repo
    return tmp_path


def run_cli(tmp_path, *extra, mock=True):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        (ROOT / "config" / "settings.yaml")
        .read_text()
        .replace("mock_fixtures_dir: fixtures/llm", f"mock_fixtures_dir: {ROOT / 'fixtures/llm'}")
    )
    args = cli_args(tmp_path, *extra)
    args[args.index("--config") + 1] = str(settings_path)
    env = {"MOCK_LLM": "1" if mock else "0"}
    return CliRunner().invoke(main, args, env=env, catch_exceptions=False)


def read_rows(tmp_path):
    with (tmp_path / "out" / "enriched.csv").open(encoding="utf-8") as fh:
        return {r["domain"]: r for r in csv.DictReader(fh)}


def test_mock_run_end_to_end(in_tmp):
    result = run_cli(in_tmp)
    assert result.exit_code == 0, result.output
    rows = read_rows(in_tmp)
    assert len(rows) == 13

    first = next(iter(rows.values()))
    assert first["tier"] == "A"

    bakery = rows["harbourside-sourdough.example"]
    assert bakery["status"] == "ok"
    assert bakery["contact_email"] == "hello@harbourside-sourdough.example"
    assert "size 2-10 (+30)" in bakery["score_reasons"]
    assert "meet-the-team" not in bakery["pages_fetched"]  # people pages are never fetched

    assert rows["oldmarketantiques.example"]["status"] == "skipped_robots"
    assert rows["gloucesterroaddeli.example"]["status"] == "fetch_failed"
    assert rows["bedminsterbikes.example"]["tier"] == "review"
    assert rows["ledgerandco.example"]["tier"] == "C"

    books = rows["redcliffebooks.example"]
    assert books["contact_email"] == ""
    assert "contact_email:personal_email_rejected" in books["flags"]

    yoga = rows["totterdownyoga.example"]
    assert yoga["size_signal"] == ""
    assert "size_signal:low_confidence(0.40)" in yoga["flags"]
    assert "contact_email:not_found_in_source" in yoga["flags"]

    stats = json.loads((in_tmp / "out" / "run_stats.json").read_text())
    assert stats["companies"] == 13
    assert stats["status_counts"] == {"ok": 11, "skipped_robots": 1, "fetch_failed": 1}
    assert stats["cost_usd"] == 0

    summary = (in_tmp / "out" / "summary.md").read_text()
    assert "## Top leads" in summary and "## Needs attention" in summary


def test_no_personal_data_in_any_output(in_tmp):
    run_cli(in_tmp)
    for name in ("enriched.csv", "summary.md", "run_stats.json"):
        text = (in_tmp / "out" / name).read_text()
        assert "priya" not in text.lower()
        assert "sam@" not in text.lower()


def test_rerun_uses_http_cache(in_tmp):
    run_cli(in_tmp)
    run_cli(in_tmp)
    stats = json.loads((in_tmp / "out" / "run_stats.json").read_text())
    assert stats["fetch"]["cache_hits"] > 0


def test_dry_run_writes_nothing(in_tmp):
    result = run_cli(in_tmp, "--dry-run", mock=False)
    assert result.exit_code == 0
    assert "Dry run" in result.output and "Cost:" in result.output
    assert "ANTHROPIC_API_KEY is not set" in result.output
    assert not (in_tmp / "out").exists()
    assert not (in_tmp / "cache").exists()


def test_limit_option(in_tmp):
    run_cli(in_tmp, "--limit", "2")
    assert len(read_rows(in_tmp)) == 2


def test_missing_credentials_abort_run(in_tmp, monkeypatch):
    class NoAuthClient:
        def __init__(self, **_):
            self.messages = self

        def create(self, **_):
            raise TypeError("Could not resolve authentication method.")

    monkeypatch.setattr(extractors.anthropic, "Anthropic", NoAuthClient)
    result = CliRunner().invoke(main, cli_args(in_tmp, "--no-cache"), env={"MOCK_LLM": "0"})
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
    assert not (in_tmp / "out" / "enriched.csv").exists()


def test_missing_input_file(in_tmp):
    args = cli_args(in_tmp)
    args[1] = str(in_tmp / "nope.csv")
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0 and "not found" in result.output


def test_csv_without_website_column(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("company,city\nA,Bristol\n")
    with pytest.raises(InputError, match="website column"):
        read_companies(bad)


def test_read_companies_normalises_and_dedupes(tmp_path):
    f = tmp_path / "in.csv"
    f.write_text("﻿Name,URL\nA,https://www.a.example/x\nA again,a.example\nB,\nC,not-a-domain\n")
    companies, warnings = read_companies(f)
    assert [c.domain for c in companies] == ["a.example"]
    assert len(warnings) == 3


class RecordingExtractor:
    label = "recording"

    def __init__(self):
        self.documents: list[str] = []
        self.inner = MockExtractor(ROOT / "fixtures" / "llm")

    def extract(self, company, domain, document):
        self.documents.append(document)
        return self.inner.extract(company, domain, document)


def make_fetcher(settings):
    return PoliteFetcher(
        settings.fetch, FixtureTransport(SITES), sleep=lambda _s: None, host_check=None
    )


def test_personal_email_never_reaches_the_llm():
    settings = Settings()
    ex = RecordingExtractor()
    record = enrich_company(
        Company("Redcliffe Books & Coffee", "redcliffebooks.example"),
        make_fetcher(settings),
        ex,
        settings,
        load_icp(ROOT / "config" / "icp.yaml"),
    )
    assert record.personal_emails_removed == 1
    assert "priya.example@" not in ex.documents[0].lower()
    assert "[personal email removed]" in ex.documents[0]
    # Known limitation: first names in free text ("Write to Priya") are not detected.
    # The schema has no person fields and the output test below checks nothing leaks out.


def test_extractor_failure_marks_company_not_run():
    class Failing:
        label = "failing"

        def extract(self, *a):
            return ExtractionResult(error="API error HTTP 500", input_tokens=10)

    settings = Settings()
    record = enrich_company(
        Company("Clifton Paws", "cliftonpaws.example"),
        make_fetcher(settings),
        Failing(),
        settings,
        load_icp(ROOT / "config" / "icp.yaml"),
    )
    assert record.status == "extraction_failed" and "HTTP 500" in record.error
    assert record.score is None


def test_timeout_site_is_fetch_failed(tmp_path):
    site = tmp_path / "slow.example"
    site.mkdir()
    (site / "index.status").write_text("timeout")
    settings = Settings()
    fetcher = PoliteFetcher(
        settings.fetch, FixtureTransport(tmp_path), sleep=lambda _s: None, host_check=None
    )
    record = enrich_company(
        Company("Slow", "slow.example"),
        fetcher,
        MockExtractor(tmp_path),
        settings,
        load_icp(ROOT / "config" / "icp.yaml"),
    )
    assert record.status == "fetch_failed" and "timeout" in record.error


@pytest.mark.parametrize(
    "value,expected",
    [("=HYPERLINK(1)", "'=HYPERLINK(1)"), ("+1", "'+1"), ("@x", "'@x"), ("ok", "ok"), (None, "")],
)
def test_csv_formula_injection_is_neutralised(value, expected):
    assert safe_cell(value) == expected


def test_unexpected_exception_fails_one_company_not_the_batch():
    from lead_enrich.pipeline import RunStats, run_pipeline

    class Exploding(MockExtractor):
        def extract(self, company, domain, document):
            if domain == "cliftonpaws.example":
                raise RuntimeError("parser bug on hostile page")
            return super().extract(company, domain, document)

    settings = Settings()
    companies, _ = read_companies(INPUT)
    stats = RunStats(input_path="x", mode="test", model="none")
    records = run_pipeline(
        companies,
        make_fetcher(settings),
        Exploding(ROOT / "fixtures" / "llm"),
        settings,
        load_icp(ROOT / "config" / "icp.yaml"),
        stats,
    )
    assert len(records) == 13
    bad = next(r for r in records if r.company.domain == "cliftonpaws.example")
    assert bad.status == "internal_error" and "RuntimeError" in bad.error
    assert sum(r.status == "ok" for r in records) == 10


def test_fatal_error_still_aborts_the_run():
    from lead_enrich.extractors import FatalExtractorError
    from lead_enrich.pipeline import RunStats, run_pipeline

    class BadKey:
        label = "bad key"

        def extract(self, *a):
            raise FatalExtractorError("bad key")

    settings = Settings()
    collected: list = []
    with pytest.raises(FatalExtractorError):
        run_pipeline(
            [Company("Clifton Paws", "cliftonpaws.example")],
            make_fetcher(settings),
            BadKey(),
            settings,
            load_icp(ROOT / "config" / "icp.yaml"),
            RunStats(input_path="x", mode="test", model="none"),
            records=collected,
        )


def test_interrupted_run_still_writes_partial_outputs(in_tmp, monkeypatch):
    calls = {"n": 0}
    original = MockExtractor.extract

    def interrupt_third(self, company, domain, document):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return original(self, company, domain, document)

    monkeypatch.setattr(MockExtractor, "extract", interrupt_third)
    result = run_cli(in_tmp)
    assert result.exit_code == 130
    rows = read_rows(in_tmp)
    assert len(rows) == 2
    summary = (in_tmp / "out" / "summary.md").read_text()
    assert "Interrupted" in summary
    stats = json.loads((in_tmp / "out" / "run_stats.json").read_text())
    assert stats["interrupted"]


def test_cache_at_rest_holds_no_raw_html_or_personal_emails(in_tmp):
    run_cli(in_tmp)
    files = [p for p in (in_tmp / "cache").rglob("*.json")]
    assert files, "the run should have written a cache"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in files).lower()
    assert "priya.example@" not in blob  # the personal email on redcliffebooks.example
    assert "<html" not in blob and "<body" not in blob  # parsed text only, never raw HTML


def test_cache_files_are_private(in_tmp):
    import stat

    run_cli(in_tmp)
    for p in (in_tmp / "cache").rglob("*"):
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode & 0o077 == 0, f"{p} is readable by group/others ({oct(mode)})"


class HttpOnlyTransport:
    """https fails at the network level; plain http serves a page with contact details."""

    PAGE = (
        "<html><body><p>Plain Shop. Email info@plain.example</p>"
        "<form><input type='email'><textarea></textarea></form></body></html>"
    )

    def get(self, url, headers, timeout):
        from lead_enrich.fetcher import HttpResponse, TransportError

        if url.startswith("https://"):
            raise TransportError("TLS handshake failed")
        if url.endswith("robots.txt"):
            return HttpResponse(url, 404, {"Content-Type": "text/plain"}, "")
        return HttpResponse(url, 200, {"Content-Type": "text/html"}, self.PAGE)


class PlainShopLLM:
    label = "plain"

    def extract(self, company, domain, document):
        return ExtractionResult(
            data={
                "industry": {"value": "retail", "detail": None, "confidence": 0.9},
                "description": {"value": "A shop.", "confidence": 0.9},
                "size_signal": {"value": "2-10", "evidence": "x", "confidence": 0.9},
                "location": {
                    "city": "Bristol",
                    "region": None,
                    "country_code": "GB",
                    "confidence": 0.9,
                },
                "products_services": {"value": ["Things"], "confidence": 0.9},
                "contact_email": {"value": "info@plain.example", "confidence": 0.95},
                "contact_form_url": {"value": "http://plain.example/", "confidence": 0.9},
            }
        )


def test_contact_channels_from_plain_http_pages_are_not_accepted(tmp_path):
    from lead_enrich.report import write_csv

    settings = Settings()
    fetcher = PoliteFetcher(
        settings.fetch, HttpOnlyTransport(), sleep=lambda _s: None, host_check=None
    )
    record = enrich_company(
        Company("Plain Shop", "plain.example"),
        fetcher,
        PlainShopLLM(),
        settings,
        load_icp(ROOT / "config" / "icp.yaml"),
    )
    assert record.status == "ok"
    assert record.insecure_transport is True
    assert record.enrichment.get("contact_email") is None
    assert record.enrichment.get("contact_form_url") is None
    assert "contact_email:insecure_source" in record.enrichment.flags()
    assert "contact_form_url:insecure_source" in record.enrichment.flags()
    assert "no verified contact channel (+0)" in record.score.reasons

    write_csv([record], tmp_path / "out.csv")
    with (tmp_path / "out.csv").open() as fh:
        row = next(csv.DictReader(fh))
    assert row["insecure_transport"] == "yes"
