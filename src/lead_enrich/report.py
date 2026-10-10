"""Outputs: enriched CSV, Markdown summary, machine-readable run stats."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from .pipeline import LeadRecord, RunStats
from .scoring import ICP

CSV_COLUMNS = [
    "company_name",
    "domain",
    "status",
    "tier",
    "score",
    "score_reasons",
    "industry",
    "industry_detail",
    "description",
    "size_signal",
    "size_evidence",
    "city",
    "region",
    "country_code",
    "products_services",
    "contact_email",
    "contact_form_url",
    "flags",
    "error",
    "insecure_transport",
    "pages_fetched",
    "industry_confidence",
    "description_confidence",
    "size_confidence",
    "location_confidence",
    "products_confidence",
    "contact_email_confidence",
    "contact_form_confidence",
]

TIER_ORDER = {"A": 0, "B": 1, "C": 2, "review": 3}
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> str:
    """Stringify and neutralise spreadsheet formula injection from website text."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def sort_key(r: LeadRecord) -> tuple:
    if r.score is None:
        return (9, 0, r.company.name.lower())
    return (TIER_ORDER.get(r.score.tier, 5), -r.score.score, r.company.name.lower())


def record_row(r: LeadRecord) -> dict[str, object]:
    row: dict[str, object] = {
        "company_name": r.company.name,
        "domain": r.company.domain,
        "status": r.status,
        "error": r.error,
        "insecure_transport": "yes" if r.insecure_transport else "",
        "pages_fetched": " ".join(r.pages),
    }
    if r.score:
        row.update(tier=r.score.tier, score=r.score.score, score_reasons="; ".join(r.score.reasons))
    if e := r.enrichment:
        f = e.fields
        loc = f["location"].value or {}
        row.update(
            industry=f["industry"].value,
            industry_detail=f["industry"].note if f["industry"].value else None,
            description=f["description"].value,
            size_signal=f["size_signal"].value,
            size_evidence=f["size_signal"].note if f["size_signal"].value else None,
            city=loc.get("city"),
            region=loc.get("region"),
            country_code=loc.get("country_code"),
            products_services="; ".join(f["products_services"].value or []),
            contact_email=f["contact_email"].value,
            contact_form_url=f["contact_form_url"].value,
            flags="; ".join(e.flags()),
            industry_confidence=f["industry"].confidence,
            description_confidence=f["description"].confidence,
            size_confidence=f["size_signal"].confidence,
            location_confidence=f["location"].confidence,
            products_confidence=f["products_services"].confidence,
            contact_email_confidence=f["contact_email"].confidence,
            contact_form_confidence=f["contact_form_url"].confidence,
        )
    return row


