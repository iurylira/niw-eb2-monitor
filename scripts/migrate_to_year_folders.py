#!/usr/bin/env python3
"""One-time (but safe to re-run) migration: move any flat files directly
under data/pdfs/, data/text/, data/results/ into <year>/ subfolders,
matching the partitioning fetch_decisions.py / extract_text.py /
classify_queue.py now write by default. Idempotent -- files already inside
a subfolder are left alone, and a name collision at the destination is
reported rather than silently overwritten.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.fetch_decisions import parse_filename_date  # noqa: E402

PDF_DIR = ROOT / "data" / "pdfs"
TXT_DIR = ROOT / "data" / "text"
RESULTS_DIR = ROOT / "data" / "results"


def year_for_name(name: str, decision_date: str | None = None) -> str:
    if decision_date:
        try:
            return decision_date.split("-")[0]
        except IndexError:
            pass
    fdate = parse_filename_date(name)
    return str(fdate.year) if fdate else "unknown"


def migrate_flat_files(directory: Path, get_year) -> int:
    moved = 0
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        if path.name == "summary.csv":
            continue
        year = get_year(path)
        dest_dir = directory / year
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        if dest.exists():
            print(f"[warn] collision, leaving flat: {path.name} (already exists at {dest})")
            continue
        path.rename(dest)
        moved += 1
    return moved


def main() -> None:
    pdf_moved = migrate_flat_files(PDF_DIR, lambda p: year_for_name(p.name))
    print(f"[done] pdfs moved: {pdf_moved}")

    txt_moved = migrate_flat_files(TXT_DIR, lambda p: year_for_name(p.name))
    print(f"[done] text files moved: {txt_moved}")

    def result_year(p: Path) -> str:
        try:
            d = json.loads(p.read_text())
        except json.JSONDecodeError:
            d = {}
        return year_for_name(p.name, d.get("decision_date"))

    results_moved = migrate_flat_files(RESULTS_DIR, result_year)
    print(f"[done] results moved: {results_moved}")


if __name__ == "__main__":
    main()
