# Progress

Status legend: `[x]` done, `[ ]` not done. Re-read at the start of each work chunk.

## Phase 0 - Tracking
- [x] PROGRESS.md, DECISIONS.md, BUILD_LOG.md created

## Phase 1 - Scaffold
- [x] `pyproject.toml`, `src/lead_enrich/` package, `enrich` console script
- [ ] `.env.example`, `config/settings.yaml`, `config/icp.yaml`

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
- [ ] Strict JSON schema for the LLM, pydantic validation with per-field flags
- [ ] Evidence check: contact channels must appear in fetched pages
- [ ] Claude extractor (structured outputs, usage + cost tracking, error handling)
- [ ] Mock extractor (`MOCK_LLM=1`) + LLM response cache
- [ ] Tests

## Phase 5 - Scoring
- [ ] YAML ICP config, rule engine with reason per rule, tiers
- [ ] Tests

## Phase 6 - Pipeline + CLI + output
- [ ] `enrich run input/companies.csv`, `--dry-run`, `--html-fixtures`
- [ ] `output/enriched.csv` + `output/summary.md` + `output/run_stats.json`
- [ ] Demo input + saved HTML fixtures + mock LLM fixtures
- [ ] End-to-end tests (mock mode), failure-path tests

## Phase 7 - CI
- [ ] GitHub Actions: ruff + pytest on 3.11-3.13, offline

## Phase 8 - Docs
- [ ] README.md (Mermaid architecture, quickstart, config, limitations, compliance)
- [ ] CASE_STUDY.md (problem, how, result, before/after table)
- [ ] Optional n8n workflow

## Phase 9 - Definition of done
- [ ] Mock-mode demo run end to end
- [ ] Real-mode demo run (needs `ANTHROPIC_API_KEY`)
- [ ] CI green
- [ ] No secrets in history
- [ ] PR opened with summary
