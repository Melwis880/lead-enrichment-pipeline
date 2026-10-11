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
  `pip install -e .`. The `pytest` on PATH was a separate uv tool environment (its shebang
  pointed at a different Python), not the project's. Fix: always run `python -m pytest`.
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
  in `cache/llm`. Delete `cache/` to purge. *(Fixed in the optimization pass below.)*
- **SHA-pinning GitHub Actions and a lockfile:** out of scope by request.

## Optimization pass (2026-10-10)
An optimization audit (`OPTIMIZATIONS.md`) found little worth speeding up: CPU for the whole mock
run is 0.36 s. The real problems were in the live-network path, which the offline fixtures never
exercised (`FixtureTransport` always sends a correctly cased `Content-Type` with
`charset=utf-8`). Applied, one commit each, every fix with a test that fails on the old code:
- **Lowercase headers** (`content-type:`, `location:`): every page failed as
  `not HTML (missing)` and redirects were not followed. Headers are now case-insensitive.
- **No charset in the header** made requests decode UTF-8 pages as Latin-1 (`Café` -> `CafÃ©`).
  The charset now comes from the page's `<meta>` tag, else UTF-8.
- **Dead and slow sites:** connect timeouts and blown body deadlines were retried 3 times, and
  then the whole sequence was repeated over plain HTTP. Now: 5 s connect timeout, no retry for
  those two, and the HTTP fallback runs only after a TLS error or a refused connection.
