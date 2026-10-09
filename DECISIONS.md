# Decisions

One entry per architecture or security decision, written when the decision is made.

## 2026-10-09 - Python + small dependency set
- **Decision:** Python 3.11+, `requests`, `beautifulsoup4`, `pydantic` v2, `PyYAML`, `click`,
  official `anthropic` SDK. No crawling framework.
- **Alternatives:** Scrapy (too heavy for 3 pages per company, hides the politeness logic we want
  to show), `httpx` async (concurrency not needed at this volume, adds complexity).
- **Why:** Every politeness rule (robots, delay, retries, cache) is visible in ~200 lines a
  reviewer can read. The target volume (tens to hundreds of companies) does not need async.

## 2026-10-09 - Demo list: invented companies on `.example` domains
- **Decision:** `input/companies.csv` lists 12 invented Bristol-area businesses on `*.example`
  domains (reserved by RFC 2606, cannot resolve). Their websites are saved HTML fixtures.
- **Alternatives:** A real list of local businesses (needs live network, which this build
  environment does not have; real sites change, so the demo would not be reproducible; risk of
  misrepresenting a real business in a public portfolio).
- **Why:** Reproducible, offline, zero chance of hitting or misdescribing a real company. Real
  sites are supported by the same code path; only the transport differs.

## 2026-10-09 - robots.txt: RFC 9309 status semantics, never configurable off
- **Decision:** 2xx -> obey rules; 4xx (incl. 401/403) -> allowed; 5xx or network error ->
  treat the whole site as disallowed. Robots checking has no off switch in config.
- **Alternatives:** `urllib.robotparser.read()` defaults (treats 401/403 as disallow-all and
  swallows network errors); a `respect_robots: false` flag for testing.
- **Why:** RFC 9309 is the current standard, and "unreachable means stay away" is the safe
  default. A disable flag in a public repo invites misuse; tests use fixtures instead.

## 2026-10-09 - Fetcher follows redirects itself
- **Decision:** The transport never auto-follows redirects; the fetcher follows up to 5 hops and
  checks robots.txt + rate limit for every hop's origin.
- **Alternatives:** `requests` default redirect following.
- **Why:** `example.com -> www.example.com` is common, and the target host can have different
  robots rules. Auto-follow would fetch pages without checking them.

## 2026-10-09 - Politeness defaults
- **Decision:** Sequential processing, >= 2 s between requests to the same host (robots
  `Crawl-delay` wins if larger; sites asking for > 60 s are skipped), 10 s timeout, 2 retries
  with exponential backoff only for timeouts / 429 / 5xx, `Retry-After` honoured (capped 60 s),
  max 3 pages per company, 2 MB per page, one-week disk cache.
- **Alternatives:** Concurrent fetching across domains (faster, but harder to reason about and
  not needed for tens-hundreds of companies); no cache (re-hits sites on every re-run).
- **Why:** A small sales team re-runs the list often while tuning scoring; the cache means a
  re-run costs the target sites nothing.

## 2026-10-09 - Personal data minimisation happens before the LLM call
- **Decision:** Only role-based mailboxes (`info@`, `hello@`, `contact@` ... fixed allowlist) are
  kept. Every other email address is replaced with `[personal email removed]` in the page text
  before it is sent to the model. Team / staff / careers pages are never fetched.
- **Alternatives:** Send raw text and tell the model to ignore personal data; filter only the
  model's output.
- **Why:** Data minimisation (GDPR Art. 5(1)(c)): personal data we never send cannot leak into
  the output, logs, or a third-party processor. The output-side check stays as a second layer.

## 2026-10-09 - Two layers of output control: structured outputs + per-field validation
- **Decision:** The request uses Claude structured outputs (`output_config.format` with a strict
  JSON schema), and the answer is then validated field by field in pydantic/plain Python. A bad
  field is flagged and blanked; the rest of the record is kept. Fields below
  `validation.min_confidence` (default 0.6) are flagged and blanked too ("flagged, not guessed").
- **Alternatives:** `messages.parse()` into one pydantic model (one bad field fails the whole
  company); tool use with a forced tool (same guarantee, more plumbing); free-text JSON in the
  prompt (no schema guarantee).
- **Why:** The schema guarantees shape; it cannot guarantee truth or policy. Per-field checks keep
  partial results useful, and the flag tells the salesperson exactly what to double-check.

## 2026-10-09 - Contact channels must be verifiable in the fetched pages
- **Decision:** `contact_email` is accepted only if it is role-based (allowlist) and literally
  appears in the fetched pages; `contact_form_url` only if it is on the company's own site and we
  detected a form with an email field or textarea on that exact page.
- **Alternatives:** Trust the model's answer; pattern-guess `info@domain`.
- **Why:** Contact data is what a salesperson acts on. A hallucinated or guessed address is worse
  than a blank, and a guessed personal address would be a GDPR problem.

## 2026-10-09 - Default model and prompt-injection stance
- **Decision:** Default `claude-haiku-4-5-20251001` (per brief), overridable with `ENRICH_MODEL`
  or `llm.model`. Website text is wrapped in `<pages>` and the system prompt declares it
  untrusted data. No tools are given to the model.
- **Alternatives:** A larger model by default (higher cost for a simple extraction task).
- **Why:** Extraction from 3 short pages is a small task. A hostile page can at most distort its
  own record, and that record still passes the schema and evidence checks.

## 2026-10-09 - Error policy: fail the company, not the run (except misconfiguration)
- **Decision:** Network errors, HTTP errors, refusals, truncated or invalid output mark that one
  company as failed with a reason. Auth errors and unknown model abort the whole run immediately.
- **Alternatives:** Abort on any error; silently skip.
- **Why:** One broken website should not cost the other 99 results, but a bad API key would fail
  100 times and waste the user's time.

## 2026-10-09 - LLM cache + hand-written mock fixtures
- **Decision:** Real LLM answers are cached on disk keyed by model + prompt version + system
  prompt + schema + page text. `MOCK_LLM=1` reads `fixtures/llm/<domain>.json`, which were
  written by hand for the invented demo companies and are labeled as such.
- **Alternatives:** Record real responses as fixtures (impossible here: no API key in the build
  environment); a heuristic fake extractor.
- **Why:** Tests and CI stay offline and deterministic. Hand-written fixtures are honest about
  what they are, and they deliberately include bad fields to exercise the validation layer.
