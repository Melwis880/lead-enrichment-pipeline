"""Orchestration: CSV in -> fetch -> parse -> extract -> validate -> score -> records out."""

from __future__ import annotations

import csv
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .extractors import Extractor, FatalExtractorError
from .fetcher import FetchResult, PoliteFetcher
from .parser import ParsedPage, build_llm_document, discover_pages, normalize_domain, parse_html
from .schema import Enrichment, Evidence, ExtractionError, validate_extraction
from .scoring import ICP, ScoreResult, score_lead

NAME_COLUMNS = ("company_name", "company", "name")
DOMAIN_COLUMNS = ("website", "domain", "url")


class InputError(ValueError):
    pass


@dataclass
class Company:
    name: str
    domain: str


@dataclass
class LeadRecord:
    company: Company
    status: str = "ok"  # ok | skipped_robots | fetch_failed | extraction_failed | internal_error
    error: str | None = None
    pages: list[str] = field(default_factory=list)
    enrichment: Enrichment | None = None
    score: ScoreResult | None = None
    input_truncated: bool = False
    personal_emails_removed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_cached: bool = False
    fetch_s: float = 0.0
    llm_s: float = 0.0
    total_s: float = 0.0


@dataclass
class RunStats:
    input_path: str
    mode: str
    model: str
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    wall_time_s: float = 0.0
    fetch: dict[str, float] = field(default_factory=dict)
    input_warnings: list[str] = field(default_factory=list)
    interrupted: str | None = None  # why the run stopped early, if it did


def read_companies(path: str | Path) -> tuple[list[Company], list[str]]:
    """Read the input CSV. Accepts company_name/name + website/domain/url columns."""
    path = Path(path)
    if not path.exists():
        raise InputError(f"input file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = [h.strip().lower() for h in (reader.fieldnames or [])]
        name_col = next((c for c in NAME_COLUMNS if c in headers), None)
        domain_col = next((c for c in DOMAIN_COLUMNS if c in headers), None)
        if domain_col is None:
            raise InputError(
                f"{path}: needs a website column (one of {', '.join(DOMAIN_COLUMNS)}); "
                f"found {headers or 'no header'}"
            )
        companies: list[Company] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for line_no, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            domain = normalize_domain(row.get(domain_col, ""))
            if not domain or "." not in domain:
                warnings.append(f"line {line_no}: skipped, no valid website")
                continue
            if domain in seen:
                warnings.append(f"line {line_no}: skipped duplicate {domain}")
                continue
            seen.add(domain)
            name = row.get(name_col, "") if name_col else ""
            companies.append(Company(name=name or domain, domain=domain))
    return companies, warnings


def _fetch_home(fetcher: PoliteFetcher, domain: str) -> FetchResult:
    result = fetcher.fetch(f"https://{domain}/")
    if result.error and result.error.startswith("network error"):
        # Some small-business sites still have no working TLS. Try plain HTTP once.
        fallback = fetcher.fetch(f"http://{domain}/")
        if fallback.ok or fallback.skipped:
            return fallback
    return result


def enrich_company(
    company: Company,
    fetcher: PoliteFetcher,
    extractor: Extractor,
    settings: Settings,
    icp: ICP,
) -> LeadRecord:
    started = time.monotonic()
    record = LeadRecord(company=company)
    try:
        home = _fetch_home(fetcher, company.domain)
        if home.skipped:
            record.status, record.error = "skipped_robots", home.skipped
            return record
        if not home.ok:
            record.status, record.error = "fetch_failed", f"homepage: {home.error}"
            return record

        pages: list[ParsedPage] = [parse_html(home.text, home.final_url)]
        record.pages.append(home.final_url)
        for url in discover_pages(pages[0], company.domain, settings.fetch.max_pages_per_company):
            sub = fetcher.fetch(url)
            if sub.ok:
                pages.append(parse_html(sub.text, sub.final_url))
                record.pages.append(sub.final_url)
        record.fetch_s = time.monotonic() - started
        record.personal_emails_removed = sum(p.personal_emails_removed for p in pages)

        document, record.input_truncated = build_llm_document(pages, settings.llm.max_input_chars)
        evidence = Evidence(
            domain=company.domain,
            generic_emails={e for p in pages for e in p.generic_emails},
            form_urls={p.url for p in pages if p.has_contact_form},
        )
        llm_started = time.monotonic()
        result = extractor.extract(company.name, company.domain, document)
        record.llm_s = time.monotonic() - llm_started
        record.input_tokens, record.output_tokens = result.input_tokens, result.output_tokens
        record.cost_usd, record.llm_cached = result.cost_usd, result.from_cache
        if result.error or result.data is None:
            record.status, record.error = "extraction_failed", result.error or "no data"
            return record
        try:
            record.enrichment = validate_extraction(
                result.data, evidence, settings.validation.min_confidence
            )
        except ExtractionError as exc:
            record.status, record.error = "extraction_failed", str(exc)
            return record
        record.score = score_lead(record.enrichment, icp)
        return record
    finally:
        record.total_s = time.monotonic() - started


def run_pipeline(
    companies: list[Company],
    fetcher: PoliteFetcher,
    extractor: Extractor,
    settings: Settings,
    icp: ICP,
    stats: RunStats,
    progress: Callable[[int, int, LeadRecord], None] | None = None,
    records: list[LeadRecord] | None = None,
) -> list[LeadRecord]:
    """Enrich every company. Results are appended to ``records`` as they finish, so a caller
    that passes its own list keeps the finished companies if the run is interrupted."""
    records = [] if records is None else records
    started = time.monotonic()
    try:
        for i, company in enumerate(companies, start=1):
            try:
                record = enrich_company(company, fetcher, extractor, settings, icp)
            except FatalExtractorError:
                raise
            except Exception as exc:  # one hostile or odd site must never sink the batch
                record = LeadRecord(
                    company=company,
                    status="internal_error",
                    error=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
            records.append(record)
            if progress:
                progress(i, len(companies), record)
    finally:
        stats.wall_time_s = time.monotonic() - started
        stats.fetch = dict(fetcher.stats)
    return records
