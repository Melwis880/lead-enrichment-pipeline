# Build log

Running notes for turning this build into posts. Factual only; all numbers are labeled with
where they came from.

## Environment notes
- Build ran in a sandboxed cloud container: outbound HTTP to arbitrary websites is blocked
  (proxy returns `403` on `CONNECT`), and no `ANTHROPIC_API_KEY` is configured. So everything was
  built and tested against saved HTML fixtures and mock LLM responses. On 2026-10-10 the
  real-mode run was skipped by decision (no API key will be available): Claude was never called,
  and AI cost/latency/quality figures are estimates or unmeasured.

## Key decisions (see DECISIONS.md for the full list)
- Python with a small, readable dependency set instead of a crawling framework.
- Invented demo companies on `.example` domains so the demo is reproducible and cannot
  misrepresent a real business.

## Problems hit and how they were solved
- **`pytest` imported nothing.** `ModuleNotFoundError: No module named 'lead_enrich'` right after
  `pip install -e .`. The `pytest` on PATH was a separate uv tool environment
  (`#!/root/.local/share/uv/tools/pytest/bin/python`). Fix: always run `python -m pytest`.
- **Credentials error is a `TypeError`, not an API error.** With no key, the Anthropic SDK
  raises `TypeError: Could not resolve authentication method...` on the first request. Caught
  and turned into one clear message ("Set ANTHROPIC_API_KEY ... or run with MOCK_LLM=1") that
  aborts the run instead of failing 13 companies one by one.
- **Email redaction is not name redaction.** The end-to-end test asserting no personal data
  reaches the LLM failed: `'priya' is contained here: write to priya at [personal email
  removed]`. The email was gone, the first name in the sentence was not. Decided not to add NER
  (see DECISIONS.md); documented as a limitation and kept the output-side test.
- **Spreadsheet formula injection.** Score reasons were first written as `+30 size 2-10`. A cell
  starting with `+` is a formula in Excel. Reworded to `size 2-10 (+30)` and added a generic
  `'` prefix for any cell starting with `= + - @`.

## Security review (2026-10-10)
An external audit of the PR listed 6 must-fix and 5 small items. All 11 are fixed, one commit
each (or two for the larger ones), and each reproducible issue has a test that fails on the
previous code (checked by running the new tests against the old source; the SSRF guard did not
exist before, so its tests fail at import there).

**What was found and fixed**
- **Batch could hang or crash on one site.** `resp.apparent_encoding` after streaming re-read
  the rest of the response: a test server streaming without end held the old code for 37 s
  (`assert (195.62 - 158.76) < 3` failed). `charset=utf8mb4` raised `LookupError`; a
  `Retry-After: ²` raised `ValueError`; a slow-drip server was only bounded by the per-read
  timeout. Now: `read1` loop with byte cap and overall deadline, safe decoding, strict
  `Retry-After`, per-company `internal_error`, partial outputs on Ctrl-C (exit 130).
- **SSRF.** Redirects and DNS could point the fetcher at `169.254.169.254` or an intranet host.
  Now every request is preceded by a resolve-and-check (all addresses must be public; IPv6 forms
  embedding IPv4 are unwrapped, because `ipaddress` calls `64:ff9b::7f00:1` "global").
- **Slack/Markdown injection.** A `Content-Type` header or an LLM field like the city could put
  `<!channel>` or a disguised link into the Slack post. Every summary field is now escaped and
  errors only carry a validated media type.
- **robots.txt matching depended on the Python version.** On 3.11, `Allow: /` followed by
  `Disallow: /contact` allowed `/contact`, and `Disallow: /*.html$` blocked nothing. CPython
  3.13.16's `robotparser` already does longest match, so the new tests *passed* on the old code
  under 3.13 and failed under 3.11. Switched to protego.
- **Personal emails at rest.** Redaction happened before the LLM, but the fetcher cached the raw
  HTML for a week. Now only the parsed, redacted page is cached, in `0700` folders.
- **Redaction gaps.** `sales+jane.smith@` counted as the role box `sales`; `jane@müller.de` and
  `jane [at] shop [dot] example` were not recognised. All fixed; patterns checked for
  catastrophic backtracking on 100k+ character inputs (worst case 0.04 s).
