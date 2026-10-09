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
