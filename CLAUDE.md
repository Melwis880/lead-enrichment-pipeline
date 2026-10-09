# Case Study 2: Company Lead Enrichment Pipeline

## Goal
Build a pipeline that takes a raw list of company websites and returns an enriched, scored lead
list a small sales team can act on. This repo is a public portfolio case study for an AI
automation consultant.

## Demo scenario
A small B2B agency selling bookkeeping software to local businesses. Today a salesperson opens
each website by hand, guesses the industry and size, and types notes into a spreadsheet.

## MVP scope
In scope:
- Input: `input/companies.csv` with company name + website domain (use a small, real, public list
  of businesses or invented ones; document which).
- Fetch each company's public homepage / about / contact page politely: respect `robots.txt`,
  set a clear User-Agent, rate-limit, timeouts, retries, cache responses on disk.
- Extract with the Claude API (default `claude-haiku-4-5-20251001`, configurable) into a strict
  JSON schema: industry, short description, approximate size signal, location, products/services,
  public business contact channel (generic info@ / contact form URL only), confidence per field.
- Validate output with a schema (pydantic); bad or low-confidence fields are flagged, not guessed.
- Scoring: a transparent, rule-based fit score (configurable YAML) against an ideal customer
  profile, with the reason for each score.
- Output: `output/enriched.csv` + a short Markdown summary report.
- CLI: `enrich run input/companies.csv`, plus `--dry-run` and `MOCK_LLM=1` / cached-fixture mode.

Out of scope: personal data of individuals (no scraping or guessing personal emails, no LinkedIn
scraping), paid data APIs, CRM sync, outreach.

## Constraints
- Compliance first: only public business information, no personal data. State this in the README
  (GDPR-aware design). Respect site terms.
- Never commit secrets; `.env.example` only.
- Deterministic tests: use saved HTML fixtures, no live network in tests.

## Deliverables
1. Code + pytest suite (parsing, robots handling, schema validation, scoring, failure paths).
   Tests pass offline.
2. GitHub Actions CI.
3. English `README.md` with architecture (Mermaid), quickstart, config, limitations, compliance note.
4. English `CASE_STUDY.md` answering: What problem does it solve? How does it work? What is the
   concrete result in a real business? Before/after table: time per company by hand (labeled
   estimate with assumption) vs. measured pipeline time and cost per 100 companies on the demo run.
   Never invent client results.
5. Optional `n8n/` workflow JSON that runs the CLI on a schedule and posts the summary.

## Project tracking (required)
- `PROGRESS.md`: split the work into phases with checkbox tasks. Create it first, re-read it at
  the start of each work chunk, tick tasks as they finish, and leave an accurate final state
  (done / not done / next steps) at the end.
- `DECISIONS.md`: one short entry per architecture or security decision: date, decision,
  alternatives considered, why. Write it when the decision is made, not afterwards.
- Commit both with the code.

## Build log for content (required)
Keep `BUILD_LOG.md` updated as you work, so the member can turn the build into posts later:
- Key decisions and why (including options you rejected).
- Problems hit and how you solved them (real errors, short snippets).
- Measured numbers from demo runs (time, cost, accuracy), labeled as demo data.
- 3-5 "post hooks": one-sentence lessons or surprising findings worth sharing.
- Suggested screenshots/GIFs to capture (exact command or page to show).
Commit it with the code; keep it factual, no invented results.

## Definition of done
`enrich run` works end to end on the demo list (mock mode and real mode), CI green, docs complete,
BUILD_LOG.md, PROGRESS.md and DECISIONS.md up to date, no secrets in history, PR opened with a summary.
