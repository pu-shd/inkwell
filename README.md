# inkwell

Presidio-based scrubber for sensitive records. It reads a records folder **read-only** and
writes a scrubbed, pseudonymized copy, preserving file and folder modification times.

## What it removes

| Kind | How |
|---|---|
| SSNs and ID numbers | Presidio `US_SSN` + custom `SSN_LIKE` (any `###-##-####`, valid or not), 9-digit IDs, labeled fields (`SSN:`, `ID number`, `EMPLID`, plus `INKWELL_EXTRA_ID_LABELS`) incl. value on the next OCR line |
| Biometric data | Faces in images/scans blacked out (OpenCV); documents about fingerprints/biometrics withheld |
| DOB, place of birth, mother's maiden name | Labeled-field recognizer (`Date of Birth`, `DOB`, `born in`, `Place of Birth`, `Maiden Name`, `née`) |
| Names & addresses of record subjects and their families | Presidio `PERSON`/`LOCATION`, street/PO box/ZIP patterns, labeled fields (`Father's name`, `Parent/Guardian`, `Home address`…), **every subject name harvested from the folder names**, and display names from each email's headers |
| Protected record types | Document classifier (`classify.py`) with a withholding profile; matching documents (PDFs: pages) are **withheld** and replaced by a placeholder. The built-in example profile covers education records: grades, transcripts, schedules, disciplinary files, financial aid and special-education plans |
| Also | email addresses, phone numbers, passport/bank/card numbers, SEVIS IDs/A-numbers; PDF/Office metadata, EXIF, email routing headers, HTML scripts/comments |

## Output

* PDFs (text or scanned) are rendered at `INKWELL_DPI`, OCR'd (Tesseract, with orientation
  detection), black-boxed and **flattened**: nothing under a box survives. An invisible text
  layer of the *remaining* words keeps them searchable.
* Images: same OCR + box + face pipeline, re-encoded in the original format.
* `.eml`: bodies redacted (`<PERSON>`, `<US_SSN>`…), address headers replaced, attachments
  recursively scrubbed and renamed. `.docx`/`.xlsx`/`.html`/`.txt`/`.zip` are scrubbed in place
  in their own formats (spreadsheet columns with PII headers are blanked wholesale).
* Paths: `Cohort 2019-20/<Person>/<file>` → `Cohort 2019-20/S-7KQ2M9XHC/D-4TB8R2NW6KQ.pdf`. Top-level
  group folders matching `INKWELL_GROUP_DIR_REGEX` (default: names ending in a year range such as
  `2019-20`) are kept; each folder below one is treated as one record subject.
* Withholding is **per page** for PDFs: flagged pages become a `WITHHELD` placeholder page;
  a PDF whose every page is flagged (and any flagged email/Office file) is withheld whole.
* Dropped (never copied): `.js .css .svg .download`, `.DS_Store`, `Thumbs.db`.
  Unsupported (`.doc`, encrypted PDFs) and failed files are **not written** — fail closed.
* `_inkwell_report.csv` / `_inkwell_summary.json` in the output: per-file status, category,
  entity counts. Pseudonymous paths only, never values.
* **Leak check**: every output is re-read; any residual SSN pattern or known subject name blocks
  the file (`leak_blocked`) instead of writing it.

## Identifiers and re-identification

Everything that links output back to people lives in `INKWELL_KEY_FILE`'s directory
(mode 700, files 600, required to be outside both input and output):

| File | One row per | Columns |
|---|---|---|
| `registry.csv` | **record subject** | `student_id`, `canonical_name`, `aliases`, `class_years`, `folders`, `institution_id`, `needs_review` |
| `key.csv` | file / dir / zip member / attachment | `original_path`, `pseudonym_path`, `student_ids` (owners), `mentions`, `sha256_original`, `sha256_output` |

(Column and entity names such as `student_id` and `KNOWN_STUDENT_NAME` come from the built-in
example profile; they hold any record subject.)

* **Random IDs.** `S-` subjects, `D-` documents, `F-` other folders: cryptographically random
  Crockford base32 with a check character (`ids.is_valid`). They carry no ordering, so they
  can't be matched to an alphabetical list of names.
* **Person, not folder.** The registry merges a person's folders across group folders
  (flagged `needs_review` in case two people share a name) and attaches loose top-level
  zips/folders named after them. `canonical_name`, `aliases` and `institution_id` may be edited by
  hand; edits survive re-runs, and new aliases are used for matching and in-text linking.
* **Fingerprints.** SHA-256 of each original and output. A renamed or moved original keeps its
  document ID (identical copies that both exist get separate IDs); `sha256_output` verifies a
  scrubbed file came from its original.
