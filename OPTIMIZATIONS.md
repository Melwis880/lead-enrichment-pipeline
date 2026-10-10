# Optimization audit

Date: 2026-10-10. Scope: everything under `src/lead_enrich/`, `config/`, `n8n/`, CI and tests, at
commit `aaebffa`. Nothing in the code was changed; this file only reports.

**How this was checked.** I read all the source, then ran four experiments from a scratch
directory against the project's own classes (no outside network):

1. `RequestsTransport` + `PoliteFetcher` against a local HTTP server that sends lowercase header
   names and `text/html` with no charset.
2. A fake-clock simulation of `enrich_company` for a healthy site, a blackholed site and a
   slow-drip site, using the default settings.
3. `cProfile` of the full mock demo run (`--html-fixtures`, `MOCK_LLM=1`, `--no-cache`).
4. CPU timing of `parse_html` and the email regexes on large and adversarial inputs, plus a count
   of text repeated across each company's pages in the fixtures.

Claude API facts (prices, caching minimums, Batch API) come from the bundled Claude API reference
dated 2026-10-06. Real mode has never been run, so every LLM cost and latency figure here is an
**estimate**.

### Measured baseline

| What | Result |
|---|---|
| Mock demo run, 13 companies, CPU | 0.36 s total under cProfile; `parse_html` (bs4 `html.parser`) is ~80% of it |
| Politeness waiting in the demo run | 71 s simulated (5.5 s per company), against 0.07 s of compute |
| One company, healthy site, 0.4 s RTT (simulated) | 6.4 s, 4 requests |
| One company, blackholed host (10 s connect timeout) | **66 s**, 6 requests, then `fetch_failed` |
| One company, slow-drip server (20 s deadline + 10 s read) | **186 s**, 6 requests, then `fetch_failed` |
| `parse_html` on 150 KB / 1.8 MB HTML | 131 ms / 1.19 s |
| Email redaction on 50-340 K-char adversarial strings | 8-74 ms, linear; no catastrophic backtracking |
| Text repeated across a company's pages (fixtures) | 12% of characters (8-24% per site) |
| Lowercase `content-type: text/html` | `fetch_failed`: `not HTML (missing)` |
| Lowercase `location:` on a 301 | `fetch_failed`: `HTTP 301` (redirect not followed) |
| `Content-Type: text/html` (no charset), UTF-8 body | Title `Café` becomes `CafÃ©` |
| Test suite | 178 tests, all pass in a few seconds; slowest is the 1.6 s slow-drip test |

---

## 1) Optimization summary

**Health.** For its stated scale (tens to low hundreds of companies, run weekly), the pipeline
is efficient. CPU is negligible, every request goes through one shared `requests.Session`,
re-runs are nearly free thanks to the parsed-page and LLM caches, the LLM is called once per
company, and the input is capped at 12,000 characters. The real problems are not speed. They are
**reliability gaps in the live-network path**, which the offline tests cannot see:
`FixtureTransport` always returns a correctly cased `Content-Type` with `charset=utf-8`, so the
demo never exercises these code paths.

**Top 3 highest-impact improvements**
1. **Make header lookups case-insensitive** (F1). Today any server that sends lowercase header
   names fails every page, and its redirects are never followed. This is a one-line fix.
2. **Bound the time lost to dead and slow sites** (F2). One blackholed site costs 10x a healthy
   one, and one slow-drip site costs 29x, because timeouts are retried and then repeated over
   plain HTTP.
3. **Find the charset in the page itself** (F3). Pages served as `text/html` with no charset
   are decoded as Latin-1, so names, addresses and `£` reach the model garbled.

**Biggest risk if nothing changes.** The first real-mode run (still pending in PROGRESS.md) will
quietly lose or garble a share of real sites for reasons the demo cannot show. A few dead sites
in the list will also add minutes to every run. Because the failures look like ordinary
`fetch_failed` rows, they could be mistaken for normal small-business website problems.

---

## 2) Findings (prioritized)

