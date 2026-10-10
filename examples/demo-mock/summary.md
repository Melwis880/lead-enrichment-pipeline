# Lead enrichment summary

- Run started: 2026-10-10T09:29:18+00:00 (UTC)
- Input: input/companies.csv (13 companies)
- Mode: mock LLM (saved fixtures) + HTML fixtures from fixtures/sites
- Model: none (mock)
- Ideal customer profile: Local small businesses near Bristol that need bookkeeping software

## Results

| Status | Companies |
|---|---|
| fetch_failed | 1 |
| ok | 11 |
| skipped_robots | 1 |

| Tier | Companies |
|---|---|
| A | 5 |
| B | 3 |
| C | 2 |
| review | 1 |

## Top leads

| # | Company | Tier | Score | Industry | Size | Location | Contact | Why |
|---|---|---|---|---|---|---|---|---|
| 1 | Avon Valley Plumbing &amp; Heating | A | 95 | construction_and_trades | 2-10 | Bristol, GB | https://avonvalleyplumbing.example/contact-us | industry construction_and_trades (+25); size 2-10 (+30); in target area (Bristol) (+25); verified business contact channel (+10); keywords: quotes (+5) |
| 2 | Harbourside Sourdough Co. | A | 95 | food_and_drink | 2-10 | Bristol, GB | hello@harbourside-sourdough.example | industry food_and_drink (+20); size 2-10 (+30); in target area (Bristol) (+25); verified business contact channel (+10); keywords: wholesale, trade account (+10) |
| 3 | Bath Stone Builders | A | 90 | construction_and_trades | 11-50 | Bath, GB | office@bathstonebuilders.example | industry construction_and_trades (+25); size 11-50 (+25); in target area (Bath) (+25); verified business contact channel (+10); keywords: quotes (+5) |
| 4 | Redcliffe Books &amp; Coffee | A | 85 | retail | 2-10 | Bristol, GB | none verified | industry retail (+20); size 2-10 (+30); in target area (Bristol) (+25); keywords: deliveries, online shop (+10) |
| 5 | Kingsdown Physio Clinic | A | 70 | health_and_care | 11-50 | Bristol, GB | reception@kingsdownphysio.example | industry health_and_care (+10); size 11-50 (+25); in target area (Bristol) (+25); verified business contact channel (+10) |
| 6 | Clifton Paws Grooming | B | 55 | other | solo | Bristol, GB | hello@cliftonpaws.example | industry other (+5); size solo (+15); in target area (Bristol) (+25); verified business contact channel (+10) |
| 7 | Totterdown Yoga Studio | B | 50 | fitness_and_sport |  | Bristol, GB | https://totterdownyoga.example/contact | industry fitness_and_sport (+15); in target area (Bristol) (+25); verified business contact channel (+10) |
| 8 | Severn Freight Logistics | B | 35 | logistics_and_transport | 51-200 | Avonmouth, GB | sales@severnfreight.example | industry logistics_and_transport (+10); size 51-200 (+5); in target country (GB) (+10); verified business contact channel (+10) |
| 9 | Cotswold Cyber Systems | C | 25 | software_and_it | 51-200 | Cheltenham, GB | contact@cotswoldcyber.example | size 51-200 (+5); in target country (GB) (+10); verified business contact channel (+10) |
| 10 | Ledger &amp; Co Accountants | C | 25 | accounting_and_finance | 2-10 | Bristol, GB | info@ledgerandco.example | industry accounting_and_finance (-40); size 2-10 (+30); in target area (Bristol) (+25); verified business contact channel (+10) |

## Needs attention

- **Bedminster Bikes**: flagged industry:low_confidence(0.55)
- **Gloucester Road Deli** (gloucesterroaddeli.example): fetch_failed - homepage: HTTP 503
- **Old Market Antiques** (oldmarketantiques.example): skipped_robots - blocked by robots.txt rules
- **Redcliffe Books &amp; Coffee**: flagged contact_email:personal_email_rejected
- **Totterdown Yoga Studio**: flagged size_signal:low_confidence(0.40), contact_email:not_found_in_source

## Run metrics (measured on this run)

- Wall time: 0.07 s (0.01 s per company)
- Politeness delay included: 71.0 s (simulated, not slept, when running on HTML fixtures)
- HTTP requests: 47, cache hits: 0, retries: 2, blocked by robots.txt: 1
- LLM calls: 0 (cache hits: 0), tokens in/out: 0/0
- LLM cost: $0.0000 ($0.0000 per 100 companies)
- Flagged fields: 4; personal emails redacted before the LLM: 1

_Public business information only. Flagged fields were left blank on purpose; check them on the company's website before use._
