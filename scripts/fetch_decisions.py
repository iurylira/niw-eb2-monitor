#!/usr/bin/env python3
"""Fetch AAO non-precedent NIW (I-140) decisions from USCIS.

Scrapes the AAO decisions listing (uri_1=18 = Form I-140 NIW), finds PDF
links, and downloads any not already seen within a date window. Maintains
data/seen.json as a dedupe index so repeated runs only pull new decisions.
Also checks data/pdfs/ directly before downloading, so a missing/corrupt/
reset seen.json can't cause an already-downloaded PDF to be silently
re-fetched and overwritten.

Politeness / good-crawler behavior:
  - Reads robots.txt and honors its Crawl-delay (falls back to a
    conservative 10s default if none is published, per common .gov practice).
  - Backs off on 429/503 using the Retry-After header when present.
  - Adds small random jitter on top of the delay so requests aren't
    perfectly periodic.
  - Filters by decision date using the filename itself (e.g.
    FEB252026_02B5203.pdf), so out-of-window decisions are skipped without
    an extra request per file.
  - Stops paginating once a listing page's decisions are entirely older
    than --since, on the assumption the listing is sorted newest-first
    (use --no-early-stop to disable if that assumption ever looks wrong).

Usage:
    python scripts/fetch_decisions.py --since 2026-01-01
    python scripts/fetch_decisions.py --since 2026-01-01 --until 2026-08-11
    python scripts/fetch_decisions.py --pages 5   # backfill further back
"""
import argparse
import json
import random
import re
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = "https://www.uscis.gov"
LISTING = (
    BASE
    + "/administrative-appeals/aao-decisions/aao-non-precedent-decisions"
    + "?uri_1=18&m=All&y=1&text-search-new-container=&items_per_page=100"
)
HEADERS = {
    # Identify yourself politely; USCIS serves this data publicly.
    "User-Agent": "aao-niw-research/1.0 (personal research; contact: iury.lira@gmail.com)"
}
ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "data" / "pdfs"
SEEN_FILE = ROOT / "data" / "seen.json"
DEFAULT_CRAWL_DELAY = 10.0  # seconds, used if robots.txt publishes none

FNAME_DATE_RE = re.compile(r"^([A-Z]{3})(\d{2})(\d{4})_")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_filename_date(name: str) -> date | None:
    """Extract the decision date from an AAO filename, e.g. FEB252026_xxx.pdf."""
    m = FNAME_DATE_RE.match(name)
    if not m:
        return None
    mon, day, year = m.groups()
    month = MONTHS.get(mon.upper())
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def get_crawl_delay(session: requests.Session) -> float:
    """Read robots.txt and return the Crawl-delay for our user-agent, or a
    conservative default if the site doesn't publish one."""
    rp = RobotFileParser()
    try:
        resp = session.get(f"{BASE}/robots.txt", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        rp.parse(resp.text.splitlines())
        delay = rp.crawl_delay("*")
        if delay:
            return float(delay)
    except requests.RequestException:
        pass
    return DEFAULT_CRAWL_DELAY


def robots_allows(session: requests.Session, url: str) -> bool:
    rp = RobotFileParser()
    try:
        resp = session.get(f"{BASE}/robots.txt", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(HEADERS["User-Agent"], url)
    except requests.RequestException:
        # If robots.txt is unreachable, don't block on it, but stay polite.
        return True


def polite_sleep(base_delay: float) -> None:
    time.sleep(base_delay + random.uniform(0, 1.0))


def get_with_backoff(session: requests.Session, url: str, base_delay: float, **kw) -> requests.Response:
    for attempt in range(4):
        resp = session.get(url, headers=HEADERS, timeout=kw.get("timeout", 30))
        if resp.status_code in (429, 503):
            wait = float(resp.headers.get("Retry-After", base_delay * (2 ** attempt)))
            print(f"[backoff] {resp.status_code} on {url}, waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def list_pdf_links(session: requests.Session, base_delay: float, page: int = 0) -> list[str]:
    url = LISTING + (f"&page={page}" if page else "")
    resp = get_with_backoff(session, url, base_delay)
    soup = BeautifulSoup(resp.text, "html.parser")
    links = []
    for a in soup.select("a[href]"):
        href = a["href"]
        if re.search(r"\.pdf(\?|$)", href, re.IGNORECASE):
            links.append(urljoin(BASE, href))
    return list(dict.fromkeys(links))  # de-dupe, preserve order


def download(session: requests.Session, url: str, dest: Path, base_delay: float) -> None:
    resp = get_with_backoff(session, url, base_delay, timeout=60)
    dest.write_bytes(resp.content)


def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except json.JSONDecodeError:
            print(f"[warn] {SEEN_FILE} is corrupt, starting fresh")
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=5, help="max listing pages to scan")
    ap.add_argument("--since", type=str, default="2026-01-01", help="earliest decision date (YYYY-MM-DD)")
    ap.add_argument("--until", type=str, default=None, help="latest decision date (YYYY-MM-DD), default today")
    ap.add_argument("--no-early-stop", action="store_true", help="scan all --pages even past the --since window")
    args = ap.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d").date()
    until = datetime.strptime(args.until, "%Y-%m-%d").date() if args.until else date.today()

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    seen = load_seen()
    session = requests.Session()

    if not robots_allows(session, LISTING):
        print("[stop] robots.txt disallows this path — not crawling.")
        return
    base_delay = get_crawl_delay(session)
    print(f"[info] using crawl delay {base_delay:.0f}s (from robots.txt or default)")
    print(f"[info] date window: {since} .. {until}")

    new = 0
    for page in range(args.pages):
        try:
            links = list_pdf_links(session, base_delay, page)
        except requests.RequestException as exc:
            print(f"[warn] failed to fetch listing page {page}: {exc}")
            break
        polite_sleep(base_delay)

        page_dates = []
        for url in links:
            name = url.rsplit("/", 1)[-1].split("?")[0]
            fdate = parse_filename_date(name)
            if fdate:
                page_dates.append(fdate)
            if fdate and not (since <= fdate <= until):
                continue  # outside window, skip without downloading
            if name in seen:
                continue
            dest = PDF_DIR / name
            if dest.exists():
                # Already on disk even though seen.json doesn't know about it
                # (e.g. seen.json was deleted/corrupt/reset) — backfill the
                # index instead of re-downloading and overwriting the file.
                seen[name] = {"url": url, "fetched": time.strftime("%Y-%m-%d"), "decision_date": str(fdate) if fdate else None}
                continue
            try:
                download(session, url, dest, base_delay)
                seen[name] = {"url": url, "fetched": time.strftime("%Y-%m-%d"), "decision_date": str(fdate) if fdate else None}
                new += 1
                print(f"[new] {name}")
                polite_sleep(base_delay)
            except requests.RequestException as exc:
                print(f"[warn] failed {url}: {exc}")
        save_seen(seen)

        print(f"[info] page {page}: {len(links)} links, newest-to-oldest dates seen: "
              f"{max(page_dates) if page_dates else '?'} .. {min(page_dates) if page_dates else '?'}")

        if not args.no_early_stop and page_dates and max(page_dates) < since:
            print("[info] entire page is older than --since; stopping early (assumes newest-first listing)")
            break

    print(f"[done] {new} new decision(s) downloaded within window -> {PDF_DIR}")


if __name__ == "__main__":
    main()