- **Cache lifetimes:** pages 72 h (was 168 h, the same as the weekly n8n schedule, so a weekly
  run could reuse last week's pages and answers), robots.txt 24 h (RFC 9309).
- **Timing report:** `run_stats.json` and the summary now show average fetch time and the 3
  slowest companies.
- **LLM cache** stores only the validated result; validation also removes personal emails from
  free-text fields. **Cost report** uses a per-model price table; an unknown model is reported
  as "unknown price", never priced as Haiku 4.5.

**Problems hit**
- Making the retry tests fail on the old code *by assertion* (not just because a new keyword
  argument did not exist) needed a real dead host. A local socket with a full listen queue
  (`listen(0)` plus three unaccepted connects) gives a genuine `ConnectTimeout` on 127.0.0.1;
  the old code fails with `assert (3 == 1)` attempts and `assert 6 == 1` requests per company.
- My first "before" demo run came back with `Tiers: {}`: I had started it from a scratch folder,
  and `llm.mock_fixtures_dir` is relative. Re-ran from the repo root.

**Measured (local sockets and fake-clock simulation, not live sites)**

| Case, default settings | Before | After |
|---|---|---|
| One dead (blackholed) host, real sockets | 33.0 s, 3 attempts | 5.0 s, 1 attempt |
| One slow-drip host, real sockets | 63.1 s, 3 attempts | 20.0 s, 1 attempt |
| Healthy host / refused port, real sockets | 2.0 s / 4.0 s | 2.0 s / 4.0 s (unchanged) |
| Dead company, https + http (simulation) | 66.0 s, 6 requests | 5.0 s, 1 request |
| Slow-drip company, worst case (simulation) | 186.0 s | 30.0 s |
| Healthy company (simulation) | 6.4 s | 6.4 s |
| Header/charset cases handled (4 local cases) | 1 of 4 | 4 of 4 |
| Mock demo (demo data) | 11 ok, A5/B3/C2/review 1, 47 requests | identical `enriched.csv` and counts |
| Test suite | 178 tests | 205 tests, ~8-10 s |

**Post hooks**
- "My scraper passed every test and would have failed on real websites: the fixtures always sent
  `Content-Type`, the real world sometimes sends `content-type`."
- "A dead website cost my pipeline 66 seconds and a healthy one 6. Retrying a timeout three
  times, then again over HTTP, is not resilience, it is waiting."

**Screenshot:** `python -m pytest tests/test_transport.py -k "dead_host or deadline" -v`
next to the before/after table above.

## Security review of the optimization pass (2026-10-10)
A second, targeted audit of the optimization commits found one medium and three low issues; all
fixed, each with a test that fails on the previous code.
- **Charset as a weapon (medium).** Reading the charset from `<meta>` widened an old path: a page
  declaring `charset=punycode` is decoded with Python's punycode codec, which is quadratic. An
  80k-character body took 2.4 s; at the 2 MB byte cap it would be many minutes per page, and the
  download deadline does not cover decoding. Now only known web encodings are used; anything
  else decodes as UTF-8 (same body: 0.002 s).
- **Certificate failure fell back to HTTP (low).** requests reports an expired or untrusted
  certificate as the same `SSLError` as "no TLS on 443", so both triggered the plain-HTTP
  fallback. A failed certificate check is the typical sign of someone in the middle; it is now
  its own kind (`tls_cert`) and never falls back.
- **LLM cache retention (low).** Raw answers from before the validated-only change were never
  read again and never deleted, and validated entries had no TTL while free text can still hold
  names. The cache now expires after 30 days and every run prunes expired and outdated entries.
- **LLM cache key (low).** Added `PARSER_VERSION`, because validation uses the parser's
  redaction; an entry that no longer fits the schema is now a miss instead of a permanent
  `internal_error`.
- Also: timeouts and TTLs must be > 0 (a zero timeout made every company fail), robots.txt TTL
  is capped at 24 h, and README now names phone numbers in free text and cached pages being used
  without a new robots.txt check.

**Post hook**
- "The optimization that fixed garbled text also opened a door: if you trust the page to name
  its own charset, it can name one that takes minutes to decode."

## Full re-audit before merge (2026-10-10)
A third audit of the whole branch found two high issues that both earlier passes missed. Both
come from the same root: two parsers reading one URL differently.
- **`~/.netrc` credentials to an attacker (high).** `requests.Session` attaches saved netrc
  credentials, choosing them by `urlparse`'s host, while urllib3 connects to the host it parses.
  `http://attacker.example\@github.com/` is github.com to the first and attacker.example to the
  second. A local test with a dummy netrc showed `Authorization: Basic ...` arriving at the
  wrong server. Fix: `session.trust_env = False`.
- **SSRF guard bypass (high).** The guard checked the host with `urlsplit`; requests sent the URL
  to the host urllib3 parsed. A homepage link `http://169.254.169.254\@shop.example/../latest/`
  passed the same-site check and the guard as shop.example, then became a GET of the metadata
  address. A German `ß` in a hostname gave a second mismatch (IDNA 2003 for the DNS check, UTS 46
  for the connection). Fix: convert every URL once to requests' prepared form and use only that.
  Seven new tests fail on the old code.
- Also: dependency minimums raised past three CVEs, control characters replaced in terminal
  output, `<page>` tags escaped in the LLM document, fixture reads kept inside their folder.

**Post hook**
- "My SSRF guard and my HTTP library disagreed about which host was in the same URL. One
  backslash was enough to send a request to the cloud metadata address."

## Targeted audit of the re-audit fixes (2026-10-10)
The SSRF fix held: about 60,000 URL variants (userinfo, ports, IPv6 zones, percent-encoding,
IDNA, full-width characters) never made the checked host and the connected host differ. The
audit still found one medium and several low issues, two of them introduced by my own fixes:
- **`</pages>` (medium).** The tag escape matched `<page\b`, and "pages" has no word boundary
  after "page", so site text could close the outer wrapper. Now every `<` is escaped.
- **IDN regression (introduced by the canonical-URL fix).** Links became punycode while the CSV
  domain stayed Unicode, so `bäckerei-müller.example` found no subpages and every contact form
  looked off-site. Sites are now compared in punycode, with the same UTS 46 mapping requests
  uses (Python's `idna` codec maps `ß` to `ss`, a third mismatch).
- **Not idempotent.** One prepare turned `/x/%2e%2e/private` into `/x/../private`, and requests
  turned that into `/private` when sending: robots.txt checked one path, another was requested.
  URLs are now prepared until they stop changing.
- Also: `PARSER_VERSION` bump, sanitised CLI output everywhere (including U+202E), fixture
  symlink escape, `::/96` addresses, clean blocks for invalid ports and redirect targets.

**Post hook**
- "My security fix broke German bakeries: the links were now punycode, the input list was not."

## Public release prep (2026-10-11)
Before making the repo public, every claim in README.md and CASE_STUDY.md was checked against
the code and a fresh mock run.
- The quickstart commands worked exactly as written; the fresh `enriched.csv` was byte-identical
  to `examples/demo-mock/enriched.csv`.
- Drift found: the "AI part is 183 of ~1,800 lines" figure was stale after the audit passes
  (now 170 of 2,544 lines, about 7%), and three validation flags (`invalid_email`,
  `invalid_shape`, `invalid_confidence`) were missing from the README list.
- Re-measured from the mock run (demo data): page text sent to the LLM is 249-1,608 characters
  per company, average 1,108, matching the earlier figure.
- Two invented demo businesses had real-looking street addresses with full Bristol postcodes.
  They now use a street name and an `XX` postcode like the other fixtures (`Wapping Wharf,
  Bristol BS1 4XX`, `Kingsdown Parade, Bristol BS6 5XX`). `enriched.csv` and `summary.md` from
  the mock run are unchanged; average LLM input drops from 1,108 to 1,106 characters.
- History scan (all commits): no API keys, tokens or private keys; only `.env.example` was ever
  tracked; email addresses are on reserved `.example` domains or GitHub/Anthropic noreply
  addresses; phone numbers are in Ofcom's drama ranges or not diallable.

**Post hook**
- "Docs drift faster than code: two audit passes later, my own 'AI is 10% of the code' line was
  wrong. Re-measure every number before you publish."

## Measured numbers (demo data)
All from the mock demo: 13 invented companies, saved HTML, hand-written mock LLM answers.
Command: `MOCK_LLM=1 enrich run input/companies.csv --html-fixtures fixtures/sites`.
- Pipeline compute for 13 companies: 0.09 s (3 runs, same result each time).
- HTTP requests: 47 (incl. robots.txt and 2 retries on the 503 site); 1 site blocked by robots.
- Politeness delay a live run would add: 70.9 s (rate limiter, simulated) = ~5.5 s per company.
- Outcome: 11 enriched (A 5, B 3, C 2, review 1), 1 skipped by robots.txt, 1 fetch failed.
- Validation caught 4 of 4 planted bad fields; 1 personal email redacted before the LLM step.
- Demo page text sent to the LLM: 249-1,608 characters per company, average 1,106 (1,108 before
  the fixture addresses were made fictional on 2026-10-11).
- Dry-run cost ceiling (estimate, not measured): <= $0.62 per 100 companies at full 12,000-char
  input, ~450 output tokens, $1 / $5 per million tokens.
- Real LLM run: **not run** (skipped by decision; no API key available). No measured AI
  tokens, cost, latency or extraction accuracy exist for this project.
- Test suite: 178 tests after the security fixes (92 before), offline, passing on Python 3.11,
  3.12 and 3.13. After the later audit passes: 271 tests.

## Post hooks
- "The AI part (prompt, API call, error handling) is 170 of 2,544 lines, about 7%. Politeness,
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
- Done: `docs/demo-run.png` (terminal output of the mock demo run, shown at the top of README).
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
