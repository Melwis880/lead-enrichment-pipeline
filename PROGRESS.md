# Progress

Status legend: `[x]` done, `[ ]` not done. Re-read at the start of each work chunk.

## Phase 0 - Tracking
- [x] PROGRESS.md, DECISIONS.md, BUILD_LOG.md created

## Phase 1 - Scaffold
- [x] `pyproject.toml`, `src/lead_enrich/` package, `enrich` console script
- [x] `.env.example`, `config/settings.yaml`, `config/icp.yaml`

## Phase 2 - Polite fetching
- [x] robots.txt handling (RFC 9309 status-code semantics, Crawl-delay)
- [x] Clear User-Agent, per-domain rate limit, timeouts, retries with backoff
- [x] On-disk response cache
- [x] HTML-fixture transport for offline runs and tests
- [x] Tests: robots, fetcher (retries, cache, rate limit, failure paths)

## Phase 3 - Parsing
- [x] Visible-text extraction, about/contact page discovery
- [x] Generic vs. personal email detection; redact personal emails before LLM
- [x] Tests

## Phase 4 - Extraction + validation
- [x] Strict JSON schema for the LLM, pydantic validation with per-field flags
- [x] Evidence check: contact channels must appear in fetched pages
- [x] Claude extractor (structured outputs, usage + cost tracking, error handling)
- [x] Mock extractor (`MOCK_LLM=1`) + LLM response cache
- [x] Tests

## Phase 5 - Scoring
- [x] YAML ICP config, rule engine with reason per rule, tiers
- [x] Tests

## Phase 6 - Pipeline + CLI + output
- [x] `enrich run input/companies.csv`, `--dry-run`, `--html-fixtures`
- [x] `output/enriched.csv` + `output/summary.md` + `output/run_stats.json`
- [x] Demo input + saved HTML fixtures + mock LLM fixtures
- [x] End-to-end tests (mock mode), failure-path tests

## Phase 7 - CI
- [x] GitHub Actions: ruff + pytest on 3.11-3.13, offline

## Phase 8 - Docs
- [x] README.md (Mermaid architecture, quickstart, config, limitations, compliance)
- [x] CASE_STUDY.md (problem, how, result, before/after table)
- [x] Optional n8n workflow

## Phase 9 - Definition of done
- [x] Mock-mode demo run end to end
- [ ] Real-mode demo run (needs `ANTHROPIC_API_KEY`; blocked in the build environment)
- [ ] CI green
- [x] No secrets in history (checked `git log -p` for key patterns; only `.env.example` tracked)
- [ ] PR opened with summary

## Current state (2026-10-09)

**Done**
- Full pipeline: polite fetching, parsing + redaction, Claude extraction with structured
  outputs, per-field validation with flags, YAML scoring, CSV/Markdown/JSON outputs, CLI with
  `--dry-run`, `--html-fixtures`, `--limit`, `--no-cache`, `MOCK_LLM=1`.
- 92 offline tests passing locally on Python 3.11, 3.12, 3.13; ruff clean.
- Mock-mode demo run end to end; output committed in `examples/demo-mock/`.
- README, CASE_STUDY, DECISIONS, BUILD_LOG, n8n workflow.

**Not done**
- Real-mode run (real Claude API). The build environment has no API key and blocks outbound
  web traffic, so real latency, tokens and cost are not measured. CASE_STUDY marks them pending.
- n8n workflow not yet imported into a live n8n instance.

**Next steps**
1. `cp .env.example .env`, add `ANTHROPIC_API_KEY`, run
   `enrich run input/companies.csv --html-fixtures fixtures/sites`; copy the measured numbers
   from `output/run_stats.json` into CASE_STUDY.md and BUILD_LOG.md, and compare the real
   extraction against the hand-written mock answers.
2. Run live on a small list of real public business websites you are allowed to crawl, and
   check 20 rows by hand for field accuracy.
3. Time the manual process on ~10 companies to replace the labeled 6-minute estimate.
4. Import and test the n8n workflow.
