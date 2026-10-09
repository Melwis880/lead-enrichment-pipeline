# Build log

Running notes for turning this build into posts. Factual only; all numbers are labeled with
where they came from.

## Environment notes
- Build ran in a sandboxed cloud container: outbound HTTP to arbitrary websites is blocked
  (proxy returns `403` on `CONNECT`), and no `ANTHROPIC_API_KEY` is configured. So everything was
  built and tested against saved HTML fixtures and mock LLM responses. Real-mode numbers are
  marked "pending" until someone runs it with a key.

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
- Real LLM run: **pending** (no API key in the build environment).
- Test suite: 92 tests, offline, passing on Python 3.11, 3.12 and 3.13 locally.

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
