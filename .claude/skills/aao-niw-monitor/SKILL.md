---
name: aao-niw-monitor
description: Monitor USCIS AAO non-precedent NIW (EB-2, Form I-140) decisions. Fetch new decision PDFs, extract text, classify denial reasons against the Dhanasar framework, and maintain a running dataset plus trend report. Trigger on "run the AAO monitor", "check for new NIW decisions", "classify AAO decisions", or "update the NIW denial report".
---

# AAO NIW Decision Monitor

## Pipeline

1. **Fetch** — `python scripts/fetch_decisions.py --since 2026-01-01` (defaults to since=2026-01-01, until=today, 5 listing pages). New PDFs land in `data/pdfs/`; `data/seen.json` dedupes. The script reads `robots.txt` for the crawl delay, backs off on 429/503, and skips out-of-window PDFs by parsing the date straight out of the filename — no extra request per file.
2. **Extract** — `python scripts/extract_text.py`. Text files land in `data/text/`. PDFs already carry an OCR layer; the script falls back to Tesseract only if a page is a raw scan.
3. **Classify** — For each `data/text/*.txt` without a matching `data/results/*.json`: read the full text, then classify it using the schema in `taxonomy.json`. Write one JSON object per decision to `data/results/<stem>.json`.
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

Set this up as a **Claude Code Routine** (weekly is plenty — USCIS says decisions are typically posted within a month of being issued, so daily gains nothing and just adds load to their server). The routine should run steps 1–4 in order and summarize *only the new decisions* and any shift in the top denial reasons. Steps 3–4 need Claude's judgment, so this pipeline can't run as a pure background cron job — it needs to run through Claude Code (or be split, see README "Running unattended").
