"""Command line: ``enrich run input/companies.csv``."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import click

from .cache import JsonDiskCache
from .config import load_dotenv, load_settings
from .extractors import ClaudeExtractor, FatalExtractorError, MockExtractor
from .fetcher import FixtureTransport, PoliteFetcher, RequestsTransport
from .pipeline import (
    LLM_CACHE_PREFIX,
    InputError,
    LeadRecord,
    RunStats,
    read_companies,
    run_pipeline,
)
from .report import write_csv, write_stats, write_summary
from .schema import EXTRACTION_JSON_SCHEMA
from .scoring import load_icp

# Rough planning numbers for --dry-run only; real runs report measured usage.
CHARS_PER_TOKEN = 4
PROMPT_OVERHEAD_TOKENS = 1_000  # system prompt + schema + wrapper
TYPICAL_OUTPUT_TOKENS = 450


@click.group()
@click.version_option(package_name="lead-enrich")
def main() -> None:
    """Enrich company websites into a scored lead list (public business info only)."""


@main.command()
@click.argument("input_csv", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--config",
    "config_path",
    default="config/settings.yaml",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Pipeline settings YAML.",
)
@click.option(
    "--icp",
    "icp_path",
    default="config/icp.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Scoring rules YAML.",
)
@click.option(
    "--output-dir",
    default="output",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
)
@click.option(
    "--html-fixtures",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Serve websites from saved HTML in this folder instead of the network.",
)
@click.option("--limit", type=int, help="Only process the first N companies.")
@click.option("--no-cache", is_flag=True, help="Ignore and do not write the on-disk caches.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate input and config, show the plan and a cost estimate. "
    "No network, no LLM calls, no files written.",
)
def run(
    input_csv: Path,
    config_path: Path,
    icp_path: Path,
    output_dir: Path,
    html_fixtures: Path | None,
    limit: int | None,
    no_cache: bool,
    dry_run: bool,
) -> None:
    """Fetch, extract, validate and score every company in INPUT_CSV."""
    load_dotenv()
    settings = load_settings(config_path)
    try:
        icp = load_icp(icp_path)
        companies, warnings = read_companies(input_csv)
    except (InputError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if limit is not None:
        companies = companies[:limit]
    if not companies:
        raise click.ClickException(f"no companies to process in {input_csv}")
    for w in warnings:
        click.secho(f"warning: {w}", fg="yellow", err=True)

    llm_mode = (
        "mock LLM (saved fixtures)" if settings.mock_llm else f"Claude API ({settings.llm.model})"
    )
    web_mode = f"HTML fixtures from {html_fixtures}" if html_fixtures else "live websites"
    mode = f"{llm_mode} + {web_mode}"

    if not settings.mock_llm and settings.llm.price() is None:
        click.secho(
            f"warning: no price for model {settings.llm.model} in llm.prices; "
            "LLM cost will be reported as unknown",
            fg="yellow",
            err=True,
        )

    if dry_run:
        _print_plan(companies, settings, icp_path, mode)
        return

    http_cache = llm_cache = None
    if not no_cache:
        # Fixture runs get their own cache folder so they never mix with live data.
        suffix = "-fixtures" if html_fixtures else ""
        http_cache = JsonDiskCache(
            settings.fetch.cache_dir + suffix, settings.fetch.cache_ttl_hours
        )
        llm_cache = JsonDiskCache(settings.llm.cache_dir + suffix, settings.llm.cache_ttl_hours)
        # Drop expired entries and raw model answers left by older versions, so nothing that
        # validation would reject stays on disk.
        pruned = http_cache.prune() + llm_cache.prune(lambda k: k.startswith(LLM_CACHE_PREFIX))
        if pruned:
            click.echo(f"cache: removed {pruned} expired or outdated entries")

    if html_fixtures:
        # Waiting between reads of local files protects nobody; the wait is still counted.
        fetcher = PoliteFetcher(
            settings.fetch,
            FixtureTransport(html_fixtures),
            http_cache,
            sleep=lambda _s: None,
            host_check=None,  # saved files, no network
        )
    else:
        fetcher = PoliteFetcher(
            settings.fetch,
            RequestsTransport(
                settings.fetch.max_page_bytes,
                settings.fetch.deadline_seconds,
                settings.fetch.connect_timeout_seconds,
            ),
            http_cache,
        )
    if settings.mock_llm:
        extractor = MockExtractor(settings.llm.mock_fixtures_dir)
    else:
        extractor = ClaudeExtractor(settings.llm)

    stats = RunStats(
        input_path=str(input_csv),
        mode=mode,
        model=settings.llm.model if not settings.mock_llm else "none (mock)",
        input_warnings=warnings,
    )
    click.echo(f"Enriching {len(companies)} companies - {mode}")
    records: list[LeadRecord] = []
    fatal: FatalExtractorError | None = None
    interrupted = False
    try:
        run_pipeline(
            companies,
            fetcher,
            extractor,
            settings,
            icp,
            stats,
            _progress,
            records,
            page_cache=http_cache,
            llm_cache=llm_cache,  # validated results only; the mock extractor is never cached
        )
    except FatalExtractorError as exc:
        fatal = exc
        stats.interrupted = f"Stopped by a configuration error: {exc}"
    except KeyboardInterrupt:
        interrupted = True
        stats.interrupted = "Interrupted by the user"
    if stats.interrupted:
        stats.interrupted += f" after {len(records)} of {len(companies)} companies."

    if records:  # never lose finished companies, even when the run stops early
        csv_path = output_dir / "enriched.csv"
        summary_path = output_dir / "summary.md"
        stats_path = output_dir / "run_stats.json"
        write_csv(records, csv_path)
        write_summary(records, stats, icp, summary_path)
        payload = write_stats(records, stats, stats_path)
        click.echo(
            f"\nDone in {payload['wall_time_s']} s. Tiers: {payload['tier_counts']}. "
            f"LLM cost: {_dollars(payload['cost_usd'])}"
        )
        click.echo(f"Wrote {csv_path}, {summary_path}, {stats_path}")
    if fatal is not None:
        raise click.ClickException(str(fatal)) from fatal
    if interrupted:
        click.secho(stats.interrupted, fg="yellow", err=True)
        sys.exit(130)
    if not any(r.status == "ok" for r in records):
        sys.exit(1)


CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _printable(text: str) -> str:
    """Site- and input-controlled text with control characters replaced: an ESC sequence in an
    error message must not be able to reset or rewrite the operator's terminal."""
    return CONTROL_CHARS_RE.sub("?", text)