* **In-text pseudonyms.** A known subject's name becomes `<S-…>` in text formats; on scans the ID
  is printed white on the black box and added to the text layer. Partial names ("Ms. Roe")
  resolve only when unambiguous or when exactly one candidate owns the document; otherwise
  `<KNOWN_STUDENT_NAME>`. Every document's resolved names are listed in `key.csv` `mentions`.
  Trade-off: anyone holding the output can link one person's documents to each other (not to
  the person), which also makes the output somewhat easier to re-identify from context.

Reassociating: find the person in `registry.csv` → `student_id` → output folder
`*/S-…/` plus every `key.csv` row whose `student_ids` or `mentions` contains it.
Destroying the key directory makes the output non-reidentifiable by these files.

## Install

From a release (needs Python 3.12 and Tesseract; the spaCy model, about 0.6 GB, comes with it):

```zsh
brew install tesseract uv
uv tool install https://github.com/pu-shd/inkwell/releases/download/vX.Y.Z/inkwell-X.Y.Z-py3-none-any.whl
inkwell --version
```

Each release also has the sdist, `SHA256SUMS` and build-provenance attestations
(`gh attestation verify <file> -R pu-shd/inkwell`), and a container image
`ghcr.io/pu-shd/inkwell:vX.Y.Z` (Tesseract included; run it network-isolated, as
`scripts/docker-run.sh` does). Configuration is read from `INKWELL_*` environment variables
(see `example.env`). From a clone, use `scripts/setup.sh` instead.

## Usage

```zsh
scripts/setup.sh                  # .venv + locked deps, creates .env from example.env
$EDITOR .env
scripts/run.sh --plan-only        # fast: counts by type for the selection, writes nothing
scripts/run.sh --dry-run          # full simulation, writes only a private report (see below)
scripts/run.sh --limit 50         # pilot
scripts/run.sh                    # full run; re-runnable, skips finished files (--force to redo)
scripts/run.sh --pause            # from another terminal: pause / --resume / --stop (see below)
scripts/run.sh --audit            # after a recognizer fix: re-check existing outputs, delete any
                                  # that fail (plus images), then run again to redo just those
scripts/docker-run.sh             # same, in a network-isolated container
scripts/test.sh                   # lint + mypy + tests in Docker (CI); --local for .venv
```

### Site-specific settings

| Variable | Purpose | Example |
|---|---|---|
| `INKWELL_GROUP_DIR_REGEX` | top-level grouping folders; one capturing group = year label | `^Cohort (\d{4}-\d{2})$` |
| `INKWELL_EXTRA_ID_LABELS` | extra form labels that precede an ID number | `XUID,Member ID` |
| `INKWELL_EXTRA_NAME_NOISE` | words in folder names that are not names (codes, departments) | `msc,ba` |

### Permitted names

Names of people who may stay visible (staff, officials, signatories…) go in a CSV beside the
key file, set with `INKWELL_PERMITTED_NAMES_FILE`:

```csv
name,aliases,emails,phones
Ada Lovelace,"Augusta Ada King;A. Lovelace",ada@example.org,(555) 555-0100
```

* Full names only, matched in either order and ignoring case and titles (`Dr. Ada Lovelace`,
  `Lovelace, Ada`). A lone surname or first name stays redacted.
* Record subjects always win: an entry that is also a registry person is dropped at startup
  (counted as `permitted_conflicts` in the summary, listed in `permitted-conflicts.csv` beside the
  key), and any span overlapping a known subject stays redacted.
* Only names are kept. With `INKWELL_PERMITTED_CONTACTS=true`, the listed emails and phone
  numbers of permitted people are kept too; nothing else is.
* Kept names are counted as `PERMITTED_NAME` (and `PERMITTED_CONTACT`) in the report.
* `--suggest-permitted [--min-students N]` drafts a list from full names that appear in the
  files of at least N different registry people (default 3), from email display names and the
  text already in each document (no OCR). It writes `permitted-suggestions-<time>.csv` beside
  the key, in the same format plus evidence columns, and applies nothing. Review it, delete rows,
  then point `INKWELL_PERMITTED_NAMES_FILE` at it.
* Suggestions are only name-shaped text: at least two full words, no form labels, degrees,
  markup, garbled encodings, initials-only forms, salutations or citations. Variants differing only
  in initials share one row. Detection is still imprecise (streets, buildings, product names and
  OCR fragments can get through); put site-specific words in `INKWELL_EXTRA_NAME_NOISE`, and
  always review the list.

### Permitted places

