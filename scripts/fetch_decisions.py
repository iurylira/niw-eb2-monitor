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
    python scripts/fetch_decisions.py --pages 5        # backfill further back
    python scripts/fetch_decisions.py --year 2025      # a single historical year

The listing's year filter (`y=`) is NOT a fixed value -- e.g. y=1 currently
means "2026" but will mean "2027" once USCIS rolls the site's default over,
so it's resolved at runtime by parsing the listing page's own year <select>
options rather than hardcoded. Without --year, every calendar year spanned
by --since..--until is fetched in turn (newest year first).
"""
import argparse
import json
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = "https://www.uscis.gov"
LISTING_BASE = (
    BASE
    + "/administrative-appeals/aao-decisions/aao-non-precedent-decisions"
    + "?uri_1=18&m=All&text-search-new-container=&items_per_page=100"
)
YEAR_OPTION_RE = re.compile(r'<option value="(\d+)"[^>]*>(\d{4})</option>')
HEADERS = {
    # Identify yourself politely; USCIS serves this data publicly.
    "User-Agent": "aao-niw-research/1.0 (personal research; contact: iury.lira@gmail.com)"
}
ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "data" / "pdfs"
SEEN_FILE = ROOT / "data" / "seen.json"
DEFAULT_CRAWL_DELAY = 10.0  # seconds, used if robots.txt publishes none
DEFAULT_SINCE = "2026-01-01"  # fallback --since when seen.json has no history yet
# When resuming automatically, start a few days before the latest known
# decision instead of exactly on it -- the AAO listing can have same-day
# decisions posted out of order across runs, and seen.json/PDF-on-disk
# checks already make re-scanning this small window a no-op, not a re-download.
RESUME_OVERLAP_DAYS = 3

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


def list_pdf_links(session: requests.Session, base_delay: float, listing_url: str) -> list[str]:
    resp = get_with_backoff(session, listing_url, base_delay)
    soup = BeautifulSoup(resp.text, "html.parser")
    links = []
    for a in soup.select("a[href]"):
        href = a["href"]
        if re.search(r"\.pdf(\?|$)", href, re.IGNORECASE):
            links.append(urljoin(BASE, href))
    return list(dict.fromkeys(links))  # de-dupe, preserve order


def resolve_year_options(session: requests.Session, base_delay: float) -> dict[int, str]:
    """Fetch the listing page once and parse its year <select> into
    {calendar_year: y-param-value}. This can't be hardcoded -- USCIS's y=
    values are relative to "current year" (y=1 means 2026 today, but will
    mean 2027 once the site rolls over), confirmed by inspecting the
    listing's own <select name="y"> options directly."""
    resp = get_with_backoff(session, LISTING_BASE, base_delay)
    return {int(year): value for value, year in YEAR_OPTION_RE.findall(resp.text)}


def listing_url_for_year(y_value: str, page: int = 0) -> str:
    url = f"{LISTING_BASE}&y={y_value}"
    if page:
        url += f"&page={page}"
    return url


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


def pdf_dest(name: str, fdate: date | None) -> Path:
    """Destination path for a decision PDF, partitioned by decision year
    (data/pdfs/<year>/<name>.pdf) so the corpus stays browsable as it grows
    across multiple years. Filename-unparseable dates land in an 'unknown'
    bucket rather than blocking the download."""
    year_dir = PDF_DIR / (str(fdate.year) if fdate else "unknown")
    year_dir.mkdir(parents=True, exist_ok=True)
    return year_dir / name


def save_seen(seen: dict) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


def latest_seen_date(seen: dict) -> date | None:
    """Most recent decision_date recorded across data/seen.json, or None if
    there's no history yet (fresh checkout, or seen.json was reset)."""
    dates = []
    for entry in seen.values():
        raw = entry.get("decision_date")
        if not raw:
            continue
        try:
            dates.append(datetime.strptime(raw, "%Y-%m-%d").date())
        except ValueError:
            continue
    return max(dates) if dates else None


