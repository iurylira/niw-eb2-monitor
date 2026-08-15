#!/usr/bin/env python3
"""Rebuild data/results/summary.csv from data/results/*.json.

Purely mechanical aggregation (no Claude/tokens needed) — one row per
classified decision. REPORT.md synthesis (patterns, trends) still needs
Claude's judgment and is done separately by the skill.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "data" / "results"
SUMMARY_FILE = RESULTS_DIR / "summary.csv"

FIELDS = [
    "case_id", "decision_date", "occupation", "endeavor_type", "outcome",
    "dispositive_prong", "denial_reasons", "reason_summary",
    "key_quotes", "lessons",
]


def main() -> None:
    rows = []
    for path in sorted(RESULTS_DIR.rglob("*.json")):
        if path.name == "summary.csv":
            continue
        try:
            d = json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"[warn] {path.name}: invalid JSON, skipping")
            continue
        rows.append({
            "case_id": d.get("case_id", ""),
            "decision_date": d.get("decision_date", ""),
            "occupation": d.get("occupation", ""),
            "endeavor_type": d.get("endeavor_type", ""),
            "outcome": d.get("outcome", ""),
            "dispositive_prong": d.get("dispositive_prong", ""),
            "denial_reasons": ";".join(d.get("denial_reasons", []) or []),
            "reason_summary": d.get("reason_summary", ""),
            "key_quotes": " | ".join(d.get("key_quotes", []) or []),
            "lessons": " | ".join(d.get("lessons", []) or []),
        })
    rows.sort(key=lambda r: r["decision_date"] or "")

    with SUMMARY_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[done] wrote {len(rows)} row(s) -> {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
