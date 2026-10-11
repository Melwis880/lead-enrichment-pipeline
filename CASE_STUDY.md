# Case study: company lead enrichment for a small B2B sales team

## What problem does it solve?

A small agency sells bookkeeping software to local businesses. Its lead list is a spreadsheet
of company names and websites. Today a salesperson opens each website, clicks around to guess
the industry and size, looks for a contact address, and types notes into the sheet before
deciding whether to call. The work is slow and repetitive, and two people looking at the same
site often record different things. Nobody can say afterwards why one lead was ranked above
another.

## How does it work?

One command (`enrich run companies.csv`) does what the salesperson did by hand, in a fixed and
documented way:

1. **Fetch politely.** For each company, read robots.txt first, then fetch at most three public
   pages: the homepage, an about page and a contact page. The tool identifies itself, waits at
   least 2 seconds between requests to the same site, retries only on temporary errors, and
   caches the parsed, redacted pages for three days (never the raw HTML). Team and careers pages
   are skipped on purpose.
2. **Strip personal data before AI sees it.** Any email address that is not a role mailbox
   (`info@`, `hello@` ...) is replaced with `[personal email removed]`.
3. **Extract with Claude.** The page text goes to Claude (default: Haiku 4.5) with a strict JSON
   schema: industry, short description, size signal with evidence, location, products/services,
   business contact channel, and a confidence for each field.
4. **Check the answer instead of trusting it.** Each field is validated on its own. Fields with
   low confidence are left blank and flagged. A contact email is kept only if it is a role
   mailbox *and* literally appears on the pages; a contact form URL only if we found a form on
   that page.
5. **Score with rules the team can read.** A YAML file gives points for industry, size, location,
   a verified contact channel and keywords such as "invoicing" or "wholesale". Every point is
   written next to the lead: `size 2-10 (+30); in target area (Bristol) (+25)`.
6. **Hand over a list.** `enriched.csv` (best leads first), a one-page `summary.md` with the top
   10 and everything that needs a human look, and `run_stats.json` with time and cost.

Architecture diagram and configuration: [README.md](README.md). Design trade-offs:
[DECISIONS.md](DECISIONS.md).

## What is the concrete result in a real business?

**There is no client deployment yet, so there are no client results to report.** What exists is
a working pipeline and a reproducible demo on 13 invented local businesses (saved web pages, mock
AI answers written by hand). **Real mode was not run:** no API key was available, so Claude was
never called, and all AI cost, latency and quality figures below are estimates or untested. On
that demo:

- 11 of 13 companies were enriched and ranked (5 A, 3 B, 2 C, 1 "review"). One site was skipped
  because its robots.txt forbids crawling, one failed because the server returned 503 on every
  retry. Both appear in the report with the reason, rather than disappearing.
- The validation layer caught all 4 bad fields planted in the mock answers: a personal email
  address, a plausible `info@` address that was not on the site, a size guess with weak evidence,
  and a low-confidence industry. All four were blanked and flagged, not passed on.
- An accountancy firm (a likely competitor or partner, not a buyer) ranked C despite being local
  and small, and the CSV says why: `industry accounting_and_finance (-40)`.
- These results test the pipeline around the model (fetching, validation, scoring, reporting).
  They say nothing about how accurately Claude extracts industry, size or location from real
  websites: that has not been measured.

What a pilot with a real team should measure: minutes saved per 100 leads (time-tracked, both
ways), field accuracy on a 20-company sample checked by hand, and whether tier A leads convert
better than tier C after a month.

## Before / after (per 100 companies)

| | By hand | Pipeline |
|---|---|---|
| Time per company | ~6 min (**estimate**, see assumption below) | 0.007 s compute + ~5.5 s polite waiting (**measured, demo data**: mock run on saved pages) + LLM latency (**not measured**: real mode not run; ~2 s assumed) |
| Time per 100 companies | ~10 hours (**estimate**) | ~15-20 minutes unattended (**estimate**: about 4 requests per company at >= 2 s each, from the demo data, plus an assumed ~2 s LLM call) |
| AI cost per 100 companies | none | **Not measured** (real mode not run). **Estimate**: <= $0.62, from the dry-run formula (full 12,000-character input at ~4 characters per token, ~1,000 prompt tokens, ~450 output tokens, $1/$5 per million tokens) |
| Extraction quality | Depends on who looks | **Not measured** (real mode not run). Mock answers only show that bad fields are caught and flagged |
| Consistency | Depends on who looks | Same rules every time; every score has written reasons |
| Personal data | Whatever the person copies | Personal emails removed before AI; only role mailboxes or form URLs in output |

**Assumption behind the manual estimate (not measured):** open the homepage, find the about and
contact pages, judge industry and size, copy a contact channel, type about 6 fields into a
spreadsheet: about 6 minutes per company for someone practised. Replace this with a timed sample
from the actual team before quoting it.

**What was measured on the demo run (demo data)** (`MOCK_LLM=1 enrich run input/companies.csv
--html-fixtures fixtures/sites`, 13 companies, see `examples/demo-mock/run_stats.json`):

- Pipeline compute (fetch from saved files, parse, validate, score, write outputs): 0.09 s total.
- 47 HTTP requests including robots.txt and 2 retries; 1 site blocked by robots.txt.
- Politeness delay a live run would have spent waiting: 70.9 s (computed by the rate limiter,
  not slept, because the pages were local files). That is about 5.5 s per company.
- AI tokens and cost: 0 (mock mode). Real mode was not run, so there are no measured AI numbers.

**How to measure them later, if a key becomes available:** put an API key in `.env` and run
`enrich run input/companies.csv --html-fixtures fixtures/sites`. That sends the same 13 saved
demo sites to Claude and writes measured tokens, latency and cost to `output/run_stats.json`.
Note that the demo pages are short (about 1,100 characters per company on average), so real
websites will cost more per company than this run; a live run on a real list gives the
representative number.

## Lessons

- The model is the easy part. The AI module is about 7% of the code (`extractors.py` is 170 of
  2,544 lines in `src/`); the rest is politeness, validation and making failures visible.
- Asking the model for a confidence per field is only useful if you act on it. Here, anything
  under 0.6 is blanked and flagged.
- "Verify against the source" is cheap and catches the most harmful errors: invented contact
  addresses.
- Removing email addresses is not the same as removing personal data; names in running text
  remain. That gap is documented rather than papered over.
