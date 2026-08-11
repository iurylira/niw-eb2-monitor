# AAO NIW Decision Monitor

An agent that watches USCIS AAO non-precedent decisions for Form I-140 NIW
(EB-2), downloads new decision PDFs, extracts their text, and classifies the
denial reasoning against the *Dhanasar* framework — producing a dataset and a
trend report you can use to strengthen your own NIW petition.

## Architecture

```
USCIS listing (uri_1=18) ──> fetch_decisions.py ──> data/pdfs/*.pdf
                                                        │
                              extract_text.py  <────────┘
                              (pdfplumber + Tesseract fallback)
                                    │
                              data/text/*.txt
                                    │
                    Claude Code (skill: aao-niw-monitor)
                    classifies per taxonomy.json
                                    │
                data/results/*.json + summary.csv + REPORT.md
```

Deterministic work (HTTP, PDF, OCR) is plain Python; judgment work
(classification, lessons, trend synthesis) is done by Claude Code itself via
the skill in `.claude/skills/aao-niw-monitor/`. Because classification runs
inside Claude Code, it's covered by your Pro subscription — no separate API
key or per-token billing. (A standalone `anthropic` SDK script would need an
API key with its own usage-based billing, so the skill route is the
cost-efficient one for Pro.)

## Setup

```bash
pip install requests beautifulsoup4 pdfplumber pytesseract pillow
# macOS: brew install tesseract     (only needed for the rare raw-scan PDF)
```

## Run

Inside Claude Code, from this folder:

> run the AAO monitor

or manually:

```bash
python scripts/fetch_decisions.py --pages 2
python scripts/extract_text.py
# then ask Claude Code to classify data/text and rebuild REPORT.md
```

## Date window

By default `fetch_decisions.py` pulls decisions from **2026-01-01 to today**,
read straight off each PDF's filename (e.g. `FEB252026_02B5203.pdf` → Feb 25,
2026) — no per-file request needed to check the date. Override with:

```bash
python scripts/fetch_decisions.py --since 2026-01-01 --until 2026-08-11
```

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