The place equivalent of permitted names: institutional place names (campus, buildings, the
institution's town) that may stay visible, in a CSV set with `INKWELL_PERMITTED_PLACES_FILE`:

```csv
place,aliases
Springfield,"Springfield, Ohio;Springfield OH"
Wexford Hall,
```

* Matched against whole `LOCATION` results (case, punctuation and a leading "the" ignored), or
  against comma/line-separated parts of one. Single words are allowed. Digits are not, so
  street addresses and postal codes always stay redacted.
* Only `LOCATION` results are unmasked. A place in a labeled field (place of birth, home or
  campus address), a street-address match, or a span overlapping a record subject's name stays
  redacted. Counted as `PERMITTED_PLACE`; the summary has `permitted_places`.
* Trade-off: a permitted town is also visible where it is part of someone's own address outside
  a labeled field.
* `--suggest-permitted` also writes `permitted-places-suggestions-<time>.csv`. Recurrence alone
  doesn't separate the institution's places from hometowns and countries of origin, which also
  recur across people, so a place must appear in the files of at least `--min-place-share`
  percent of all registry people (default 8, and never fewer than `--min-students`). On one
  reviewed corpus every rejected place was below that share and the institution's places above
  it. Form labels ("Home City"), dotted abbreviations, other institutions and subject words are
  never suggested. A place seen in the labeled place-of-birth or address fields of half or more
  of its people is left out (counted in the log); `evidence_personal_students` counts the rest.
  Review the list as you would the names list.

### Selecting by date

`--modified-after/--modified-before` and `--created-after/--created-before` take a date
(`2019-09-01`), a local date-time (`2019-09-01T08:30`) or an age (`30d`, `12h`, `2w`). *After* is
inclusive, *before* exclusive; flags combine. Creation time is the file's birth time on this disk
(when it was downloaded/copied here, not when the document was written) and exists only on
macOS/BSD: in Docker/Linux `--created-*` stops with an error rather than matching nothing.

### Dry run

`--dry-run` does everything a real run does (OCR, classification, per-page withholding,
redaction, leak check) but writes nothing to the output folder, key or registry. It writes one
CSV, `dryrun-<time>.csv` beside the key file (or `--report PATH`), mode 600, with
`original_path, pseudonym_path, pseudonym_is_new, kind, would_status, category, entities,
notes, modified, created`. It lists **original paths** (which contain names), so it is refused
inside the input or output folder. Values are never written. It takes as long as a real run.

### Pausing, stopping and resuming

| To | Do |
|---|---|
| pause for a while (keeps everything in memory) | `scripts/run.sh --pause` from another terminal, or Ctrl-Z in the run's terminal |
| continue a paused run | `scripts/run.sh --resume` (or `fg` after Ctrl-Z) |
| stop (e.g. before a restart) | Ctrl-C, or `scripts/run.sh --stop` from another terminal |
| continue after a stop, crash or power loss | run the same command again |

A stopped run saves every finished file's key data, removes half-written files and exits with
code `130`. A rerun skips files whose output exists **and** whose result is in the key, so a file
that was written but not yet recorded is simply redone. Only one run at a time may use a key
folder (`inkwell.lock` there; a lock left by a crash is taken over). A dry run can be paused but not
resumed after a stop: its report only means something as one complete pass. Sleep is safe: work
continues on wake (`scripts/run.sh` keeps the Mac from idle-sleeping while it runs).

### Redaction and withholding switches

| Flags | Withheld records | Everything else | Warning |
|---|---|---|---|
| *(default)* | placeholder | redacted, leak-checked | — |
| `--no-withhold` | redacted, readable, noted `flagged:<category>` | redacted, leak-checked | `WITHHOLDING DISABLED` |
| `--no-redact` | placeholder | **copied unchanged** (status `copied`, no leak check) | `REDACTION DISABLED` |
| both | refused | | |

The warning is logged at start and end and written to `_inkwell_WARNING.txt` in the output.
An output folder holds one mode (`_inkwell_mode.json`); switching modes against an existing
output requires `--force` without `--limit` or date filters (or a new `INKWELL_OUTPUT_DIR`).

Exit codes: `0` ok · `1` config error · `2` some files errored/leak-blocked (see report) ·
`3` no input / nothing selected.

### Releasing

1. `uv version X.Y.Z`; rename `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z]`; merge to `main`.
2. `scripts/release.sh --dry-run X.Y.Z` runs every check, the Docker test suite and a local build.
3. `scripts/release.sh X.Y.Z` does the same, then pushes tag `vX.Y.Z`. The `release` workflow
   tests, builds, checks the distributions (`scripts/check_dist.py` allows only listed files),
   installs the wheel into a clean environment, and publishes the GitHub release and image.

## Limits — human review still required

* Handwriting and very degraded scans OCR poorly; pages flagged `low_ocr_confidence` in the
  report should be eyeballed. Unrecognized text can't be redacted.
* `PERSON` redacts every detected name except permitted ones (see *Permitted names*).
  `LOCATION` redacts all places except permitted ones (see *Permitted places*).
* The withhold classifier is keyword-based. It is tuned to over-withhold (award letters
  that mention a fellowship or stipend count as financial aid in the example profile). Adjust
  `classify.py` or `INKWELL_WITHHOLD_THRESHOLD`.
* Pseudonymized links between saved HTML pages and their assets no longer resolve.