- **Small items.** `.env` loader allowlist; `off_domain_email`; `insecure_transport` column;
  `persist-credentials: false` and dependency upper bounds; README note that keyword points are
  self-reported by the site.

**Found while fixing**
- The `insecure_transport` test showed that a site with HTTPS completely down was recorded as
  `skipped_robots`: the robots.txt fetch failed first, so the HTTP fallback never ran.
- Re-reading my own diff: the SSRF check inside the retry loop could raise past `fetch()` if DNS
  changed between two checks, turning a clean "blocked" into `internal_error`. Fixed with a test.

**Left out, and why**
- **DNS rebinding** (check-then-connect window): closing it needs a custom transport adapter that
  connects to the checked IP while keeping TLS verification on the hostname. Documented in the
  README; recommended next step before running on a network with sensitive internal services.
- **Slow response headers:** the deadline covers the body. Connect and header reading are bounded
  per read by `timeout_seconds`, not overall, so a server dripping headers can still hold one
  request longer than the deadline.
- **Names in free text** remain (unchanged decision, see DECISIONS.md).
- **LLM cache** stores the model's raw JSON answer, before validation. If a personal address ever
  slipped past redaction and the model repeated it, it would be rejected in the output but kept
  in `cache/llm`. Delete `cache/` to purge.
- **SHA-pinning GitHub Actions and a lockfile:** out of scope by request.

## Measured numbers (demo data)
All from the mock demo: 13 invented companies, saved HTML, hand-written mock LLM answers.
Command: `MOCK_LLM=1 enrich run input/companies.csv --html-fixtures fixtures/sites`.
- Pipeline compute for 13 companies: 0.09 s (3 runs, same result each time).
- HTTP requests: 47 (incl. robots.txt and 2 retries on the 503 site); 1 site blocked by robots.
- Politeness delay a live run would add: 70.9 s (rate limiter, simulated) = ~5.5 s per company.
- Outcome: 11 enriched (A 5, B 3, C 2, review 1), 1 skipped by robots.txt, 1 fetch failed.
- Validation caught 4 of 4 planted bad fields; 1 personal email redacted before the LLM step.
- Demo page text sent to the LLM: 249-1,608 characters per company, average 1,108.
- Dry-run cost ceiling (estimate, not measured): <= $0.62 per 100 companies at full 12,000-char
  input, ~450 output tokens, $1 / $5 per million tokens.
- Real LLM run: **not run** (skipped by decision; no API key available). No measured AI
  tokens, cost, latency or extraction accuracy exist for this project.
- Test suite: 178 tests after the security fixes (92 before), offline, passing on Python 3.11,
  3.12 and 3.13.

## Post hooks
- "The AI part (prompt, API call, error handling) is 183 of ~1,800 lines, about 10%. Politeness,
  validation and failure reporting are the rest."
- "My own end-to-end test caught me: I redacted every personal email, and the person's first name
  was still sitting in the sentence next to it."
- "If the model says a business email is info@company.com, check that it is actually on the page.
  A plausible invented address is the most expensive kind of error in sales data."
- "A lead score nobody can explain is a lead score nobody trusts. Every point here comes with a
  reason in plain text."
- "robots.txt that returns 503 means 'stay away', not 'anything goes' (RFC 9309)."
- "Same code, same robots.txt, different answer: Python 3.11's robotparser allowed a page that
  3.13's blocked. My new tests passed on 3.13 and only failed when I re-ran them on 3.11."
- "My scraper redacted personal emails before sending text to the AI, and then cached the raw
  HTML on disk for a week. Data minimisation has to cover storage, not just the API call."

## Suggested screenshots / GIFs
- Terminal GIF of `MOCK_LLM=1 enrich run input/companies.csv --html-fixtures fixtures/sites`
  showing the per-company progress lines with tiers and flags.
- `enrich run input/companies.csv --dry-run` output (plan + cost ceiling, nothing fetched).
- `output/enriched.csv` opened in Google Sheets/Excel, columns `tier`, `score`,
  `score_reasons`, `flags` visible, sorted A first.
- The "Needs attention" section of `output/summary.md` rendered on GitHub
  (`examples/demo-mock/summary.md`).
- `config/icp.yaml` next to one row's `score_reasons` to show how a rule becomes a reason.
- GitHub Actions run page with the job summary showing the demo `summary.md`.
- The Mermaid architecture diagram in README.md as rendered on GitHub.
