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
- [ ] Real-mode demo run: **not done, skipped by decision (2026-10-10)**. No API key will be
      available; cost and quality figures are estimates (see README Limitations, CASE_STUDY)
- [x] CI green (GitHub Actions on PR #1: Python 3.11, 3.12, 3.13)
- [x] No secrets in history (checked `git log -p` for key patterns; only `.env.example` tracked)
- [x] PR opened with summary; marked ready for review on 2026-10-10 without the real-mode run

## Phase 10 - Security audit fixes (2026-10-10)
- [x] 1. Robustness: body deadline + byte cap, safe decoding, strict Retry-After, per-company
      `internal_error`, partial outputs on interrupt; tests for utf8mb4, missing Content-Type,
      non-ASCII-digit Retry-After, oversized body, slow drip
- [x] 2. SSRF: reject non-public addresses on every hop; reject IP-literal input; tests; document
      DNS-rebinding gap
- [x] 3. Slack/Markdown injection: validated media type in errors; escape all summary fields; tests
- [x] 4. robots.txt via protego (longest match, `*`, `$`); tests
- [x] 5. No raw HTML at rest: cache parsed, redacted pages only; cache dirs 0700; README matches
- [x] 6. Redaction gaps: plus-addressing, internationalised domains, obfuscated forms; tests
- [x] 7. `.env` loader allowlist
- [x] 8. `off_domain_email` flag
- [x] 9. `insecure_transport` column; no contact channels from HTTP pages
- [x] 10. CI `persist-credentials: false`; dependency upper bounds
- [x] 11. README: keyword points are self-reported by the site
- [x] BUILD_LOG security review section, DECISIONS entries, PR description updated

## Phase 11 - Optimization pass (2026-10-10, see OPTIMIZATIONS.md)
- [x] F1 Case-insensitive response headers; test with a real local server
- [x] F3 Charset from the page's `<meta>` when the header has none (no Latin-1 default)
- [x] F2 (partial) Separate 5 s connect timeout; no retry after connect timeout or blown
      deadline; HTTP fallback only after TLS error or refused connection; DECISIONS entry
- [ ] F2 (rest) Per-company time budget: not requested in this pass
- [x] F6 Page cache 72 h, robots.txt 24 h (weekly run no longer reuses last week's pages)
- [x] F9 Average fetch time and 3 slowest companies in `run_stats.json` and the summary
- [x] LLM cache stores only the validated result; personal emails removed from free-text
      fields (open item from the security review)
- [x] Per-model price table; unknown model reported as "unknown price" with a warning;
      alias `claude-haiku-4-5` priced like the dated ID
- [x] Before/after measurements; OPTIMIZATIONS.md "Applied"; BUILD_LOG section
- [ ] F4 concurrency, F5 model change, F7 input budget, F8 cache size/prune, F10 validator
      reuse: deferred by request (reasons in OPTIMIZATIONS.md "Applied")

## Phase 12 - Security review of the optimization pass (2026-10-10)
- [x] Charset allowlist: `punycode`, `utf-7`, `unicode_escape` and other non-web or unknown
      charsets decode as UTF-8 (80k-character punycode body: 2.4 s -> 0.002 s)
- [x] No HTTP fallback after a certificate verification failure (new kind `tls_cert`)
- [x] LLM cache: 30-day TTL; each run prunes expired entries and raw answers from older versions
- [x] LLM cache key includes PARSER_VERSION; an entry that no longer fits the schema is a miss
- [x] Fetch/LLM timeouts and cache TTLs must be > 0; robots.txt TTL at most 24 h
- [x] README: charset rule, certificate rule, cache expiry, phone numbers in free text, cached
      pages used without a new robots.txt check
- [ ] Lockfile / hashed runtime dependencies, DNS rebinding: still out of scope

## Phase 13 - Full re-audit before merge (2026-10-10)
- [x] HTTP session ignores the environment (`trust_env = False`): no `~/.netrc` credentials sent
- [x] One canonical URL form (requests' prepared URL) for the SSRF check, same-site test, robots
      origin and request: closes the `\@` host trick and the IDNA 2003 vs UTS 46 mismatch
- [x] `requests>=2.32.4`, `urllib3>=2.6.3` (CVE-2024-47081, CVE-2025-66471, CVE-2025-66418)
- [x] Control characters replaced in progress output; hosts quoted in errors
- [x] Site text cannot open or close `<page>` blocks in the LLM document
- [x] Fixture transport cannot read files outside its folder
- [ ] DNS rebinding (connect to the checked IP), SHA-pinned Actions, lockfile: still out of scope

## Current state (2026-10-10)

**Done**
- Full pipeline: polite fetching, parsing + redaction, Claude extraction with structured
  outputs, per-field validation with flags, YAML scoring, CSV/Markdown/JSON outputs, CLI with
  `--dry-run`, `--html-fixtures`, `--limit`, `--no-cache`, `MOCK_LLM=1`.
- Security audit (2026-10-10): all 11 items fixed (robustness, SSRF guard, Slack/Markdown
  escaping, protego robots, no raw HTML at rest, redaction gaps, `.env` allowlist,
  `off_domain_email`, `insecure_transport`, CI/dependency hardening, README note).
- Optimization pass (2026-10-10): lowercase headers, missing charset, dead/slow-site retries
  and HTTP fallback, cache lifetimes, timing report, validated-only LLM cache, per-model price
  table. Dead host 33 s -> 5 s and slow-drip host 63 s -> 20 s on local sockets.
- Security review of the optimization pass (2026-10-10): charset allowlist, no HTTP fallback
  after a certificate failure, LLM cache TTL and startup pruning, parser version in the LLM
  cache key, positive timeouts.
- Full re-audit before merge (2026-10-10): netrc leak, SSRF parser mismatch, CVE minimums,
  terminal escapes, page-tag injection, fixture path traversal fixed.
- 252 offline tests passing locally on Python 3.13; ruff 0.17.0 clean.
- Mock-mode demo run end to end; output committed in `examples/demo-mock/`.
- README, CASE_STUDY, DECISIONS, BUILD_LOG, n8n workflow.

**Not done**
- Real-mode run (real Claude API): **skipped by decision** on 2026-10-10 because no API key will
  be available. Claude was never called; AI latency, tokens and cost are estimates, and
  extraction quality is not measured. README Limitations and CASE_STUDY say so.
- n8n workflow not yet imported into a live n8n instance.

**Next steps**
1. Only if an API key becomes available: `cp .env.example .env`, add `ANTHROPIC_API_KEY`, run
   `enrich run input/companies.csv --html-fixtures fixtures/sites`; copy the measured numbers
   from `output/run_stats.json` into CASE_STUDY.md and BUILD_LOG.md, and compare the real
   extraction against the hand-written mock answers.
2. Run live on a small list of real public business websites you are allowed to crawl, and
   check 20 rows by hand for field accuracy.
3. Time the manual process on ~10 companies to replace the labeled 6-minute estimate.
4. Import and test the n8n workflow.
5. Close the DNS-rebinding window (connect to the checked IP) before running on a network with
   sensitive internal services; consider an overall deadline for the header phase too.
6. Out of scope for now, by request: SHA-pinning GitHub Actions, a lockfile.
7. Deferred optimizations (OPTIMIZATIONS.md): per-company time budget (F2 rest), cross-domain
   concurrency only if lists grow past a few hundred (F4), evaluate `claude-haiku-5-5` on the
   20-company hand-checked sample, owner's decision (F5), LLM input budget (F7), cache size cap
   and prune command (F8).
