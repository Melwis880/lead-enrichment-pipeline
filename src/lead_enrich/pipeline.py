"""Orchestration: CSV in -> fetch -> parse -> extract -> validate -> score -> records out."""

from __future__ import annotations

import csv
import hashlib
import ipaddress
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .cache import JsonDiskCache
from .config import Settings
from .extractors import Extractor, FatalExtractorError, user_message
from .fetcher import FetchResult, PoliteFetcher
from .parser import (
    PARSER_VERSION,
    ParsedPage,
    build_llm_document,
    discover_pages,
    normalize_domain,
    parse_html,
)
from .schema import (
    VALIDATION_VERSION,
    Enrichment,
    Evidence,
    ExtractionError,
    validate_extraction,
)
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
    insecure_transport: bool = False  # some pages came over plain HTTP
    personal_emails_removed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0  # None: LLM called, but its price is unknown
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
            if _is_ip_literal(domain):
                warnings.append(f"line {line_no}: skipped, IP addresses are not accepted")
                continue
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


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


class PageSource:
    """Fetch + parse, with an optional cache of the *parsed, redacted* page.

    Raw HTML never touches the disk: what is cached is the ParsedPage, whose text already has
    personal email addresses removed. The key carries PARSER_VERSION, so a change to parsing or
    redaction invalidates older entries instead of reusing weaker-redacted text.
    """

    def __init__(self, fetcher: PoliteFetcher, cache: JsonDiskCache | None = None) -> None:
        self.fetcher = fetcher
        self.cache = cache

    def get(self, url: str) -> tuple[FetchResult, ParsedPage | None]:
        key = f"parsed-{PARSER_VERSION}:{url}"
        if self.cache is not None and (hit := self.cache.get(key)):
            self.fetcher.stats["cache_hits"] += 1
            page = ParsedPage.from_dict(hit["page"])
            return FetchResult(url, page.url, 200, from_cache=True), page
        result = self.fetcher.fetch(url)
        if not result.ok:
            return result, None
        page = parse_html(result.text, result.final_url)
        result.text = ""  # drop the raw HTML as soon as it is parsed
        if self.cache is not None:
            self.cache.set(key, {"page": page.to_dict()})
        return result, page


# Network failures on https:// after which plain http:// is worth one try.
HTTP_FALLBACK_KINDS = frozenset({"tls", "refused"})


def _fetch_home(source: PageSource, domain: str) -> tuple[FetchResult, ParsedPage | None]:
    result, page = source.get(f"https://{domain}/")
    if result.error_kind in HTTP_FALLBACK_KINDS:
        # Some small-business sites still have no working TLS (broken handshake, nothing on 443).
        # Try plain HTTP once. Not after a timeout: a host that does not answer on 443 rarely
        # answers on 80, and waiting for it twice is what made dead sites so slow.
        fallback, fallback_page = source.get(f"http://{domain}/")
        if fallback.ok or fallback.skipped:
            return fallback, fallback_page
    return result, page


def _llm_cache_key(
    extractor: Extractor,
    company: Company,
    document: str,
    evidence: Evidence,
    min_confidence: float,
) -> str | None:
    """Key of the validated-result cache: what decides the model's answer plus everything
    validation reads. None for extractors that are not cached (the mock)."""
    identity = getattr(extractor, "cache_identity", None)
    if identity is None:
        return None
    blob = json.dumps(
        [
            identity(),
            user_message(company.name, company.domain, document),
            evidence.fingerprint(),
            min_confidence,
            VALIDATION_VERSION,
        ],
        sort_keys=True,
    )
    return "llm-validated:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def enrich_company(
    company: Company,
    fetcher: PoliteFetcher,
    extractor: Extractor,
    settings: Settings,
    icp: ICP,
    page_cache: JsonDiskCache | None = None,
    llm_cache: JsonDiskCache | None = None,
) -> LeadRecord:
    started = time.monotonic()
    record = LeadRecord(company=company)
    source = PageSource(fetcher, page_cache)
    try:
        home, home_page = _fetch_home(source, company.domain)
        if home.skipped:
            record.status, record.error = "skipped_robots", home.skipped
            return record
        if home_page is None:
            record.status, record.error = "fetch_failed", f"homepage: {home.error}"
            return record

        pages: list[ParsedPage] = [home_page]
        record.pages.append(home_page.url)
        for url in discover_pages(pages[0], company.domain, settings.fetch.max_pages_per_company):
            _sub, sub_page = source.get(url)
            if sub_page is not None:
                pages.append(sub_page)
                record.pages.append(sub_page.url)
        record.fetch_s = time.monotonic() - started
        record.personal_emails_removed = sum(p.personal_emails_removed for p in pages)

        document, record.input_truncated = build_llm_document(pages, settings.llm.max_input_chars)
        secure = [p for p in pages if p.url.startswith("https://")]
        insecure = [p for p in pages if not p.url.startswith("https://")]
        record.insecure_transport = bool(insecure)
        evidence = Evidence(
            domain=company.domain,
            generic_emails={e for p in secure for e in p.generic_emails},
            form_urls={p.url for p in secure if p.has_contact_form},
            insecure_emails={e for p in insecure for e in p.generic_emails},
            insecure_form_urls={p.url for p in insecure if p.has_contact_form},
        )
        min_confidence = settings.validation.min_confidence
        key = _llm_cache_key(extractor, company, document, evidence, min_confidence)
        if llm_cache is not None and key and (hit := llm_cache.get(key)):
            record.enrichment = Enrichment.model_validate(hit["enrichment"])
            record.llm_cached = True
        else:
            llm_started = time.monotonic()
            result = extractor.extract(company.name, company.domain, document)
            record.llm_s = time.monotonic() - llm_started
            record.input_tokens, record.output_tokens = result.input_tokens, result.output_tokens
            record.cost_usd = result.cost_usd
            if result.error or result.data is None:
                record.status, record.error = "extraction_failed", result.error or "no data"
                return record
            try:
                record.enrichment = validate_extraction(result.data, evidence, min_confidence)
            except ExtractionError as exc:
                record.status, record.error = "extraction_failed", str(exc)
                return record
            if llm_cache is not None and key:
                # Only the validated result is stored, never the raw answer: rejected values
                # (a personal address the model repeated, an unverified contact) stay off disk.
                llm_cache.set(key, {"enrichment": record.enrichment.model_dump()})
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
    page_cache: JsonDiskCache | None = None,
    llm_cache: JsonDiskCache | None = None,
) -> list[LeadRecord]:
    """Enrich every company. Results are appended to ``records`` as they finish, so a caller
    that passes its own list keeps the finished companies if the run is interrupted."""
    records = [] if records is None else records
    started = time.monotonic()
    try:
        for i, company in enumerate(companies, start=1):
            try:
                record = enrich_company(
                    company, fetcher, extractor, settings, icp, page_cache, llm_cache
                )
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
