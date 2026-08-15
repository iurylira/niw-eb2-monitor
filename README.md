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

**Data is partitioned by decision year**: `data/pdfs/<year>/`,
`data/text/<year>/`, and `data/results/<year>/` (year comes from the
filename, e.g. `JUL082026_10B5203.pdf` → `2026/`; unparseable dates land in
an `unknown/` bucket). `extract_text.py` and `classify_queue.py` mirror
whatever subfolder each source file is already in, so this requires no
extra bookkeeping. `summary.csv` and `REPORT.md` stay aggregated across all
years at the top level. If you're migrating an older flat layout, run
`scripts/migrate_to_year_folders.py` once (idempotent, reports collisions
instead of overwriting).

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
`data/results/<stem>.json` following the schema in `taxonomy.json`, which
includes a plain-English `reason_summary` for every decision (not just
`denial_reasons` codes) and free-text `lessons`.

`outcome` (`dismissed` / `sustained` / `remanded` / `withdrawn_moot`) is
**not** left entirely to the model's judgment: every AAO decision ends with
an explicit `ORDER: The appeal is dismissed/sustained...` line, which
`order_line_outcome()` in `classify_queue.py` regex-parses as ground truth
and uses to override the model's answer whenever they disagree. This exists
because a 3B local model was found to persistently confuse "the AAO
sustains the appeal" (petitioner wins) with prose that merely discusses
sustaining the *original officer's* denial — a targeted prompt fix reduced
but didn't eliminate it, so the deterministic override is the actual fix.
`decision_date` gets the same treatment for the same reason: the filename
already encodes the true date (used everywhere else in the pipeline for
date-window filtering), and the model was found to occasionally swap in a
different month it read out of the decision's prose.

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

`REPORT.md`'s "Top denial reasons" list pulls each code's description
straight from `taxonomy.json` (no reclassification needed to keep it
current), and its "Patterns & emerging themes" section is a separate AI
synthesis pass (`synthesize_patterns_with_ollama`) that reads across a
sample of up to 150 dismissed decisions' `reason_summary` text looking for
cross-cutting patterns the fixed taxonomy codes don't capture on their own,
plus a "candidate new denial patterns" callout when something recurs that
isn't well covered by any existing code yet.

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

## Roadmap — making this actually useful for your own petition

The dataset today answers "what fails, in general." A petition needs "what
succeeds, for someone like me." Ranked roughly by payoff:

1. **Mine the wins, not just the losses.** `synthesize_patterns_with_ollama`
   only ever samples `outcome == "dismissed"` decisions. Add a mirror pass
   over `sustained` decisions: what evidence/framing shows up across wins?
2. **Deepen the archive.** A handful of confirmed wins out of a few hundred
   decisions isn't enough to generalize from. Backfill further back
   (`fetch_decisions.py --since 2023-01-01 --pages 40`, then extract +
   classify) so there's a larger win sample to learn from.
3. **Filter precedent to your own occupation/endeavor.** A mining
   engineer's winning Prong 1 argument isn't a software engineer's. Add
   `scripts/find_similar.py "software engineer"` to filter `summary.csv` by
   `occupation`/`endeavor_type` before reading lessons.
4. **Stress-test your own draft against the taxonomy.** The most direct use
   of this dataset is checking, not reading: cross-reference a draft
   petition against every documented `denial_reasons` pattern before
   filing, not after a denial.
5. **Record evidence types, not just outcomes.** `denial_reasons` codes
   capture AAO's conclusion, not what evidence was actually on the table
   (independent expert letters vs. employer-only, concrete economic
   projections vs. none) — the levers a petitioner actually controls.
6. **Spend real judgment where it's cheap to.** The local model is noisier
   on `denial_reasons`/`dispositive_prong` than on the now-ground-truth
   `outcome` field. Once filtered to "decisions like mine" (#3), that
   subset is small enough to reclassify with Claude Code's own judgment
   instead of Ollama — full accuracy where it matters, without paying for
   the whole corpus.

## Notes

- Set a real contact address in the `User-Agent` in `fetch_decisions.py` and
  keep the polite 2s delay — this is public government data, but be a good
  citizen toward uscis.gov.
- `data/seen.json` makes runs incremental; safe to schedule weekly (Claude
  Code Routines works well for this).
- The classification schema lives in `taxonomy.json`; extend the code list as
  you observe new recurring grounds.