### F1. Header lookups are case-sensitive, so lowercase headers break fetching
- **Category:** Reliability (Network)
- **Severity:** High
- **Impact:** fetch success rate, lead coverage
- **Evidence:** `RequestsTransport.get` turns the headers into a plain dict:
  `dict(resp.headers)` at [fetcher.py:78](src/lead_enrich/fetcher.py#L78). The dict keeps the
  server's original casing, but every lookup uses canonical casing:
  `resp.headers.get("Location")` at [fetcher.py:261](src/lead_enrich/fetcher.py#L261),
  `"Content-Type"` at [fetcher.py:266](src/lead_enrich/fetcher.py#L266), `"Location"` at
  [fetcher.py:289](src/lead_enrich/fetcher.py#L289) (robots.txt redirect) and `"Retry-After"` at
  [fetcher.py:346](src/lead_enrich/fetcher.py#L346). Measured: a lowercase `content-type`
  gives `not HTML (missing)`, and a lowercase `location` gives `HTTP 301`.
- **Why it's inefficient:** header names are case-insensitive (RFC 9110 §5.1). Some proxies and
  edges send them lowercase over HTTP/1.1; Envoy does this by default. Every such site becomes
  `fetch_failed`, robots.txt redirects on them are not followed, and their `Retry-After` is
  ignored. How common this is among small-business hosts has not been measured.
- **Recommended fix:** keep headers case-insensitive at the boundary. Wrap them in
  `requests.structures.CaseInsensitiveDict` in `HttpResponse.__post_init__`, so every transport,
  including the test fakes, is covered (patch in section 6).
- **Tradeoffs / risks:** none significant. Add one transport test with lowercase headers.
- **Expected impact:** affected sites go from 0% to normal success. The share of real sites
  affected is unknown; measure it on the first live run.
- **Removal Safety:** Safe
- **Reuse Scope:** module (`fetcher.py`)

### F2. Dead and slow sites cost 10-29x a healthy site
- **Category:** Reliability / Network
- **Severity:** High (live runs)
- **Impact:** wall time per run, predictability of the weekly job
- **Evidence:** simulated per company: healthy 6.4 s, blackholed 66 s, slow drip 186 s. Four
  things add up:
  - `_get_with_retries` retries *every* `TransportError`
    ([fetcher.py:337-343](src/lead_enrich/fetcher.py#L337-L343)), including connect timeouts and
    `deadline ... exceeded` ([fetcher.py:67-68](src/lead_enrich/fetcher.py#L67-L68)).
  - One float `timeout` is used for both connecting and reading
    ([fetcher.py:63](src/lead_enrich/fetcher.py#L63)). urllib3 also tries each resolved address
    in turn, so a host with both an IPv6 and an IPv4 address can cost twice that.
  - `_fetch_home` then repeats the whole sequence over `http://`
    ([pipeline.py:146-153](src/lead_enrich/pipeline.py#L146-L153)) for any `network error`,
    timeouts included.
  - The robots.txt fetch takes all of these retries before the homepage is ever tried.
- **Why it's inefficient:** a server that dripped bytes past a 20 s deadline or blackholed a
  connect will almost certainly do the same 1-2 s later. Retrying it three times, twice, buys
  little. In a sequential run, every one of those seconds blocks all the other companies.
- **Recommended fix:**
  1. Split the timeouts: `timeout=(connect, read)` with connect around 5 s.
  2. Give `TransportError` a `retryable` flag. Do not retry deadline-exceeded or connect
     timeouts; keep retries for 429/5xx, connection resets and refused connections.
  3. Fall back to `http://` only when the HTTPS failure was TLS-related or the connection was
     refused, not after a timeout.
  4. Optionally, add a per-company time budget (for example 60 s), checked before each subpage.

  Patch sketch in section 6.
- **Tradeoffs / risks:** a site with a one-off connect blip loses its retry. This changes the
  retry semantics recorded in DECISIONS.md ("2 retries ... for timeouts"), so add a new entry;
  the retry-count tests will need updating.
- **Expected impact:** blackholed site from 66 s to about 10-15 s; slow-drip site from 186 s to
  about 30-60 s (estimates from the same simulation arithmetic).
- **Removal Safety:** Needs Verification
- **Reuse Scope:** module (`fetcher.py`, `pipeline._fetch_home`)

### F3. Pages served without a charset are decoded as Latin-1
- **Category:** Reliability (correctness of LLM input)
- **Severity:** Medium
- **Impact:** extraction quality (names, cities, `£`, accented words); slightly more input tokens
- **Evidence:** `decode_body(..., resp.encoding)` at
  [fetcher.py:79](src/lead_enrich/fetcher.py#L79). For any `text/*` response without a charset,
  `requests` sets `resp.encoding` to `ISO-8859-1` (checked:
  `get_encoding_from_headers({"content-type": "text/html"}) == "ISO-8859-1"`). Latin-1 decoding
  never fails, so the UTF-8 fallback in `decode_body` is never reached. Measured: `Café` became
  `CafÃ©`.
- **Why it's inefficient:** most such pages are UTF-8 and declare it only in
  `<meta charset="utf-8">`. The model receives garbled text, and evidence matching for contact
  channels can miss where an address contains non-ASCII characters.
- **Recommended fix:** use the header charset only when the header actually names one.
  Otherwise read the `<meta>` declaration from the bytes already in the buffer with
  `bs4.dammit.EncodingDetector.find_declared_encoding(body[:4096], is_html=True)`; bs4 is already
  a dependency, and I checked that it handles both `<meta charset>` and the `http-equiv` form.
  Default to UTF-8. Do **not** use `resp.apparent_encoding`: BUILD_LOG records that it re-reads
  the stream and hung for 37 s.
- **Tradeoffs / risks:** a page with a wrong `<meta>` declaration is decoded as it declares,
  which is what browsers do too.
- **Expected impact:** correct text for every UTF-8 page without a header charset.
- **Removal Safety:** Safe
- **Reuse Scope:** module (`fetcher.py`)

### F4. A sequential run sits idle during politeness waits and LLM calls
- **Category:** Concurrency
- **Severity:** Medium at the documented scale; High beyond roughly 300 companies
- **Impact:** wall time
- **Evidence:** a single loop over companies at
  [pipeline.py:234](src/lead_enrich/pipeline.py#L234), with `_wait_turn` sleeping in that thread
  ([fetcher.py:311-321](src/lead_enrich/fetcher.py#L311-L321)). The demo measured 5.5 s of
  politeness wait per company against 0.005 s of compute. CASE_STUDY estimates 15-20 minutes
  per 100 companies, almost all of it idle waiting (polite delays, network round-trips and the
  LLM call).
- **Why it's inefficient:** the delay protects *one host*, but because the run is sequential it
  blocks *all* hosts. Each company is a different domain (duplicates are removed at
  [pipeline.py:101-104](src/lead_enrich/pipeline.py#L101-L104)), so working on several companies
  at once keeps every site's own delay untouched.
- **Recommended fix (only if volume grows):** a `ThreadPoolExecutor` over companies with 4-8
  workers. Prerequisites:
  - a lock around `_robots`, `_last_request` and `stats` in `PoliteFetcher`, reserving each
    host's next request time under the lock and sleeping outside it;
  - unique temp file names in `JsonDiskCache.set`, because today two writers of the same key
    would both use `<digest>.tmp` ([cache.py:43](src/lead_enrich/cache.py#L43));
  - cancelling pending work on Ctrl-C so partial outputs still work.

  A single-threaded alternative: interleave companies by always taking the next request from
  whichever host is ready. No threads, the same politeness rules, but the LLM call still blocks.
- **Tradeoffs / risks:** this reverses the DECISIONS.md entry of 2026-10-09 ("Sequential
  processing ... not needed for tens-hundreds of companies"), so it needs a new entry. Many small
  sites share one hosting IP (Wix, Squarespace, shared hosts) and would receive N parallel
  requests; consider a per-IP cap. Debug logs and the progress output become interleaved.
- **Expected impact:** close to N times faster, for example 15-20 minutes per 100 companies down
  to about 3-4 minutes with 6 workers (estimate). No effect on cost.
- **Removal Safety:** Needs Verification
- **Reuse Scope:** service-wide (`pipeline.py`, `fetcher.py`, `cache.py`)

### F5. LLM cost: Haiku 4.5 is now 10x the per-token price of Haiku 5.5
- **Category:** Cost
- **Severity:** Medium (the dollar amounts are small at demo scale)
- **Impact:** cost per 100 companies
- **Evidence:** the model is `claude-haiku-4-5-20251001` at $1/$5 per million tokens
  ([config.py:31](src/lead_enrich/config.py#L31), [settings.yaml:16-25](config/settings.yaml#L16-L25)).
  `claude-haiku-5-5` costs $0.10/$0.50 for prompts up to 100K tokens; this pipeline's prompts are
  about 4K. The dry-run ceiling today is about $0.62 per 100 companies.
- **Why it's inefficient:** this is a high-volume structured-extraction task, the exact workload
  the cheapest current model is meant for.
- **Recommended fix:** evaluate Haiku 5.5 with `ENRICH_MODEL=claude-haiku-5-5` on the
  20-company hand-checked sample already planned in PROGRESS.md "Next steps". Code needed before
  that:
  1. **Thinking is on by default on Haiku 5.5 and counts toward `max_tokens` (1024).** Add an
     optional `llm.effort` setting (for example `low`) and send it in `output_config`. Send it
     only when it is set, because `effort` errors on Haiku 4.5. Otherwise expect more
     `model output truncated` errors.
  2. Update the two price settings.
  3. Expect about 30% more tokens for the same text (newer tokenizer).

  The extractor already reads content blocks by type
  ([extractors.py:152](src/lead_enrich/extractors.py#L152)) and sets no `temperature`, so both
  of Haiku 5.5's stricter rules are already met.
- **Related gap:** prices are not tied to the model. After `ENRICH_MODEL=...` the cost report
  silently keeps using the Haiku 4.5 prices. A small model-to-price table, or a warning when the
  model is not the configured one, would prevent wrong numbers in `run_stats.json`.
- **Tradeoffs / risks:** project CLAUDE.md fixes the default as `claude-haiku-4-5-20251001`, so
  changing the default is the owner's decision. Extraction quality has not been measured on
  either model.
- **Expected impact:** about $0.62 down to about $0.09 per 100 companies, roughly 85% less
  (estimate: 30% more tokens, plus low-effort thinking).
- **Removal Safety:** Needs Verification
- **Reuse Scope:** module (`extractors.py`, `config.py`)

### F6. Cache lifetime equals the weekly schedule, and robots.txt is kept for a week
- **Category:** Caching (stale invalidation)
- **Severity:** Medium
- **Impact:** freshness of the weekly n8n run; robots.txt compliance
- **Evidence:** `cache_ttl_hours: 168` ([settings.yaml:13](config/settings.yaml#L13)), and the
  n8n trigger fires every Monday at 07:00 ([n8n/enrich-weekly.json](n8n/enrich-weekly.json)).
  An entry is a miss only when its age is *greater than* the TTL
  ([cache.py:30-34](src/lead_enrich/cache.py#L30-L34)). A page stored at 07:00:03 last Monday is
  read at about 07:00:03 this Monday, so its age is 168 h give or take a few seconds. Cache hits
  also make the run faster, so later companies are reached *earlier* than last week, and their
  entries are hits too. The same document then gives an LLM cache hit, so the weekly run can
  reproduce last week's report without ever touching the sites. robots.txt uses the same cache
  and TTL ([fetcher.py:279-302](src/lead_enrich/fetcher.py#L279-L302)); RFC 9309 §2.4 says a
  crawler should not use a cached robots.txt for more than 24 h.
- **Why it's inefficient:** the scheduled job's whole purpose (catching changes) depends on
  sub-second timing.
- **Recommended fix:** two TTLs: `robots_cache_ttl_hours: 24` and a page TTL clearly shorter
  than the schedule interval (for example 72 h). Implement with a `max_age` argument on
  `JsonDiskCache.get` or a second cache instance. Document the rule "page TTL < schedule
  interval".
- **Tradeoffs / risks:** the weekly run makes its full set of requests (about 4 per company),
  which is what it was meant to do.
- **Expected impact:** the weekly report always reflects this week's sites; robots.txt is
  rechecked daily.
- **Removal Safety:** Safe (config plus a small cache API addition)
- **Reuse Scope:** module (`cache.py`, `fetcher.py`, config)

### F7. LLM input budget: repeated navigation text, and an even split that wastes budget
- **Category:** Cost / Algorithm (token efficiency)
- **Severity:** Low-Medium
- **Impact:** input tokens per company, or more useful content for the same tokens
- **Evidence:**
  - Only `script/style/noscript/svg/iframe/template/canvas` are stripped
    ([parser.py:45](src/lead_enrich/parser.py#L45)), so `nav`, header, cookie banners and footer
    are repeated on every page.
  - `build_llm_document` gives each page `max_chars // len(pages)` and cuts each page from the
    top ([parser.py:198-204](src/lead_enrich/parser.py#L198-L204)). The navigation at the top of
    each page is kept while the content further down is cut.
  - Budget that a short page does not use (for example a 300-character contact page) is not
    given to a long homepage. A homepage of 9,000 characters, an about page of 3,000 and a
    contact page of 300 use only 7,300 of the 12,000 characters, and the homepage is cut at
    4,000.
  - Measured on the fixtures: 12% of each company's text repeats across its pages. These are
    hand-written pages with little navigation; real sites are likely higher (unmeasured).
- **Why it's inefficient:** the same tokens are paid for several times, and the even split
  cuts content while budget is left over.
- **Recommended fix:**
  1. Drop `nav` and `[role=navigation]` from the text. Links are collected before stripping, so
     page discovery is unaffected.
  2. In `build_llm_document`, drop lines already sent in an earlier page; this needs text split
     on block elements (`get_text("\n")`) instead of on spaces.
  3. Share the budget so short pages keep their full text and long pages split the remainder
     (sketch in section 6).
- **Tradeoffs / risks:** requires bumping `PARSER_VERSION`, which invalidates the page cache.
  Keep the homepage footer, which often holds the address and contact email. Plain-text emails
  are collected after stripping ([parser.py:143-146](src/lead_enrich/parser.py#L143-L146)), so
  collect them before removing any new tags, or contact evidence will shrink. Redaction must
  still run on all text.
- **Expected impact:** roughly 10-25% fewer input tokens per company, or the same tokens with
  more real content (estimate; measure on live pages).
- **Removal Safety:** Needs Verification
- **Reuse Scope:** module (`parser.py`)

### F8. The page cache stores full text, and nothing ever prunes it
- **Category:** I/O / Memory (disk)
- **Severity:** Low
- **Impact:** disk use over time
- **Evidence:** `PageSource` caches the whole `ParsedPage`, including all its text and links
  ([pipeline.py:141-142](src/lead_enrich/pipeline.py#L141-L142)); a 1.8 MB page produced 1.6
  million characters of text. Expired entries are skipped but never deleted
  ([cache.py:30-34](src/lead_enrich/cache.py#L30-L34)). A `PARSER_VERSION` bump or a changed
  page leaves old files behind for good. The LLM cache has no TTL and gains a new entry whenever
  page text changes.
- **Why it's inefficient:** the LLM never sees more than `max_input_chars` (12,000) per company,
  and links are only needed for the homepage.
- **Recommended fix:**
  1. Cap the stored text generously (for example 50,000 characters, safely above any per-page
     budget).
  2. Store links for the homepage only.
  3. Add `enrich cache prune`, which deletes expired files and files older than N days.
- **Tradeoffs / risks:** if `max_input_chars` is later raised above the cap, cached pages are
  short. Put the cap in the cache key, or keep it well above the budget.
- **Expected impact:** bounded disk use (small in absolute terms at this scale).
- **Removal Safety:** Likely Safe
- **Reuse Scope:** module (`pipeline.py`, `cache.py`, `cli.py`)

### F9. Timing fields that are measured but never reported (dead code)
- **Category:** Maintainability (Dead Code)
- **Severity:** Low
- **Impact:** diagnosability; less code that does nothing
- **Evidence:** `LeadRecord.fetch_s` and `total_s` are set
  ([pipeline.py:183](src/lead_enrich/pipeline.py#L183),
  [pipeline.py:215](src/lead_enrich/pipeline.py#L215)) but never read by the CSV, the summary or
  `run_stats.json`. `FetchResult.from_cache` is set
  ([pipeline.py:135](src/lead_enrich/pipeline.py#L135)) but never read in `src/`.
- **Why it matters:** these are exactly the numbers needed to find the slow sites in F2 and to
  replace the estimate in CASE_STUDY with measured fetch time.
- **Recommended fix:** report them, not delete them: average and p95 of `fetch_s`, plus the 3
  slowest domains, in `run_stats.json` and the summary. Drop `FetchResult.from_cache` unless a
  test uses it.
- **Tradeoffs / risks:** none.
- **Classification:** Dead Code (reuse it rather than remove it)
- **Removal Safety:** Safe
- **Reuse Scope:** module (`pipeline.py`, `report.py`)

### F10. Two identical validators in the scoring config
- **Category:** Maintainability (Reuse Opportunity)
- **Severity:** Low
- **Evidence:** `IndustryRule._known` and `SizeRule._known` differ only in the allowed set and
  the message ([scoring.py:13-34](src/lead_enrich/scoring.py#L13-L34)).
- **Recommended fix:** one helper, `_only_known(values, allowed, label)`, called by both.
  Optional; worth doing only if more such rules are added.
- **Classification:** Reuse Opportunity
- **Removal Safety:** Safe
- **Reuse Scope:** local file

### Checked and not worth changing
- **HTML parsing CPU** (`html.parser` instead of `lxml`): 131 ms for a typical 150 KB page,
  against at least 6 s of polite waiting per company. Adding a compiled dependency does not pay
  off.
- **Email and redaction regexes:** linear on adversarial input (8-74 ms on 340K characters).
  No change needed.
- **Prompt caching:** on Haiku 4.5 the minimum cacheable prefix is 4,096 tokens, and the system
  prompt plus schema is about 1,000, so it would never apply. On Haiku 5.5 (512-token minimum) it
  would apply but save about $0.0001 per company.
- **Structured-output schema compilation:** the schema is static; there is a one-time compile
  on first use, then it is cached for 24 h. Negligible.
- **Report and scoring repetition** (`compute_metrics` twice, `flags()` recomputed, target sets
  rebuilt per lead, `normalize_domain` for each link): microseconds at hundreds of rows.
- **`JsonDiskCache.set` syscalls** (mkdir/chmod twice per write): negligible next to network
  I/O, and they keep the 0700/0600 permissions.
- **CI runs lint on all 3 Python versions:** a separate lint job would save a few seconds per
  run. Not worth the extra workflow complexity.

---

## 3) Quick wins (do first)

| # | Change | Effort | Payoff |
|---|---|---|---|
| F1 | `CaseInsensitiveDict` for response headers, plus one lowercase-header test | ~15 min | Sites behind lowercasing proxies work |
| F3 | Charset from the header only if declared, else `<meta>`, else UTF-8, plus one test | ~30 min | No garbled text for UTF-8 pages served without a charset |
| F6 | Separate TTLs (robots 24 h, pages < schedule interval) | ~30 min | Weekly run actually refreshes; RFC 9309 §2.4 |
| F9 | Report `fetch_s` / slowest domains in `run_stats.json` | ~30 min | Measured fetch time for CASE_STUDY; finds slow sites |
| F2 (part) | `timeout=(connect, read)` and no retry after deadline-exceeded | ~1 h | Cuts most of the dead-site penalty |

All of these should land before the first real-mode run, so that run measures the pipeline and
not these bugs.

## 4) Deeper optimizations (do next)

1. **F2 in full:** a `retryable` kind on `TransportError`, HTTP fallback only after TLS errors or
   refused connections, and a per-company time budget. Needs a DECISIONS.md entry.
2. **F5:** evaluate `claude-haiku-5-5` (with `effort` and price settings) against Haiku 4.5 on
   the hand-checked 20-company sample. The owner decides the default.
3. **F7:** navigation stripping, line dedupe across pages, and a shared input budget, measured
   on live pages.
4. **F4:** cross-domain concurrency, or a single-threaded interleaving scheduler, once lists
   grow beyond a few hundred companies. Needs a new DECISIONS.md entry reversing the 2026-10-09
   decision.
5. **Message Batches API** for the scheduled job: 50% off all tokens; most batches finish within
   1 hour, at most 24 hours. It needs a two-phase pipeline (fetch all, submit, poll, then
   validate and score). It saves about $0.31 per 100 companies on Haiku 4.5 and about $0.05 on
   Haiku 5.5 (estimates), so it only pays off at tens of thousands of companies per month.
6. **F8:** text cap, homepage-only links, and `enrich cache prune`.

## 5) Validation plan

**Correctness tests to add (offline, local server like `tests/test_transport.py`):**
- Lowercase `content-type`, `location` and `retry-after` produce the same `FetchResult` as
  canonical casing (F1).
- A `text/html` response with no charset, a UTF-8 body and `<meta charset="utf-8">` decodes
  `Café`; the `http-equiv` form with `windows-1252` decodes correctly; no declaration means
  UTF-8 (F3).
- Fake clock: a deadline-exceeded response is not retried; a connect timeout does not trigger
  the HTTP fallback; an SSL error does; a 503 is still retried with backoff (F2).
- Cache: an entry stored at `t` is a miss at `t + 25 h` for robots.txt and at `t + 73 h` for
  pages (F6).
- `build_llm_document`: a 300-character page keeps its full text, and its unused budget goes to
  the long page; total length stays at or under `max_input_chars` (F7).
- Concurrency, if adopted: with 6 workers and a fake clock, no origin ever sees two requests
  closer than `min_delay_seconds`; each robots.txt is fetched once; results match the
  sequential run (F4).

**Benchmarks and metrics (before and after):**
- Reuse the fake-clock simulation (healthy / blackholed / slow-drip) as a pytest-benchmark or a
  plain test. Targets: blackholed under 20 s, slow drip under 60 s, healthy unchanged at 6.4 s.
- First live run on 20-50 real public sites: per-status counts, the share of `not HTML
  (missing)` and `HTTP 30x` failures (should be zero after F1), the share of pages with
  `Ã`-style mojibake (grep the cache; should be zero after F3), average and p95 of `fetch_s`,
  and total wall time.
- LLM (real mode, F5/F7): `input_tokens`, `output_tokens` and cost per company from
  `run_stats.json`; `max_tokens` truncation count; field accuracy on the 20 hand-checked
  companies, comparing Haiku 4.5 and Haiku 5.5, before and after F7.
- The mock demo run (`enrich run input/companies.csv --html-fixtures fixtures/sites`) must keep
  producing the same `examples/demo-mock/` outputs. F1, F3 and F6 change nothing in fixture mode;
  F7 bumps `PARSER_VERSION` and may change the mock documents, but not the mock answers.

**Profiling for anything new:** `python -m cProfile -s cumtime -m lead_enrich.cli run ...` on
fixtures for CPU; on live runs, the per-company `fetch_s` / `llm_s` breakdown from F9.

## 6) Optimized code / patch (sketches, not applied)

**F1: case-insensitive headers** (`fetcher.py`)
```python
from collections.abc import Mapping
from requests.structures import CaseInsensitiveDict


@dataclass
class HttpResponse:
    url: str
    status: int
    headers: Mapping[str, str]
    text: str

    def __post_init__(self) -> None:
        # Header names are case-insensitive (RFC 9110 §5.1); some proxies send them lowercase.
        self.headers = CaseInsensitiveDict(self.headers)
```
What changed: every transport's headers, including fixtures and test fakes, are looked up
without regard to case. The existing `.get("Content-Type")` / `.get("Location")` calls stay
as they are.

**F3: charset detection without re-reading the stream** (`fetcher.py`)
```python
from bs4.dammit import EncodingDetector


def body_encoding(content_type: str | None, body: bytes) -> str | None:
    """Declared charset: the Content-Type header if it names one, else <meta> in the first 4 KB.
    None means UTF-8. Never requests' ISO-8859-1 default for text/*."""
    if content_type and "charset=" in content_type.lower():
        return requests.utils.get_encoding_from_headers({"content-type": content_type})
    return EncodingDetector.find_declared_encoding(body[:4096], is_html=True)


# in RequestsTransport.get:
data = bytes(body[: self.max_bytes])
text = decode_body(data, body_encoding(resp.headers.get("Content-Type"), data))
```
What changed: `resp.encoding` is no longer trusted when the server sent no charset. The
existing `decode_body` still handles unknown or broken charsets.

**F2: timeouts and retries** (`fetcher.py`, `pipeline.py`)
```python
class TransportError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, kind: str = "other") -> None:
        super().__init__(message)
        self.retryable, self.kind = retryable, kind   # kind: timeout | tls | refused | other

# RequestsTransport.get
self.session.get(url, ..., timeout=(min(5.0, timeout), timeout), ...)
raise TransportError(f"deadline of ...s exceeded", retryable=False, kind="timeout")
except requests.ConnectTimeout as exc: raise TransportError("connect timeout", retryable=False, kind="timeout") from exc
except requests.exceptions.SSLError as exc: raise TransportError("TLS error", kind="tls") from exc

# PoliteFetcher._get_with_retries
except TransportError as exc:
    if last_try or not exc.retryable:
        raise
```
`FetchResult` gains `error_kind`; `_fetch_home` tries `http://` only when
`error_kind in {"tls", "refused"}`.

**F6: separate TTLs** (`cache.py`, `fetcher.py`, config)
```python
def get(self, key: str, max_age_hours: float | None = None) -> dict[str, Any] | None:
    ttl = self.ttl_seconds if max_age_hours is None else max_age_hours * 3600
    ...


# fetcher.robots_for
cached = self.cache.get(f"robots:{origin}", max_age_hours=self.settings.robots_cache_ttl_hours)
```
```yaml
fetch:
  cache_ttl_hours: 72          # pages; keep below the schedule interval (weekly -> < 168)
  robots_cache_ttl_hours: 24   # RFC 9309 §2.4
```

**F7: share the input budget** (`parser.py`)
```python
def _allocate(lengths: list[int], total: int) -> list[int]:
    """Short pages keep their full text; long pages split what is left evenly."""
    alloc, remaining = [0] * len(lengths), total
    order = sorted(range(len(lengths)), key=lengths.__getitem__)
    for k, i in enumerate(order):
        alloc[i] = min(lengths[i], remaining // (len(order) - k))
        remaining -= alloc[i]
    return alloc
```
In `build_llm_document`, replace the fixed `budget` with `_allocate([len(p.text) for p in
pages], max_chars)`; a page is marked truncated when its allocation is smaller than its
length.

**F5: optional effort setting** (`config.py`, `extractors.py`)
```python
class LLMSettings(BaseModel):
    effort: str | None = None  # e.g. "low" for claude-haiku-5-5; leave unset for Haiku 4.5


output_config = {"format": {"type": "json_schema", "schema": EXTRACTION_JSON_SCHEMA}}
if self.settings.effort:
    output_config["effort"] = self.settings.effort
```
Include `effort` in `_cache_key` so cached answers from different effort levels are kept apart.

---

## Applied (2026-10-10)

Applied on branch `claude/youthful-knuth-jlcprz`, one commit per item. Each fix has a test
that reproduces the problem and fails on the old code (checked by running the new tests
against a copy of `src/` from `aaebffa`). `MOCK_LLM=1 pytest` passed after every commit.

| Item | Commit | What changed |
|---|---|---|
| F1 | `63ee529` | `HttpResponse.headers` is a `CaseInsensitiveDict`, for every transport |
| F3 | `7ef818f` | Charset from `Content-Type` only if it names one, else the page's `<meta>` (bs4 `EncodingDetector` on the bytes already read), else UTF-8 |
| F2 (partial) | `618f90a` | `fetch.connect_timeout_seconds` (5 s); `TransportError.kind` / `.retryable`; no retry after a connect timeout or a blown deadline; HTTP fallback only after `tls` / `refused`. DECISIONS.md entry |
| F6 | `972729d` | `fetch.cache_ttl_hours` 168 -> 72; new `fetch.robots_cache_ttl_hours` = 24 via `JsonDiskCache.get(max_age_hours=...)` |
| F9 | `c153e5c` | `avg_fetch_time_s` and `slowest_companies` in `run_stats.json` and `summary.md`; demo output refreshed (identical `enriched.csv`). `FetchResult.from_cache` kept: tests use it |
| LLM cache (security review leftover) | `94afa33` | Cache moved to `pipeline.enrich_company`, stores the validated `Enrichment` only; key adds evidence, `min_confidence`, `VALIDATION_VERSION`. Validation also removes personal emails from free-text fields |
| Price table | `bd29a44` + alias commit | `llm.prices` (model -> input/output per MTok), only `claude-haiku-4-5-20251001` and its alias `claude-haiku-4-5` (same price); unknown model: start-up warning, `cost_usd: null`, "unknown price" in summary, CLI line and dry run |

### Measurements, before -> after

All numbers are from local sockets on 127.0.0.1, a fake-clock simulation or the mock demo
(**demo data**). No live website or real Claude API call was involved.

**Real sockets, `PoliteFetcher.fetch` of one host, default settings, real sleeps**
(`measure_fetch.py`; a blackholed host is a listen queue that is full, a drip sends one byte
every 0.2 s):

| Case | Before | After |
|---|---|---|
| Healthy host | 2.0 s, 2 requests | 2.0 s, 2 requests |
| Refused port | 4.0 s, 3 attempts | 4.0 s, 3 attempts (still retried, by design) |
| Blackholed host | **33.0 s**, 3 attempts | **5.0 s**, 1 attempt |
| Slow-drip host | **63.1 s**, 3 attempts | **20.0 s**, 1 attempt |

**Fake-clock simulation of `enrich_company` for one company** (`sim_company.py`, same model as
the audit: 10 s / 5 s connect timeout, drip = deadline + read timeout worst case):

| Case | Before | After |
|---|---|---|
| Healthy site | 6.4 s, 4 requests | 6.4 s, 4 requests |
| Blackholed host (https + http) | **66.0 s**, 6 requests | **5.0 s**, 1 request |
| Slow drip (https + http) | **186.0 s**, 6 requests | **30.0 s**, 1 request |
| HTTPS TLS broken, HTTP fine | 10.6 s, read over HTTP | 10.6 s, read over HTTP (unchanged) |
| HTTPS blackholed, HTTP fine | 39.4 s, read over HTTP | 5.0 s, `fetch_failed` (accepted trade-off, see DECISIONS.md) |

**Headers and charset** (`exp_headers.py`, 4 cases against a local server): before, 1 of 4
handled (canonical headers with a charset); after, 4 of 4 (lowercase `content-type`, lowercase
`location`, `text/html` without a charset: `Café`, not `CafÃ©`).

**Cache lifetime (tests with a fake wall clock):** a page cached 168 h minus 5 s earlier was
reused before and is re-fetched now; robots.txt cached 25 h earlier was reused before and is
re-read now; a page cached 71 h earlier is still served from cache (re-runs stay free).

**Mock demo run** (`MOCK_LLM=1 enrich run input/companies.csv --html-fixtures fixtures/sites`,
demo data): `enriched.csv` byte-identical before and after; 11 ok / 1 skipped by robots /
1 fetch failed; tiers A 5, B 3, C 2, review 1; 47 requests, 2 retries, 4 flagged fields, 1
personal email redacted; simulated politeness wait 70.9 s; compute 0.09 s.

**Tests:** 178 -> 205 (27 new, 2 extractor-level cache tests replaced by pipeline-level ones).
Suite time locally about 6 s -> 8-10 s, mostly from the new real-socket tests.

### Behaviour changes to be aware of
- A site whose port 443 is firewalled while port 80 works is now `fetch_failed`, no longer an
  `insecure_transport` row. A DNS failure on HTTPS no longer tries HTTP either (same host name).
- A one-off connect blip is no longer retried; read timeouts, resets, refused connections,
  429 and 5xx still are.
- Weekly runs now re-read every site (about 4 requests per company), as intended.
- LLM cache entries written before this change (raw answers) are never read again; delete
  `cache/llm` to remove them.
- A personal email in a free-text field is replaced by `[personal email removed]`. In
  `products_services` that can leave an item that is only the placeholder.
- The price table holds only `claude-haiku-4-5-20251001` and its alias `claude-haiku-4-5` (same
  model, same price). Any other model is reported as "unknown price" until it is added to
  `llm.prices`.

### Deferred, and why
- **F2, rest (per-company time budget):** not part of this request. With the retry and fallback
  changes, the worst single site drops from 186 s to about 30 s, so the budget matters less.
  Still open as well: an overall deadline for the response-header phase (PROGRESS.md "Next
  steps").
- **F4 (concurrency):** reverses the documented sequential-processing decision and is not
  needed for tens to low hundreds of companies. Revisit if lists grow past a few hundred.
- **F5 (model change to Haiku 5.5):** the owner's decision; project CLAUDE.md fixes the default.
  It needs an `effort` setting first, and extraction quality has not been measured on any model
  (real mode was never run).
- **F7 (LLM input budget):** needs a `PARSER_VERSION` bump and a measurement on live pages;
  the fixture pages are too short to show a token difference.
- **F8 (cache text cap, prune command):** low impact at this scale.
- **F10 (shared validator helper):** cosmetic.
