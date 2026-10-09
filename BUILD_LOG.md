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

## Measured numbers (demo data)

## Post hooks

## Suggested screenshots / GIFs