def write_csv(records: list[LeadRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in sorted(records, key=sort_key):
            row = record_row(r)
            writer.writerow({c: safe_cell(row.get(c)) for c in CSV_COLUMNS})


def compute_metrics(records: list[LeadRecord], stats: RunStats) -> dict[str, object]:
    n = len(records)
    tokens_in = sum(r.input_tokens for r in records)
    tokens_out = sum(r.output_tokens for r in records)
    cost = sum(r.cost_usd for r in records)
    llm_calls = sum(1 for r in records if r.input_tokens)
    fetch_times = [r.fetch_s for r in records if r.fetch_s]  # companies whose pages were fetched
    slowest = sorted(records, key=lambda r: r.total_s, reverse=True)[:3]
    return {
        "companies": n,
        "status_counts": dict(Counter(r.status for r in records)),
        "tier_counts": dict(Counter(r.score.tier for r in records if r.score)),
        "wall_time_s": round(stats.wall_time_s, 2),
        "avg_time_per_company_s": round(stats.wall_time_s / n, 2) if n else 0.0,
        "avg_fetch_time_s": round(sum(fetch_times) / len(fetch_times), 3) if fetch_times else None,
        "slowest_companies": [
            {"domain": r.company.domain, "status": r.status, "total_s": round(r.total_s, 3)}
            for r in slowest
        ],
        "politeness_wait_s": round(float(stats.fetch.get("waited_s", 0.0)), 1),
        "llm_calls": llm_calls,
        "llm_cache_hits": sum(1 for r in records if r.llm_cached),
        "avg_llm_latency_s": round(sum(r.llm_s for r in records if r.input_tokens) / llm_calls, 2)
        if llm_calls
        else None,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": round(cost, 4),
        "cost_per_100_companies_usd": round(cost / n * 100, 4) if n else 0.0,
        "flagged_fields": sum(len(r.enrichment.flags()) for r in records if r.enrichment),
        "personal_emails_redacted": sum(r.personal_emails_removed for r in records),
        "fetch": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in stats.fetch.items()},
    }


def write_stats(records: list[LeadRecord], stats: RunStats, path: Path) -> dict[str, object]:
    metrics = compute_metrics(records, stats)
    payload = {
        "started_at": stats.started_at,
        "input": stats.input_path,
        "mode": stats.mode,
        "model": stats.model,
        "input_warnings": stats.input_warnings,
        "interrupted": stats.interrupted,
        **metrics,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


_MD_ESCAPES = [
    ("\\", "\\\\"),  # first, so input cannot "un-escape" what follows
    ("&", "&amp;"),  # Slack: &, < and > are the only characters it decodes
    ("<", "&lt;"),  # kills <!channel>, <@U123>, <https://x|label>
    (">", "&gt;"),
    ("`", "\\`"),
    ("[", "\\["),  # kills Markdown links and images
    ("]", "\\]"),
    ("|", "\\|"),  # table cells
]


def _md(text: object) -> str:
    """Make untrusted text inert in Markdown (GitHub) and Slack mrkdwn."""
    out = " ".join(str(text if text is not None else "").split())
    for raw, escaped in _MD_ESCAPES:
        out = out.replace(raw, escaped)
    return out


def write_summary(records: list[LeadRecord], stats: RunStats, icp: ICP, path: Path) -> None:
    m = compute_metrics(records, stats)
    ranked = [r for r in sorted(records, key=sort_key) if r.score]
    lines = [
        "# Lead enrichment summary",
        "",
        f"- Run started: {_md(stats.started_at)} (UTC)",
        f"- Input: {_md(stats.input_path)} ({m['companies']} companies)",
        f"- Mode: {_md(stats.mode)}",
        f"- Model: {_md(stats.model)}",
        f"- Ideal customer profile: {_md(icp.name)}",
        "",
        *(
            [f"**Warning: {_md(stats.interrupted)}** Results below are partial.", ""]
            if stats.interrupted
            else []
        ),
        "## Results",
        "",
        "| Status | Companies |",
        "|---|---|",
        *[f"| {_md(k)} | {v} |" for k, v in sorted(m["status_counts"].items())],
        "",
        "| Tier | Companies |",
        "|---|---|",
        *[f"| {_md(t)} | {m['tier_counts'].get(t, 0)} |" for t in [*icp.tiers, "review"]],
        "",
        "## Top leads",
        "",
        "| # | Company | Tier | Score | Industry | Size | Location | Contact | Why |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(ranked[:10], start=1):
        f = r.enrichment.fields if r.enrichment else {}
        loc = (f["location"].value or {}) if f else {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("country_code")) if x)
        contact = (f["contact_email"].value or f["contact_form_url"].value) if f else None
        positive = [x for x in r.score.reasons if "(+0)" not in x and "tier=" not in x]
        lines.append(
            f"| {i} | {_md(r.company.name)} | {_md(r.score.tier)} | {r.score.score} "
            f"| {_md(f['industry'].value if f else '')} "
            f"| {_md(f['size_signal'].value if f else '')} "
            f"| {_md(where)} | {_md(contact) or 'none verified'} | {_md('; '.join(positive))} |"
        )

    attention = [
        r
        for r in records
        if r.status != "ok" or r.insecure_transport or (r.enrichment and r.enrichment.flags())
    ]
    lines += ["", "## Needs attention", ""]
    if not attention:
        lines.append("Nothing flagged.")
    for r in sorted(attention, key=lambda r: r.company.name.lower()):
        if r.status != "ok":
            lines.append(
                f"- **{_md(r.company.name)}** ({_md(r.company.domain)}): {_md(r.status)} - "
                f"{_md(r.error)}"
            )
        else:
            notes = []
            if r.insecure_transport:
                notes.append("fetched over plain HTTP, contact channels not accepted")
            if r.enrichment and r.enrichment.flags():
                notes.append("flagged " + ", ".join(r.enrichment.flags()))
            lines.append(f"- **{_md(r.company.name)}**: {_md('; '.join(notes))}")

    lines += [
        "",
        "## Run metrics (measured on this run)",
        "",
        f"- Wall time: {m['wall_time_s']} s ({m['avg_time_per_company_s']} s per company)",
        f"- Fetch time per company with pages: {m['avg_fetch_time_s']} s on average; slowest "
        "companies: "
        + (
            ", ".join(
                f"{_md(s['domain'])} ({s['total_s']} s, {_md(s['status'])})"
                for s in m["slowest_companies"]
            )
            or "none"
        ),
        f"- Politeness delay included: {m['politeness_wait_s']} s "
        "(simulated, not slept, when running on HTML fixtures)",
        f"- HTTP requests: {int(m['fetch'].get('requests', 0))}, cache hits: "
        f"{int(m['fetch'].get('cache_hits', 0))}, retries: {int(m['fetch'].get('retries', 0))}, "
        f"blocked by robots.txt: {int(m['fetch'].get('robots_blocked', 0))}",
        f"- LLM calls: {m['llm_calls']} (cache hits: {m['llm_cache_hits']}), tokens in/out: "
        f"{m['input_tokens']}/{m['output_tokens']}",
        f"- LLM cost: ${m['cost_usd']:.4f} "
        f"(${m['cost_per_100_companies_usd']:.4f} per 100 companies)",
        f"- Flagged fields: {m['flagged_fields']}; personal emails redacted before the LLM: "
        f"{m['personal_emails_redacted']}",
    ]
    if stats.input_warnings:
        lines += ["", "## Input warnings", "", *[f"- {_md(w)}" for w in stats.input_warnings]]
    lines += [
        "",
        "_Public business information only. Flagged fields were left blank on purpose; check them "
        "on the company's website before use._",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
