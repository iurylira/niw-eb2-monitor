#!/usr/bin/env python3
"""Extract text from downloaded AAO decision PDFs.

USCIS publishes these PDFs with an embedded OCR text layer, so pdfplumber
usually suffices. If a page yields almost no text (a raw scan), we fall
back to rasterizing the page and running Tesseract OCR.

Output: data/text/<year>/<name>.txt, mirroring the <year> subfolder each
source PDF lives under in data/pdfs/ (one file per decision, skips existing).

This step never touches Claude/tokens, so it's safe to run unattended
(cron/launchd) right after fetch_decisions.py. It does NOT trigger
classification — it only queues extracted decisions for it. After each
run, data/queue.json is rebuilt from data/text/*.txt minus data/results/*.json,
so classification (a separate, batched, Claude-driven step) always has an
accurate backlog to pull from instead of being invoked synchronously here.
"""
import json
import time
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "data" / "pdfs"
TXT_DIR = ROOT / "data" / "text"
RESULTS_DIR = ROOT / "data" / "results"
QUEUE_FILE = ROOT / "data" / "queue.json"
MIN_CHARS_PER_PAGE = 200  # below this we assume the text layer is missing


def ocr_page(page) -> str:
    """Rasterize a pdfplumber page and OCR it with Tesseract."""
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return ""
    img = page.to_image(resolution=300).original
    return pytesseract.image_to_string(img)


def extract(pdf_path: Path) -> str:
    parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if len(text) < MIN_CHARS_PER_PAGE:
                ocr_text = ocr_page(page)
                if len(ocr_text) > len(text):
                    text = ocr_text
            parts.append(text)
    return "\n\n".join(parts)


def rebuild_queue() -> list[dict]:
    """Recompute the classification backlog: every extracted text file that
    doesn't yet have a matching result. Persisted so the (separate, batched,
    Claude-driven) classify step doesn't need to rescan directories and so
    the pending count survives between unattended fetch/extract runs and
    whenever classification is actually invoked."""
    classified_stems = {p.stem for p in RESULTS_DIR.rglob("*.json")}
    pending = []
    for txt_path in sorted(TXT_DIR.rglob("*.txt")):
        if txt_path.stem in classified_stems:
            continue
        pending.append({
            "stem": txt_path.stem,
            "text_file": str(txt_path.relative_to(ROOT)),
            "queued": time.strftime("%Y-%m-%d"),
        })
    QUEUE_FILE.write_text(json.dumps(pending, indent=2))
    return pending


def main() -> None:
    TXT_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(PDF_DIR.rglob("*.pdf"))
    done = 0
    for pdf_path in pdfs:
        # Mirror the PDF's <year>/<unknown> subfolder under data/text/, so
        # both stay partitioned the same way without extract_text.py needing
        # to re-derive the date itself.
        year_dir = TXT_DIR / pdf_path.parent.relative_to(PDF_DIR)
        year_dir.mkdir(parents=True, exist_ok=True)
        out = year_dir / (pdf_path.stem + ".txt")
        if out.exists():
            continue
        try:
            text = extract(pdf_path)
            out.write_text(text)
            done += 1
            print(f"[ok] {pdf_path.name} -> {out.name} ({len(text)} chars)")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {pdf_path.name}: {exc}")
    print(f"[done] extracted {done} new file(s) -> {TXT_DIR}")

    pending = rebuild_queue()
    print(f"[queue] {len(pending)} decision(s) awaiting classification -> {QUEUE_FILE}")


if __name__ == "__main__":
    main()
