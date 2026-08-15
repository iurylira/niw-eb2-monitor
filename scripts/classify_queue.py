"""Classify queued AAO decisions using the first available backend.

Preference order:
1. local Ollama + Qwen (if available)
2. Claude Code CLI (if installed)

This keeps the project working with either backend while preserving the existing
Claude-based workflow as a fallback.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.fetch_decisions import parse_filename_date  # noqa: E402

QUEUE_FILE = ROOT / "data" / "queue.json"
TAXONOMY_FILE = ROOT / "taxonomy.json"
TXT_DIR = ROOT / "data" / "text"
RESULTS_DIR = ROOT / "data" / "results"
SUMMARY_FILE = RESULTS_DIR / "summary.csv"
REPORT_FILE = ROOT / "REPORT.md"

# qwen2.5:3b supports a 32768-token context, but Ollama's /api/chat defaults
# to 2048 unless num_ctx is set explicitly -- without this, any decision
# text long enough to overflow 2048 tokens gets silently truncated from the
# front (dropping the schema/instructions) and the model returns `{}`.
OLLAMA_NUM_CTX = 32768
# Longest decision text seen so far is ~36.6k chars; this cap gives headroom
# for longer future decisions while staying well inside OLLAMA_NUM_CTX
# (~4 chars/token, so 80000 chars is ~20k tokens, leaving room for the
# schema/instructions and the model's own output).
MAX_TEXT_CHARS = 80000


def results_path_for(item: dict) -> Path:
    """Result JSON path for a queue item, mirroring the <year>/<unknown>
    subfolder its text_file lives under (itself mirrored from data/pdfs/ by
    extract_text.py) so data/results/ stays partitioned the same way."""
    text_path = ROOT / item["text_file"]
    year_dir = RESULTS_DIR / text_path.parent.relative_to(TXT_DIR)
    year_dir.mkdir(parents=True, exist_ok=True)
    return year_dir / f"{item['stem']}.json"


def resolve_backend(status: dict[str, bool], preferred: str = "auto") -> str:
    if preferred not in {"ollama", "auto", "claude"}:
        raise ValueError(f"Unsupported backend preference: {preferred}")

    if preferred == "ollama":
        if status.get("ollama"):
            return "ollama"
        raise RuntimeError("Ollama selected but unavailable")

    if preferred == "claude":
        if status.get("claude"):
            return "claude"
        raise RuntimeError("Claude selected but unavailable")

    if status.get("ollama"):
        return "ollama"
    if status.get("claude"):
        return "claude"
    raise ValueError("No supported AI backend is available (Ollama/Qwen or Claude)")


def detect_ollama() -> dict[str, bool]:
    try:
        from scripts.ollama_support import check_local_ollama
    except ImportError:
        return {"ollama": False, "claude": False}
    check = check_local_ollama()
    return {"ollama": bool(check.get("available")), "claude": detect_claude()}


def detect_claude() -> bool:
    try:
        proc = subprocess.run(["claude", "--help"], capture_output=True, text=True, timeout=20)
        return proc.returncode == 0
    except Exception:
        return False


# A proper denial_reasons code looks like P1_NATIONAL_IMPORTANCE_NOT_SHOWN --
# found empirically that the model sometimes ignores this convention and
# writes a full free-text sentence into the array instead (e.g. "the record
# did not establish..."). Each unique sentence then gets treated as a
# brand-new "code" by sync_taxonomy_with_results, polluting taxonomy.json
# (one run added 86 sentence-like entries against ~14 real codes). Since
# reason_summary already captures the actual reasoning in prose for every
# decision, dropping non-conforming entries here loses no real information
# -- it just keeps denial_reasons as actual reusable codes.
DENIAL_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def normalize_classification_payload(payload: dict) -> dict:
    normalized = {
        "case_id": str(payload.get("case_id", "")),
        "decision_date": str(payload.get("decision_date", "")),
        "file": str(payload.get("file", "")),
        "occupation": str(payload.get("occupation", "")),
        "endeavor_type": str(payload.get("endeavor_type", "")),
        "outcome": str(payload.get("outcome", "")),
        "eb2_classification_met": bool(payload.get("eb2_classification_met", False)),
        "dispositive_prong": str(payload.get("dispositive_prong", "")),
        "prongs_reserved": payload.get("prongs_reserved") or [],
        "denial_reasons": payload.get("denial_reasons") or [],
        "key_quotes": payload.get("key_quotes") or [],
        "reason_summary": str(payload.get("reason_summary", "")),
        "lessons": payload.get("lessons") or [],
    }
    if isinstance(normalized["key_quotes"], str):
        normalized["key_quotes"] = [normalized["key_quotes"]]
    if isinstance(normalized["lessons"], str):
        normalized["lessons"] = [normalized["lessons"]]
    if isinstance(normalized["denial_reasons"], str):
        normalized["denial_reasons"] = [normalized["denial_reasons"]]
    normalized["denial_reasons"] = [
        r for r in normalized["denial_reasons"] if DENIAL_CODE_RE.match(str(r).strip())
    ]
    return normalized


def _taxonomy_reason_code(value: str) -> str:
    raw = str(value).strip()
    if not raw:
        return ""
    code = raw.split(" - ", 1)[0].strip()
    if not code:
        return raw
    return code


_DENIAL_CODE_PREFIXES = ("P1_", "P2_", "P3_", "EVIDENCE_", "PROCEDURAL_")


def _code_suffix(code: str) -> str:
    for prefix in _DENIAL_CODE_PREFIXES:
        if code.startswith(prefix):
            return code[len(prefix):]
    return code


def _resolve_denial_reason_code(code: str, known_codes: set[str]) -> str:
    """Map a model-produced code onto an existing taxonomy code when it's
    clearly the same denial ground under the wrong prong prefix (a common
    small-model mistake -- e.g. 'P3_SHORTAGE_ARGUMENT_REJECTED' instead of
    the existing 'P1_SHORTAGE_ARGUMENT_REJECTED', since shortage arguments
    are a Prong 1 concept) rather than treating it as a brand-new denial
    pattern and permanently forking the taxonomy with a near-duplicate."""
    if code in known_codes:
        return code
    suffix = _code_suffix(code)
    for existing in known_codes:
        if existing != code and _code_suffix(existing) == suffix:
            return existing
    return code


def sync_taxonomy_with_results(
    results: list[dict],
    taxonomy: dict | None = None,
    taxonomy_path: str | Path | None = None,
) -> dict:
    if not TAXONOMY_FILE.exists():
        taxonomy = {"fields": {"denial_reasons": []}}
    elif not taxonomy:
        taxonomy = json.loads(TAXONOMY_FILE.read_text())

    denial_reasons = taxonomy.get("fields", {}).get("denial_reasons", []) or []
    reason_values = list(denial_reasons)
    known_reasons = {
        _taxonomy_reason_code(entry) for entry in reason_values if _taxonomy_reason_code(entry)
    }
    new_reasons = []

    for item in results:
        reasons = item.get("denial_reasons", []) or []
        if isinstance(reasons, str):
            reasons = [reasons]
        resolved = []
        for reason in reasons:
            code = _taxonomy_reason_code(reason)
            if not code:
                continue
            canonical = _resolve_denial_reason_code(code, known_reasons)
            if canonical == code and code not in known_reasons:
                # No existing code shares this suffix under a different
                # prefix -- a genuinely new denial pattern.
                known_reasons.add(code)
                new_reasons.append(code)
            resolved.append(canonical)
        item["denial_reasons"] = resolved

    if new_reasons:
        for reason in new_reasons:
            if reason not in reason_values:
                reason_values.append(reason)

    taxonomy.setdefault("fields", {})["denial_reasons"] = reason_values

    if taxonomy_path is not None:
        path = Path(taxonomy_path)
        path.write_text(json.dumps(taxonomy, indent=2) + "\n")

    return taxonomy


# Every AAO decision ends with an unambiguous "ORDER: The appeal is
# dismissed/sustained." line -- ground truth for `outcome`, and far more
# reliable than asking a small model to infer it from the surrounding prose.
# A 3B model was found (empirically, on this corpus) to persistently confuse
# "sustained" (petitioner wins) with decisions that merely discuss or
# reference sustaining the *original officer's* reasoning -- retrying the
# same prompt twice on one case reproduced the same wrong answer both times.
# This regex-based override is checked second (order matters, since a
# withdrawn-for-remand order line always also contains "withdraw"):
#   dismiss > sustain > remand > withdraw/moot
# NOTE: the withdrawn_moot branch is untested against this corpus -- every
# "withdraw" occurrence observed so far co-occurred with "remand" (a
# withdrawn-for-remand order) and fell through to `remanded` correctly. If a
# genuine moot-dismissal order line ever surfaces with different phrasing,
# this branch may need adjusting.
_ORDER_LINE_RE = re.compile(r"ORDER:\s*(.+?)(?:\n|$)", re.IGNORECASE)


# Broad occupation buckets for filtering "decisions like mine" (see README
# roadmap #3). Deliberately deterministic keyword rules, not another AI
# call -- a batched Ollama pass over ~800 unique occupation strings was
# tried first and was unreliable at scale (defaulted ~18% to "other"
# including clear misses like "Data Engineer", "civil engineer and
# researcher"), the same batch-scale unreliability seen elsewhere in this
# pipeline. Order matters: more specific categories are checked first so
# e.g. "biomedical engineer" resolves via ENGINEERING (checked last) only
# if nothing more specific already matched.
OCCUPATION_CATEGORY_RULES = [
    ("software_technology_it", r"\b(software|information technology|\bIT\b|data (engineer|scientist)|cybersecurity|network (engineer|security)|computer science|programmer|developer|full[- ]stack|devops|cloud (engineer|architect)|database administrator|systems? (analyst|engineer|administrator)|web develop|\bUX\b|UI designer|machine learning engineer|AI engineer|artificial intelligence (engineer|research|manufactur)|QA (automation|tester)|IT (program|project) manager|technical (lead|architect)|information security|data (protection|security)|infrastructure specialist|informatics|telecommunications|advanced computing)"),
    ("healthcare_medicine", r"\b(physician|surgeon|nurse|medical|medicine|dentist|pharmacist|psychiatrist|psycholog|therapist|physiotherap|clinical|nutritionist|dietitian|veterinar|healthcare|hospital|radiolog|anesthesi|dermatolog|pediatric|oncolog|cardiolog|health promoter)"),
    ("life_sciences_research", r"\b(researcher|scientist|biolog|biostatistic|biomedical|biochemist|geneticist|microbiolog|immunolog|neuroscien|epidemiolog|toxicolog|postdoctoral|post-doctoral|research (fellow|associate|assistant)|academic researcher|chemist|physicist|statistician|mathematician|pharmaceutical scien)"),
    ("legal", r"\b(lawyer|attorney|legal|law (firm|clerk)|paralegal|counsel\b)"),
    ("finance_accounting", r"\b(accountant|accounting|financ|\bCPA\b|actuar|auditor|banking|investment|economist|quantitative analyst|operations research analyst|tax professional|treasurer|controller)"),
    ("education_academia", r"\b(teacher|professor|instructor|educator|academia|lecturer|tutor|principal\b|school (administrator|counselor)|teaching assistant)"),
    ("arts_media_design", r"\b(artist|musician|music|singer|designer|design\b|film|media|photograph|writer|journalist|actor|actress|dancer|architect\b|fashion|broadcast|communications professional|curator)"),
    ("agriculture_environmental_science", r"\b(agricultur|agronom|farm|environmental|forestry|horticultur|fisher|veterinary science|soil scien|geolog|sustainab|climate|renewable energy|clean energy)"),
    ("business_entrepreneurship_management", r"\b(CEO|COO|entrepreneur|founder|business (owner|analyst|intelligence|development|consultant|administrat|operations|continuit)|manager\b|management (consult|analyst)|marketing|sales|project manager|general manager|executive\b|consultant\b|human resources|logistic|supply chain|franchis)"),
    ("engineering_non_software", r"\b(engineer|engineering|mechanic\b|technician\b|electrician)"),
]


def categorize_occupation(occupation: str) -> str:
    low = (occupation or "").lower()
    if not low or "member of the profession" in low or "member ofthe profession" in low \
       or "individual of exceptional ability" in low or "alien of exceptional ability" in low \
       or low.startswith("none") or "not specified" in low:
        return "other"
    for category, pattern in OCCUPATION_CATEGORY_RULES:
        if re.search(pattern, low, re.IGNORECASE):
            return category
    return "other"


def order_line_outcome(text: str) -> str | None:
    m = _ORDER_LINE_RE.search(text)
    if not m:
        return None
    order_line = m.group(1).lower()
    if "dismiss" in order_line:
        return "dismissed"
    if "sustain" in order_line:
        return "sustained"
    if "remand" in order_line:
        return "remanded"
    if "withdraw" in order_line or "moot" in order_line:
        return "withdrawn_moot"
    return None


def _json_extract(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def _call_ollama(model: str, prompt: str) -> dict:
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"num_ctx": OLLAMA_NUM_CTX},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You classify AAO NIW decisions strictly using the JSON schema "
                    "described by the user. Return only valid JSON without markdown."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    result = subprocess.run(
        [
            "curl", "-sS", "-X", "POST", "http://127.0.0.1:11434/api/chat",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(payload),
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Ollama call failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    content = data.get("message", {}).get("content", "")
    return _json_extract(content)


def classify_batch_with_ollama(batch: list[dict]) -> list[dict]:
    if not batch:
        return []

    from scripts.ollama_support import check_local_ollama
    check = check_local_ollama()
    model = check.get("model", "qwen2.5:3b")

    taxonomy = (
        json.loads(TAXONOMY_FILE.read_text())
        if TAXONOMY_FILE.exists()
        else {"fields": {"denial_reasons": []}}
    )

    results = []
    total = len(batch)
    for i, item in enumerate(batch, 1):
        text_path = ROOT / item["text_file"]
        text = text_path.read_text(errors="replace")
        # Rebuilt every iteration (not hoisted out of the loop) so a newly
        # discovered denial code from an earlier decision in this same run
        # is already visible in the schema by the time we prompt for the
        # next one, instead of waiting for a whole separate run to pick it up.
        schema_text = json.dumps(taxonomy.get("fields", {}), indent=2)
        prompt = (
            "Classify this AAO NIW (EB-2) decision. Return ONLY valid JSON whose keys "
            "and allowed values follow this schema exactly (use the literal enum values "
            "given, e.g. lowercase outcome, numeric-or-'none' dispositive_prong, and "
            "denial_reasons codes exactly as spelled):\n\n"
            f"{schema_text}\n\n"
            "Use only the evidence in the text. If information is missing, use empty "
            "strings or empty arrays as appropriate.\n\nFILE: "
            f"{text_path.name}\n\nTEXT:\n{text[:MAX_TEXT_CHARS]}"
        )
        print(f"[ollama] ({i}/{total}) {text_path.name}")

        # A single bad response (malformed JSON, a subprocess timeout on an
        # unusually long decision, a dropped connection, ...) from a small
        # local model shouldn't sink an entire multi-hundred-item run --
        # retry once, then fall back to a marked-error stub so the batch
        # always finishes and every other decision's result is preserved.
        payload = None
        for attempt in range(2):
            try:
                raw = _call_ollama(model, prompt)
                payload = normalize_classification_payload(raw)
                payload["classification_status"] = "classified_by_ollama"
                break
            except Exception as exc:
                print(f"[warn] {text_path.name}: attempt {attempt + 1} failed: {exc}")
        if payload is None:
            payload = normalize_classification_payload({})
            payload["classification_status"] = "ollama_error"

        payload["file"] = text_path.name
        payload["stem"] = item["stem"]

        # Ground-truth override: trust the decision's own ORDER: line over
        # the model's outcome field whenever it resolves unambiguously (see
        # order_line_outcome docstring above for why this exists).
        ground_truth_outcome = order_line_outcome(text)
        if ground_truth_outcome and payload.get("outcome") != ground_truth_outcome:
            payload["outcome"] = ground_truth_outcome

        # Same idea for decision_date: AAO filenames encode the decision
        # date directly (e.g. JUL082026_10B5203.pdf -> 2026-07-08) and this
        # is already trusted as ground truth elsewhere in the pipeline
        # (fetch_decisions.py's date-window filtering). Found empirically:
        # the model sometimes garbles this field -- most often swapping the
        # filename's month for a different one it read out of the decision
        # text (e.g. mistaking "JUL" for "JAN"/"MAR" in prose elsewhere in
        # the document) -- so don't trust its free-form date parsing either.
        ground_truth_date = parse_filename_date(text_path.name)
        if ground_truth_date and payload.get("decision_date") != str(ground_truth_date):
            payload["decision_date"] = str(ground_truth_date)

        payload["occupation_category"] = categorize_occupation(payload.get("occupation", ""))

        # Sync the taxonomy against this one decision immediately, not once
        # at the end of the whole batch -- a new PDF can surface a genuinely
        # new denial pattern at any point, and a run stopped partway through
        # should leave taxonomy.json (and this decision's own codes) already
        # up to date rather than waiting on decisions that haven't run yet.
        # This also canonicalizes any wrong-prong-prefix code (e.g. a model
        # saying 'P3_SHORTAGE_ARGUMENT_REJECTED' for what's actually the
        # existing 'P1_SHORTAGE_ARGUMENT_REJECTED') before it's persisted.
        previous_codes = set(taxonomy.get("fields", {}).get("denial_reasons", []))
        taxonomy = sync_taxonomy_with_results([payload], taxonomy=taxonomy, taxonomy_path=TAXONOMY_FILE)
        new_codes = [
            c for c in taxonomy.get("fields", {}).get("denial_reasons", [])
            if c not in previous_codes
        ]
        if new_codes:
            print(f"[taxonomy] new denial reason(s) from {text_path.name}: " + ", ".join(new_codes))

        results.append(payload)

        # Persist as we go, not just at the end of the whole batch -- if the
        # process is killed or an unforeseen error slips past the retry loop
        # above, everything classified so far up to this point is still on
        # disk and won't be reclassified (or lost) on the next run.
        results_path_for(item).write_text(json.dumps(payload, indent=2))
    return results


def classify_batch_with_claude(batch: list[dict]) -> list[dict]:
    if not batch:
        return []
    results = []
    for item in batch:
        payload = {
            "stem": item["stem"],
            "file": item["stem"] + ".pdf",
            "classification_status": "queued_for_claude",
        }
        results.append(payload)
    return results


# Internal dict keys (used throughout the codebase, taxonomy.json, and every
# data/results/*.json file) stay short/technical -- only the CSV's own
# header row gets human-readable labels, applied at export time. See
# GLOSSARY.md for what each of these actually means.
SUMMARY_HEADER_LABELS = {
    "case_id": "Case ID",
    "decision_date": "Decision Date",
    "occupation": "Occupation (as stated)",
    "occupation_category": "Occupation Category",
    "endeavor_type": "Endeavor Type",
    "outcome": "Outcome",
    "dispositive_prong": "Decisive Prong",
    "denial_reasons": "Denial Reason Codes",
    "reason_summary": "Reason (Plain English)",
    "key_quotes": "Key AAO Quotes",
    "lessons": "Lessons for Future Petitions",
}
SUMMARY_FIELDS = list(SUMMARY_HEADER_LABELS)


def write_summary(results: list[dict]) -> None:
    rows = []
    for item in results:
        sort_key = item.get("decision_date", "")
        rows.append((sort_key, {
            SUMMARY_HEADER_LABELS["case_id"]: item.get("case_id", ""),
            SUMMARY_HEADER_LABELS["decision_date"]: item.get("decision_date", ""),
            SUMMARY_HEADER_LABELS["occupation"]: item.get("occupation", ""),
            SUMMARY_HEADER_LABELS["occupation_category"]: item.get("occupation_category", ""),
            SUMMARY_HEADER_LABELS["endeavor_type"]: item.get("endeavor_type", ""),
            SUMMARY_HEADER_LABELS["outcome"]: item.get("outcome", ""),
            SUMMARY_HEADER_LABELS["dispositive_prong"]: item.get("dispositive_prong", ""),
            SUMMARY_HEADER_LABELS["denial_reasons"]: ";".join(item.get("denial_reasons", []) or []),
            SUMMARY_HEADER_LABELS["reason_summary"]: item.get("reason_summary", ""),
            SUMMARY_HEADER_LABELS["key_quotes"]: " | ".join(item.get("key_quotes", []) or []),
            SUMMARY_HEADER_LABELS["lessons"]: " | ".join(item.get("lessons", []) or []),
        }))
    rows.sort(key=lambda pair: pair[0] or "")

    with SUMMARY_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(SUMMARY_HEADER_LABELS.values()))
        writer.writeheader()
        writer.writerows(row for _, row in rows)


# Cap the synthesis prompt to a bounded sample so it stays well inside
# OLLAMA_NUM_CTX regardless of how large the corpus grows -- an evenly
# spaced sample (by date) is used instead of "most recent N" so the model
# sees patterns across the whole time range, not just the latest batch.
SYNTHESIS_MAX_SAMPLE = 150
SYNTHESIS_SUMMARY_CHARS = 200


def _synthesis_sample(results: list[dict], max_sample: int = SYNTHESIS_MAX_SAMPLE) -> list[dict]:
    dismissed = [
        r for r in results
        if r.get("outcome") == "dismissed" and r.get("reason_summary")
    ]
    dismissed.sort(key=lambda r: r.get("decision_date", ""))
    if len(dismissed) <= max_sample:
        return dismissed
    step = len(dismissed) / max_sample
    return [dismissed[int(i * step)] for i in range(max_sample)]


def synthesize_patterns_with_ollama(results: list[dict]) -> dict | None:
    """Ask the local model to find cross-cutting patterns across many
    decisions' reason_summary text -- things several decisions have in
    common in HOW they reasoned, not just which taxonomy code fired.
    Returns None (report section is simply omitted) if Ollama isn't
    available or the corpus has no dismissed decisions with a
    reason_summary yet, rather than failing the whole report."""
    sample = _synthesis_sample(results)
    if not sample:
        return None

    from scripts.ollama_support import check_local_ollama
    check = check_local_ollama()
    model = check.get("model")
    if not model:
        return None

    reason_counts: dict[str, int] = {}
    for item in results:
        for reason in item.get("denial_reasons", []) or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    freq_lines = "\n".join(
        f"- {reason}: {count}"
        for reason, count in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    entries = []
    for item in sample:
        summary = (item.get("reason_summary") or "")[:SYNTHESIS_SUMMARY_CHARS]
        codes = ",".join(item.get("denial_reasons", []) or [])
        entries.append(
            f"- [{item.get('decision_date', '')}] prong={item.get('dispositive_prong', '')} "
            f"codes=[{codes}] :: {summary}"
        )
    entries_text = "\n".join(entries)

    prompt = (
        "You are analyzing a corpus of AAO NIW (EB-2, I-140) denial decisions to find "
        "cross-cutting PATTERNS that go beyond the fixed taxonomy codes below -- things "
        "several decisions have in common in HOW they reasoned, not just which code fired "
        "on each one individually.\n\n"
        f"Known denial-reason code frequencies across the full corpus ({len(results)} decisions):\n"
        f"{freq_lines}\n\n"
        f"Sample of {len(sample)} dismissed-decision reason summaries (spread across the full "
        f"date range):\n{entries_text}\n\n"
        "Return ONLY valid JSON with this exact shape:\n"
        '{"patterns": ["3-6 bullets, each describing a recurring pattern you observe across '
        'multiple decisions in the sample, citing roughly how many/what fraction show it"], '
        '"candidate_new_denial_patterns": ["0-3 bullets naming a genuinely new failure pattern '
        "that recurs but isn't well captured by any existing code above -- omit entirely "
        '(empty list) if nothing new stands out"]}'
    )

    for attempt in range(2):
        try:
            raw = _call_ollama(model, prompt)
            patterns = raw.get("patterns") or []
            candidates = raw.get("candidate_new_denial_patterns") or []
            if isinstance(patterns, str):
                patterns = [patterns]
            if isinstance(candidates, str):
                candidates = [candidates]
            return {
                "patterns": [str(p) for p in patterns],
                "candidate_new_denial_patterns": [str(c) for c in candidates],
                "sample_size": len(sample),
            }
        except Exception as exc:
            print(f"[warn] pattern synthesis attempt {attempt + 1} failed: {exc}")
    return None


def _denial_reason_descriptions() -> dict[str, str]:
    """Map each denial_reasons code to its human-readable description, parsed
    straight from taxonomy.json's 'CODE - description' entries -- so the
    report's code list stays in sync with the taxonomy without duplicating
    the descriptions in a second place."""
    if not TAXONOMY_FILE.exists():
        return {}
    taxonomy = json.loads(TAXONOMY_FILE.read_text())
    descriptions = {}
    for entry in taxonomy.get("fields", {}).get("denial_reasons", []) or []:
        code, sep, desc = entry.partition(" - ")
        if sep:
            descriptions[code.strip()] = desc.strip()
    return descriptions


def write_report(results: list[dict]) -> None:
    if not results:
        REPORT_FILE.write_text("# NIW Decision Report\n\nNo classified decisions available.\n")
        return

    recent = sorted(results, key=lambda r: r.get("decision_date", ""), reverse=True)[:10]
    by_outcome = {}
    by_prong = {}
    reason_counts = {}
    for item in results:
        outcome = str(item.get("outcome", "unknown"))
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1

        prong = str(item.get("dispositive_prong", "unknown"))
        by_prong[prong] = by_prong.get(prong, 0) + 1

        for reason in item.get("denial_reasons", []) or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    lines = [
        "# NIW Decision Report",
        "",
        "## Overview",
        f"- Total classified decisions: {len(results)}",
        "- Outcome counts: " + ", ".join(f"{k}={v}" for k, v in sorted(by_outcome.items())),
        "- Dispositive prong counts: " + ", ".join(f"{k}={v}" for k, v in sorted(by_prong.items())),
        "",
        "## Top denial reasons",
    ]
    reason_descriptions = _denial_reason_descriptions()
    for reason, count in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        desc = reason_descriptions.get(reason, "")
        suffix = f" — {desc}" if desc else ""
        lines.append(f"- {reason}: {count}{suffix}")

    synthesis = synthesize_patterns_with_ollama(results)
    if synthesis and synthesis["patterns"]:
        lines.extend([
            "",
            f"## Patterns & emerging themes (AI synthesis, n={synthesis['sample_size']} dismissed decisions)",
        ])
        lines.extend(f"- {p}" for p in synthesis["patterns"])
        if synthesis["candidate_new_denial_patterns"]:
            lines.extend(["", "### Candidate new denial patterns (not yet coded in taxonomy.json)"])
            lines.extend(f"- {c}" for c in synthesis["candidate_new_denial_patterns"])

    lines.extend(["", "## Most recent decisions"])
    for item in recent:
        lines.append(
            f"- {item.get('decision_date', '')} | {item.get('case_id', '')} | "
            f"{item.get('outcome', '')} | {item.get('dispositive_prong', '')} | "
            f"{item.get('occupation', '')}"
        )
        reason_summary = item.get("reason_summary", "")
        if reason_summary:
            lines.append(f"  {reason_summary}")

    REPORT_FILE.write_text("\n".join(lines) + "\n")


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if not QUEUE_FILE.exists():
        print(f"[warn] no queue at {QUEUE_FILE}; nothing to classify")
        return 0

    queue = json.loads(QUEUE_FILE.read_text())
    if not queue:
        print("[done] queue empty")
        return 0

    backend_status = detect_ollama()
    backend = resolve_backend(backend_status, "auto")
    print(f"[backend] using {backend}")

    if backend == "ollama":
        results = classify_batch_with_ollama(queue)
    else:
        results = classify_batch_with_claude(queue)

    taxonomy = (
        json.loads(TAXONOMY_FILE.read_text())
        if TAXONOMY_FILE.exists()
        else {"fields": {"denial_reasons": []}}
    )
    if results:
        previous_codes = set(taxonomy.get("fields", {}).get("denial_reasons", []))
        taxonomy = sync_taxonomy_with_results(results, taxonomy=taxonomy, taxonomy_path=TAXONOMY_FILE)
        new_codes = [
            code for code in taxonomy.get("fields", {}).get("denial_reasons", [])
            if code not in previous_codes
        ]
        if new_codes:
            print("[taxonomy] added new denial reasons: " + ", ".join(new_codes))

    # The Ollama path already persisted each result as it was produced (see
    # classify_batch_with_ollama / results_path_for) -- only the Claude stub
    # branch still needs writing here, and needs the original queue item to
    # know which <year> subfolder to mirror (stub payloads carry no
    # text_file of their own).
    queue_by_stem = {q["stem"]: q for q in queue}
    for item in results:
        if item.get("classification_status") == "classified_by_ollama":
            continue
        queue_item = queue_by_stem.get(item.get("stem"), {})
        if "text_file" in queue_item:
            out = results_path_for(queue_item)
        else:
            year_dir = RESULTS_DIR / "unknown"
            year_dir.mkdir(parents=True, exist_ok=True)
            out = year_dir / f"{item.get('stem')}.json"
        out.write_text(json.dumps(item, indent=2))

    # Aggregate over everything on disk, not just this run's batch -- the
    # Ollama path persists each result as it's produced (see
    # classify_batch_with_ollama), so a prior interrupted run's completed
    # decisions are already there and belong in the summary/report too.
    all_results = [
        json.loads(p.read_text()) for p in sorted(RESULTS_DIR.rglob("*.json"))
    ]
    write_summary(all_results)
    write_report(all_results)

    print(f"[done] wrote {len(results)} result(s) this run "
          f"({len(all_results)} total) -> {RESULTS_DIR}")
    print(f"[done] wrote {SUMMARY_FILE}")
    print(f"[done] wrote {REPORT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
