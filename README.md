# AAO NIW Decision Monitor

An agent that watches USCIS AAO non-precedent decisions for Form I-140 NIW
(EB-2), downloads new decision PDFs, extracts their text, and classifies the
denial reasoning against the *Dhanasar* framework — producing a dataset and a
trend report you can use to strengthen your own NIW petition.

## Architecture — four layers

```
USCIS listing (uri_1=18) ──> fetch_decisions.py ──> data/pdfs/*.pdf
                                                        │
                              extract_text.py  <────────┘
                              (pdfplumber + Tesseract fallback)
                                    │
                              data/text/*.txt
                                    │
                              rebuilds data/queue.json
                              (texts with no result yet)
                                    │
                     ┌──────────────┴───────────────┐
                     │                               │
        scripts/classify_queue.py          Claude Code (skill:
        (local Ollama + Qwen,               aao-niw-monitor)
         offline, no tokens)                classifies in BATCHES
                     │                               │
                     └──────────────┬────────────────┘
                                     │
                data/results/*.json + summary.csv + REPORT.md
```

Only the classify layer needs judgment/AI; the other three are deterministic
Python and never touch an LLM.

### Layer 1 — Fetch (`scripts/fetch_decisions.py`)

Scrapes the USCIS AAO non-precedent listing and downloads new decision PDFs
into `data/pdfs/`, deduping against `data/seen.json`. Plain Python, no
tokens. Polite crawler: reads `robots.txt` for crawl-delay, backs off on
429/503, adds jitter, and skips out-of-window PDFs by parsing the date out
of the filename before downloading anything (see "Being a good crawler"
below).

```bash
python scripts/fetch_decisions.py --pages 2
```

### Layer 2 — Extract (`scripts/extract_text.py`)

Pulls text out of each new PDF (`pdfplumber`, falling back to Tesseract OCR
only for raw scans) into `data/text/*.txt`. Plain Python, no tokens. At the
end of the run it rebuilds `data/queue.json` — every `data/text/*.txt` that
has no matching `data/results/*.json` yet. This is the only thing that
"enqueues" work for layer 3; extraction never classifies anything itself.

```bash
python scripts/extract_text.py
```

### Layer 3 — Classify (the only layer that needs judgment) — pick a backend

Reads `data/queue.json` and, for each item, produces
`data/results/<stem>.json` following the schema in `taxonomy.json`.

**Option A — local Ollama** (`scripts/classify_queue.py`): fully offline,
no tokens, no Claude Code session required.

```bash
python scripts/classify_queue.py
```

It auto-detects the backend: local Ollama + a Qwen model if
`scripts/ollama_support.py` finds one running, otherwise it falls back to
marking items `queued_for_claude` for the skill (Option B) to pick up.
Model selection prefers a smaller **instruct** Qwen model over larger
**coder** variants — classifying legal text doesn't benefit from a
code-completion model, and a smaller model finishes a multi-hundred-item
backlog without tripping the 300s per-call timeout
(`scripts/ollama_support.py::select_qwen_model`). A single malformed JSON
response from the model is retried once, then recorded as
`classification_status: "ollama_error"` rather than aborting the whole run
— one bad response out of a few hundred shouldn't lose everything else.

**Option B — Claude Code** (`.claude/skills/aao-niw-monitor/`): runs inside
a Claude Code session, covered by your Pro subscription — no separate API
key or per-token billing (a standalone `anthropic` SDK script would need
one). Inside Claude Code, from this folder:

> run the AAO monitor

Claude classifies the queue in batches (~12 decisions/pass) instead of one
turn per PDF.

Run the test suite for the Ollama path with:

```bash
python -m unittest discover -s tests
```

### Layer 4 — Aggregate

Rebuilds `data/results/summary.csv` and `REPORT.md` from everything in
`data/results/`. Built into `scripts/classify_queue.py` (runs automatically
at the end of Option A), or done by the skill's own synthesis step for
Option B. `scripts/build_summary.py` is a standalone CSV-only rebuild if you
ever need to regenerate `summary.csv` without re-running classification.

## Setup

```bash
pip install requests beautifulsoup4 pdfplumber pytesseract pillow
# macOS: brew install tesseract     (only needed for the rare raw-scan PDF)

# For Option A (local Ollama) — install Ollama and pull an instruct model:
#   https://ollama.com/download
ollama pull qwen2.5:3b
```

