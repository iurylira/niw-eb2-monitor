#!/usr/bin/env python3
"""Extract text from downloaded AAO decision PDFs.

USCIS publishes these PDFs with an embedded OCR text layer, so pdfplumber
usually suffices. If a page yields almost no text (a raw scan), we fall
back to rasterizing the page and running Tesseract OCR.

Output: data/text/<name>.txt (one file per decision, skips existing).
"""
import io
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "data" / "pdfs"
TXT_DIR = ROOT / "data" / "text"
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


def main() -> None:
    TXT_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    done = 0
    for pdf_path in pdfs:
        out = TXT_DIR / (pdf_path.stem + ".txt")
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


if __name__ == "__main__":
    main()
