#!/usr/bin/env bash
# Fetch new AAO decision PDFs and extract their text in one shot.
#
# fetch_decisions.py resumes automatically from the latest decision date
# already recorded in data/seen.json (no --since needed), so this is safe
# to run as often as you like, by hand or from cron/launchd -- it always
# picks up exactly where the last run left off.
#
# Any arguments are passed through to fetch_decisions.py, e.g.:
#   scripts/update.sh --pages 10       # backfill further back
#   scripts/update.sh --since 2026-01-01 --until 2026-03-01   # explicit window
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="./.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="python3"

"$PYTHON" scripts/fetch_decisions.py "$@"
"$PYTHON" scripts/extract_text.py