def fetch_year_pages(
    session: requests.Session,
    y_value: str,
    base_delay: float,
    since: date,
    until: date,
    seen: dict,
    args: argparse.Namespace,
) -> int:
    """Paginate one calendar year's listing (already resolved to its y=
    value), downloading anything new inside [since, until]. Returns the
    count of newly downloaded PDFs for this year."""
    new = 0
    for page in range(args.pages):
        listing_url = listing_url_for_year(y_value, page)
        try:
            links = list_pdf_links(session, base_delay, listing_url)
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
            dest = pdf_dest(name, fdate)
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

        if not links:
            print("[info] no links on this page; assuming end of this year's results")
            break

        if not args.no_early_stop and page_dates and max(page_dates) < since:
            print("[info] entire page is older than --since; stopping early for this year")
            break

    return new


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=5, help="max listing pages to scan per year")
    ap.add_argument(
        "--since", type=str, default=None,
        help="earliest decision date (YYYY-MM-DD). Default: resume automatically from "
             f"the latest date in data/seen.json (minus a {RESUME_OVERLAP_DAYS}-day overlap "
             f"buffer), or {DEFAULT_SINCE} if there's no history yet.",
    )
    ap.add_argument("--until", type=str, default=None, help="latest decision date (YYYY-MM-DD), default today")
    ap.add_argument(
        "--year", type=int, default=None,
        help="restrict to a single calendar year (e.g. 2025), resolved against the "
             "listing's own year filter at runtime. Default: every year spanned by "
             "--since..--until, newest first.",
    )
    ap.add_argument("--no-early-stop", action="store_true", help="scan all --pages even past the --since window")
    args = ap.parse_args()

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    seen = load_seen()

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
    elif args.year:
        # A --year backfill targets that whole calendar year -- the
        # auto-resume window (anchored to the *latest* known decision) makes
        # no sense here and would early-stop immediately against an older
        # --year request.
        since = date(args.year, 1, 1)
        print(f"[info] --year {args.year} given with no --since; defaulting to since={since}")
    else:
        latest = latest_seen_date(seen)
        if latest:
            since = latest - timedelta(days=RESUME_OVERLAP_DAYS)
            print(f"[info] no --since given; resuming from latest known decision "
                  f"({latest}) minus {RESUME_OVERLAP_DAYS}d overlap -> since={since}")
        else:
            since = datetime.strptime(DEFAULT_SINCE, "%Y-%m-%d").date()
            print(f"[info] no --since given and no history in {SEEN_FILE.name}; "
                  f"defaulting to since={since}")

    if args.until:
        until = datetime.strptime(args.until, "%Y-%m-%d").date()
    elif args.year:
        until = date(args.year, 12, 31)
    else:
        until = date.today()

    session = requests.Session()

    if not robots_allows(session, LISTING_BASE):
        print("[stop] robots.txt disallows this path — not crawling.")
        return
    base_delay = get_crawl_delay(session)
    print(f"[info] using crawl delay {base_delay:.0f}s (from robots.txt or default)")
    print(f"[info] date window: {since} .. {until}")

    print("[info] resolving available years from the listing's own year filter...")
    year_options = resolve_year_options(session, base_delay)
    polite_sleep(base_delay)
    if not year_options:
        print("[error] could not resolve year options from the listing page; aborting")
        return
    print(f"[info] listing offers years: {sorted(year_options, reverse=True)}")

    years_to_fetch = [args.year] if args.year else list(range(until.year, since.year - 1, -1))

    new = 0
    for year in years_to_fetch:
        y_value = year_options.get(year)
        if y_value is None:
            print(f"[warn] year {year} not offered by the listing; skipping")
            continue
        print(f"[info] --- year {year} (y={y_value}) ---")
        new += fetch_year_pages(session, y_value, base_delay, since, until, seen, args)

    print(f"[done] {new} new decision(s) downloaded within window -> {PDF_DIR}")


if __name__ == "__main__":
    main()