## Run everything

```bash
python scripts/fetch_decisions.py --pages 2
python scripts/extract_text.py
python scripts/classify_queue.py    # Option A: local Ollama
# — or, inside Claude Code —
# > run the AAO monitor             # Option B: Claude Code skill
```

## Date window

By default `fetch_decisions.py` **resumes automatically**: `--since` is the
latest `decision_date` already recorded in `data/seen.json`, minus a 3-day
overlap buffer (to cover same-day decisions posted out of order across
runs), or `2026-01-01` if there's no history yet. The date itself is read
straight off each PDF's filename (e.g. `FEB252026_02B5203.pdf` → Feb 25,
2026) — no per-file request needed to check it. Override with an explicit
window when you need one (e.g. a deeper backfill):

```bash
python scripts/fetch_decisions.py --since 2026-01-01 --until 2026-08-11
```

Or just run `scripts/update.sh`, which chains fetch + extract together and
always resumes from wherever the last run left off — safe to run as often
as you like, by hand or from cron.

The script stops paginating early once a whole listing page is older than
`--since`, on the assumption the listing is newest-first. Pass
`--no-early-stop` if that assumption ever looks wrong for a given run.

## Being a good crawler

- **robots.txt is read at runtime**, not hardcoded — the script fetches
  `https://www.uscis.gov/robots.txt`, honors any published `Crawl-delay` for
  `User-agent: *`, and falls back to a conservative 10s delay if none is
  published. It also checks `can_fetch()` before scraping the listing at all
  and refuses to run if disallowed.
- **Backoff, not retry-hard**: on `429`/`503` it waits the `Retry-After`
  header if given, otherwise exponential backoff, up to 4 attempts.
- **Jitter**: a random 0–1s is added on top of the delay so requests aren't
  perfectly periodic (less bot-like, easier on shared infra).
- **No wasted downloads**: since the decision date lives in the filename,
  out-of-window PDFs are skipped before any download request is made.
- Keep the `User-Agent` in `fetch_decisions.py` set to a real contact email —
  it's the single easiest thing that turns "unknown scraper" into "identified
  research tool" if anyone at USCIS ever looks at their access logs.

## Running unattended ("background")

There's no persistent Claude background daemon in the chat interface — the
practical options are:

1. **Claude Code Routine (recommended)** — schedule "run the AAO monitor"
   weekly. This is the only option that can also do step 3 (classification),
   since that needs Claude's judgment, not just Python.
2. **Plain cron / launchd for steps 1–2 only** — you can automate just the
   fetch/download with `cron` (Linux) or `launchd` (macOS) to keep
   `data/pdfs/` topped up between Claude Code sessions, then classify in a
   Claude Code session whenever convenient:
   ```bash
   # crontab -e — every Monday 9am
   0 9 * * 1 cd /path/to/aao-niw-monitor && python3 scripts/fetch_decisions.py --since 2026-01-01 >> fetch.log 2>&1
   ```
   Extraction (`extract_text.py`) is also plain Python and safe to chain onto
   the same cron line if you want text ready before your next session.

## Version control

This repo tracks `scripts/`, `taxonomy.json`, `.claude/skills/`, and this
README. `data/pdfs/`, `data/text/`, and `data/results/*.json` are gitignored
by default — see `.gitignore` for why. If you push this anywhere:

- **Keep it private.** USCIS redacts PII, but redactions aren't guaranteed
  perfect, and case details (clinic names, projected revenue, employer info)
  can still be identifying even when redacted correctly.
- If you want classification history version-controlled, the safer middle
  ground is tracking `data/results/summary.csv` and `REPORT.md` (aggregate
  stats) while leaving the per-case `*.json` files ignored.

```bash
git init                    # already done if you're reading this from the repo
git add .
git commit -m "aao-niw-monitor: initial pipeline"
git remote add origin <your-private-repo-url>
git push -u origin main
```

## Notes

- Set a real contact address in the `User-Agent` in `fetch_decisions.py` and
  keep the polite 2s delay — this is public government data, but be a good
  citizen toward uscis.gov.
- `data/seen.json` makes runs incremental; safe to schedule weekly (Claude
  Code Routines works well for this).
- The classification schema lives in `taxonomy.json`; extend the code list as
  you observe new recurring grounds.
