# Glossary

What every term and column in this project actually means — for reading
`data/results/summary.csv`, `REPORT.md`, or the per-decision JSON files in
`data/results/`.

## The legal process, in order

1. **NIW** (National Interest Waiver) — a discretionary waiver of the job
   offer / labor certification requirement normally attached to an EB-2
   immigrant visa petition, available if the petitioner shows the waiver is
   in the national interest.
2. **EB-2** — the employment-based second-preference immigrant visa
   category (advanced degree professionals / individuals of exceptional
   ability) that an NIW petition rides on. Form **I-140** is the actual
   petition filed.
3. **SCOPS** (Service Center Operations) — the original USCIS office that
   adjudicates the I-140 petition and issues the initial denial, if any.
   SCOPS's decision is *not* what this dataset analyzes directly.
4. **AAO** (Administrative Appeals Office) — the appellate body that
   reviews a SCOPS denial on appeal. **The AAO's decision is the document
   every row in this dataset comes from.** When `outcome` says `sustained`,
   that's the AAO reversing SCOPS; `dismissed` is the AAO upholding SCOPS.
5. **Dhanasar** (*Matter of Dhanasar*, 26 I&N Dec. 884 (AAO 2016)) — the
   controlling precedent decision that set the 3-prong test every NIW case
   is decided under (see below). Cited constantly in the source PDFs.
6. **Bagamasbad reservation** — when AAO decides a case on one prong and
   explicitly declines to rule on the others ("we need not, and do not,
   reach..."), citing *INS v. Bagamasbad*. Tracked in `prongs_reserved` —
   important because a dismissal on Prong 1 alone says nothing about
   whether the petitioner would have satisfied Prongs 2 or 3.

## The Dhanasar three-prong test

Every NIW petition must show all three to win. `dispositive_prong` records
*which one* actually sank (or, for `sustained` cases, which weren't an
issue) the petition:

| Prong | What it requires |
|---|---|
| **1** | The proposed endeavor has both **substantial merit** and **national importance**. |
| **2** | The petitioner is **well-positioned to advance** the endeavor (education, skills, track record, concrete plan). |
| **3** | On balance, it would **benefit the United States** to waive the job offer/labor certification requirement. |

`dispositive_prong` values: `1`, `2`, `3`, `multiple` (more than one prong
failed independently), or `none` (petition was sustained — no prong sank
it).

## Column-by-column (`summary.csv` header → meaning)

| CSV header | Underlying field | Meaning |
|---|---|---|
| Case ID | `case_id` | The AAO's own case number, from the decision's "In Re:" line. |
| Decision Date | `decision_date` | Date AAO issued the decision. Read directly from the source filename (ground truth — not left to AI judgment). |
| Occupation (as stated) | `occupation` | The petitioner's occupation exactly as described in the decision text — free text, genuinely varies petitioner to petitioner. |
| Occupation Category | `occupation_category` | A broad bucket (`software_technology_it`, `healthcare_medicine`, etc.) derived from Occupation via keyword rules, for filtering to "decisions in my field." See `scripts/classify_queue.py::categorize_occupation`. |
| Endeavor Type | `endeavor_type` | `entrepreneur_business_plan` \| `employed_professional` \| `researcher_academic` \| `other` — the general shape of the proposed endeavor. |
| Outcome | `outcome` | `dismissed` (petitioner loses, SCOPS denial upheld) \| `sustained` (petitioner wins, denial reversed) \| `remanded` (sent back to SCOPS for a new decision) \| `withdrawn_moot`. Read directly from the decision's own `ORDER:` line — ground truth, not AI judgment. |
| Decisive Prong | `dispositive_prong` | Which Dhanasar prong (see above) was dispositive. |
| Denial Reason Codes | `denial_reasons` | Zero or more short codes (see next section) tagging *why* — semicolon-separated in the CSV. |
| Reason (Plain English) | `reason_summary` | 2-3 sentences explaining the actual holding in this specific case — read this instead of decoding codes. |
| Key AAO Quotes | `key_quotes` | Up to 2 short verbatim quotes from the AAO's own decision text (not SCOPS's original denial) pinning the holding. |
| Lessons for Future Petitions | `lessons` | 1-3 actionable takeaways for what a future petition should do differently. |

## Denial reason codes (`denial_reasons`)

Each code names a specific, recurring failure pattern. Prefix tells you
which prong it belongs to:

- **`P1_*`** — Prong 1 (substantial merit / national importance) failures, e.g. `P1_NATIONAL_IMPORTANCE_NOT_SHOWN` (impact limited to employer/clients/region), `P1_FIELD_VS_ENDEAVOR_CONFLATION` (argued the *profession* matters, not the *specific endeavor*), `P1_SHORTAGE_ARGUMENT_REJECTED`, `P1_DEPRESSED_AREA_ARGUMENT_REJECTED`.
- **`P2_*`** — Prong 2 (well-positioned to advance) failures, e.g. `P2_POSITIONING_INSUFFICIENT`, `P2_PLAN_VAGUE_OR_NOT_ACTIONABLE`.
- **`P3_*`** — Prong 3 (balance favors the U.S.) failures, e.g. `P3_BALANCE_NOT_IN_US_FAVOR`.
- **`EVIDENCE_*`** — evidentiary problems that undercut an otherwise-plausible argument, e.g. `EVIDENCE_GENERIC_LETTERS` (support letters read as templated/conclusory), `EVIDENCE_INSUFFICIENT_CORROBORATION`.
- **`PROCEDURAL_*`** — process grounds unrelated to the merits, e.g. `PROCEDURAL_NEW_EVIDENCE_ON_APPEAL` (late evidence not considered).

The authoritative, up-to-date code list with descriptions lives in
`taxonomy.json`'s `fields.denial_reasons`.

## Data-quality notes worth knowing

- `outcome` and `decision_date` are **not** left to AI judgment — both are
  corrected against deterministic ground truth (the decision's own
  `ORDER:` line, and the source filename) after classification, since the
  small local model was found to be unreliable on exactly these two
  fields. See `scripts/classify_queue.py` for the override logic.
- `occupation_category` is likewise deterministic (keyword rules), not
  asked of the model — a batched AI categorization pass was tried first
  and discarded for being unreliable at scale.
- Everything else (`denial_reasons`, `dispositive_prong`,
  `reason_summary`, `key_quotes`, `lessons`) is genuine AI judgment from
  the local model and carries the usual caveat: individual-decision
  accuracy has some noise, though aggregate patterns across the corpus are
  directionally reliable.
