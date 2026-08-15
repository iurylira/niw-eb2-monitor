#!/usr/bin/env python3
"""Rebuild data/results/summary.csv from data/results/**/*.json.

Purely mechanical aggregation (no Claude/tokens needed) — one row per
classified decision. REPORT.md synthesis (patterns, trends) still needs
Claude's judgment and is done separately by the skill.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.classify_queue import SUMMARY_FILE, RESULTS_DIR, write_summary  # noqa: E402


def main() -> None:
    results = []
    for path in sorted(RESULTS_DIR.rglob("*.json")):
        try:
            results.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            print(f"[warn] {path.name}: invalid JSON, skipping")
            continue

    write_summary(results)
    print(f"[done] wrote {len(results)} row(s) -> {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