def _progress(i: int, n: int, record: LeadRecord) -> None:
    if record.score:
        detail = f"tier {record.score.tier}, score {record.score.score}"
        flags = record.enrichment.flags() if record.enrichment else []
        if flags:
            detail += f", {len(flags)} flagged"
    else:
        detail = f"{record.status}: {record.error}"
    line = f"{record.company.name} ({record.company.domain}) - {detail}"
    click.echo(f"[{i}/{n}] {_printable(line)}")


def _dollars(cost: float | None) -> str:
    return "unknown price" if cost is None else f"${cost:.4f}"


def _print_plan(companies, settings, icp_path: Path, mode: str) -> None:
    llm = settings.llm
    price = llm.price()
    max_in = PROMPT_OVERHEAD_TOKENS + llm.max_input_chars // CHARS_PER_TOKEN
    pages = len(companies) * settings.fetch.max_pages_per_company
    min_minutes = pages * settings.fetch.min_delay_seconds / 60
    click.echo("Dry run - nothing will be fetched, sent or written.\n")
    click.echo(f"Mode:       {mode}")
    click.echo(f"Companies:  {len(companies)}")
    for c in companies[:20]:
        click.echo(f"  - {c.name}: https://{c.domain}/")
    if len(companies) > 20:
        click.echo(f"  ... and {len(companies) - 20} more")
    click.echo(
        f"Pages:      up to {pages} (+ one robots.txt per site), "
        f"User-Agent: {settings.fetch.user_agent}"
    )
    click.echo(
        f"Politeness: >= {settings.fetch.min_delay_seconds}s between requests per site; "
        f"sequential run needs at least ~{min_minutes:.1f} min of delays on live sites"
    )
    click.echo(
        f"Scoring:    {icp_path}; low-confidence threshold {settings.validation.min_confidence}"
    )
    click.echo(f"Schema:     {len(EXTRACTION_JSON_SCHEMA['properties'])} fields, strict JSON")
    if settings.mock_llm:
        click.echo("Cost:       $0 (mock LLM)")
    elif price is None:
        click.echo(f"Cost:       unknown price for model {llm.model} (no entry in llm.prices)")
    else:
        per_company = (max_in * price.input + TYPICAL_OUTPUT_TOKENS * price.output) / 1_000_000
        click.echo(
            f"Cost:       estimate <= ${per_company * len(companies):.2f} "
            f"(<= ${per_company * 100:.2f} per 100 companies; assumes full "
            f"{llm.max_input_chars}-char input, ~{TYPICAL_OUTPUT_TOKENS} output tokens, "
            f"${price.input}/${price.output} per MTok)"
        )
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            click.secho(
                "Note:       ANTHROPIC_API_KEY is not set; a real run will need it "
                "(or MOCK_LLM=1).",
                fg="yellow",
            )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
