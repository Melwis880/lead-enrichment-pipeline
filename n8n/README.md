# n8n workflow (optional)

`enrich-weekly.json` runs the pipeline every Monday at 07:00 and posts `output/summary.md` to a
Slack channel. On failure it posts only the exit code; the log stays on the server, because log
lines can contain text from the crawled sites. Every field in `summary.md` is escaped for Slack
and Markdown (`&`, `<`, `>`, backticks, brackets).

Status: written by hand against the n8n node formats; **not yet imported into a live n8n
instance** (none was available in the build environment). Check node versions after import.

## Setup

1. Self-hosted n8n on the same machine as this repo (the Execute Command node runs shell
   commands, so it is not available on n8n Cloud, and newer n8n versions disable it unless you
   allow it, e.g. by removing it from `NODES_EXCLUDE`).
2. Clone the repo to `/opt/lead-enrichment-pipeline`, create `.venv`, `pip install -e .`, and put
   `ANTHROPIC_API_KEY` in `.env` (readable only by the n8n user).
3. Import `enrich-weekly.json` (Workflows -> Import from file).
4. In "Post summary to Slack", select your Slack credential and channel.
5. Run once manually, then activate.

The on-disk caches make weekly re-runs cheap: parsed pages come from cache for a week, and
unchanged page text never triggers a second LLM call.
