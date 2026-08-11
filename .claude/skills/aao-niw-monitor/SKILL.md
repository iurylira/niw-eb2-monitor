---
name: aao-niw-monitor
description: Monitor USCIS AAO non-precedent NIW (EB-2, Form I-140) decisions. Fetch new decision PDFs, extract text, classify denial reasons against the Dhanasar framework, and maintain a running dataset plus trend report. Trigger on "run the AAO monitor", "check for new NIW decisions", "classify AAO decisions", or "update the NIW denial report".
---

# AAO NIW Decision Monitor

## Pipeline

Steps 1–2 are plain Python, cost no tokens, and never invoke Claude — safe to
run unattended (cron/launchd) on their own schedule. Step 3 is the only step
that costs tokens, so it's deliberately decoupled from download time and
runs as a separate, explicitly-triggered, batched pass over a queue — never
"classify this PDF as soon as it lands."

1. **Fetch** — `python scripts/fetch_decisions.py --since 2026-01-01` (defaults to since=2026-01-01, until=today, 5 listing pages). New PDFs land in `data/pdfs/`; `data/seen.json` dedupes. The script reads `robots.txt` for the crawl delay, backs off on 429/503, and skips out-of-window PDFs by parsing the date straight out of the filename — no extra request per file.
2. **Extract** — `python scripts/extract_text.py`. Text files land in `data/text/`. PDFs already carry an OCR layer; the script falls back to Tesseract only if a page is a raw scan. At the end of the run it rebuilds `data/queue.json` — every `data/text/*.txt` that has no matching `data/results/*.json` yet — which is the backlog step 3 consumes. Extraction never classifies anything itself.
3. **Classify (batched)** — Read `data/queue.json`. If it's empty, skip straight to step 4. Otherwise process it in batches of **~12 decisions per batch** (tune down if the texts are unusually long, up if they're short — the point is amortizing one taxonomy/instructions pass over many decisions instead of paying that overhead per file):
   - Read the full text of each decision in the batch.
   - Classify all of them together against the schema in `taxonomy.json`, reasoning about the batch as a set.
   - Write one JSON object per decision to `data/results/<stem>.json`.
   - Move on to the next batch. If you stop before the queue is empty (context getting long, user wants to wrap up), that's fine — leave the remainder in place; the next `extract_text.py` run (or a manual re-scan) will regenerate `data/queue.json` with only the still-unclassified stems, so nothing is lost or double-processed.
4. **Aggregate** — Rebuild `data/results/summary.csv` (one row per decision: case_id, date, occupation, endeavor_type, outcome, dispositive_prong, denial_reasons joined by `;`) and refresh `REPORT.md`.

## Classification rules

- Anchor everything to the *Dhanasar* three-prong framework. Identify which prong was dispositive and which prongs the AAO reserved (Bagamasbad reservations matter — a "Prong 1 only" dismissal says nothing about the petitioner's Prong 2/3 strength).
- Distinguish "field vs. endeavor" conflation (the most common Prong 1 failure) from genuinely local-scope endeavors.
- Note whether EB-2 classification itself (advanced degree / exceptional ability) was conceded — it almost always is; the fight is the waiver.
- `key_quotes` must stay under 15 words each and be used only to pin the dispositive holding.
- `lessons` should be actionable for a software-engineering petitioner where possible (e.g., "quantify downstream adoption beyond the employer", "tie the endeavor to a named national initiative with evidence").

## Report (REPORT.md)

Rebuild after each run:
- Counts by outcome and by dispositive prong.
- Top 10 denial-reason codes with frequencies.
- A short "patterns for a strong petition" section synthesizing the `lessons` fields, weighted toward tech/engineering-adjacent occupations.
- Table of the 10 most recent decisions.

## Cadence

Set this up as a **Claude Code Routine** (weekly is plenty — USCIS says decisions are typically posted within a month of being issued, so daily gains nothing and just adds load to their server). The routine should run steps 1–4 in order and summarize *only the new decisions* and any shift in the top denial reasons.

Steps 1–2 (fetch/extract) don't need Claude at all and are safe to run more often, or unattended via cron, to keep `data/queue.json` topped up between routine runs (see README "Running unattended"). Steps 3–4 need Claude's judgment. Because step 3 is batched, a single routine run classifies whatever accumulated in the queue since last time — could be 1 decision or 30 — without paying per-decision overhead for each one individually.
