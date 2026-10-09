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

## Post hooks

## Suggested screenshots / GIFs
