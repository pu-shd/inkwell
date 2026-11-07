# inkwell

Scrubs a folder of sensitive records. It reads the input **read-only** and writes a redacted,
pseudonymized copy with the original modification times. A separate key folder links the copy back
to people.

## Quick start

```zsh
scripts/setup.sh                  # .venv with locked dependencies; creates .env from example.env
$EDITOR .env                      # input, output and key paths (see example.env)
scripts/run.sh --plan-only        # file counts for the selection; writes nothing
scripts/run.sh --dry-run          # full simulation; writes only a private report
scripts/run.sh                    # the real run; re-run to continue or to add new files
```

Other scripts: `scripts/docker-run.sh` (the same run in a network-isolated container),
`scripts/test.sh` (lint, types and tests in Docker; `--local` uses `.venv`).

To install a release instead of using a clone (needs Python 3.12 and Tesseract):

```zsh
brew install tesseract uv
uv tool install https://github.com/pu-shd/inkwell/releases/download/vX.Y.Z/inkwell-X.Y.Z-py3-none-any.whl
```

Each release also ships `SHA256SUMS`, provenance attestations
(`gh attestation verify <file> -R pu-shd/inkwell`) and the image `ghcr.io/pu-shd/inkwell:vX.Y.Z`.

## What it does

- **Redacts**:
  - SSNs and other ID numbers, including labeled ones (`SSN:`, `EMPLID`, plus
    `INKWELL_EXTRA_ID_LABELS`);
  - names, addresses, emails, phone numbers, dates and places of birth, maiden names;
  - passport, bank and card numbers;
  - faces in images;
  - metadata, EXIF data, email routing headers and HTML scripts.
- **Names of record subjects**:
  - Every name taken from the folder names is redacted wherever it appears.
  - In text formats it becomes the person's ID (`<S-…>`).
  - On scans, the ID is printed on the black box.
- **Withholds**:
  - A keyword classifier withholds protected record types.
  - The built-in profile covers grades, transcripts, financial aid, discipline and similar
    records.
  - In PDFs, only the flagged pages become a `WITHHELD` placeholder.
  - Any other flagged file is withheld whole.
- **PDFs and images**:
  - Each page is rendered, read with OCR (Tesseract), black-boxed and **flattened**, so nothing
    survives under a box.
  - The words that remain stay searchable through an invisible text layer.
- **Other formats**: email, Office, HTML, text and zip files are scrubbed in their own format.
  Email attachments and zip members are scrubbed the same way.
- **Fails closed**:
  - Every output is re-read before it is written.
  - A file with a residual SSN or subject name is not written; it is reported as `leak_blocked`.
  - Unsupported files (`.doc`, encrypted PDFs) and failed files are never written either.
- **Dropped**: web and system junk is never copied (`.js`, `.css`, `.svg`, fonts, `.DS_Store`,
  `Thumbs.db`, `._*` files).
- **Math**:
  - LaTeX math in text formats and math-font text in PDFs is left alone.
  - SSNs, emails and card numbers inside math are still redacted.
  - Formulas on scans are treated as ordinary text.

**Paths** are pseudonymized:

```
Cohort 2019-20/<Person>/<file>  →  Cohort 2019-20/S-7KQ2M9XHC/D-4TB8R2NW6KQ.pdf
```

- Top-level folders matching `INKWELL_GROUP_DIR_REGEX` keep their names. The default matches
  names that end in a year range such as `2019-20`.
- Each folder below one of those is treated as one person.
- IDs are random, carry no ordering and include a check character.

**Reports** in the output folder show pseudonymous paths only:
- `_inkwell_report.csv` holds every file's history across runs.
- `_inkwell_summary.json` covers the latest run only.

## The key folder

The key folder is the directory of `INKWELL_KEY_FILE`. It must be outside the input and output
folders; it has mode 700 and its files mode 600.

| File | Contents |
|---|---|
| `registry.csv` | One row per person: ID, name, aliases, class years, folders, institution ID, `needs_review` |
| `key.csv` | One row per file, folder or attachment: original path, pseudonym path, owners, names mentioned, input and output SHA-256 |

**Re-identifying a document:**
1. Find the person in `registry.csv`.
2. Use their ID to find the `*/S-…/` folder and every `key.csv` row that lists the ID.

**About the registry:**
- A person's folders are merged across years. A possible name clash is flagged `needs_review`.
- You can edit names, aliases and institution IDs by hand; the edits survive re-runs.

**About privacy:**
- Destroy the key folder and the output can't be re-identified from these files.
- Even without the key, anyone holding the output can tell which documents belong to the same
  person.

## Choosing files

| Flag | Selects |
|---|---|
| `--modified-before WHEN`, `--modified-after WHEN` | files by modification time |
| `--created-before WHEN`, `--created-after WHEN` | files by birth time on this disk; macOS only, refused on Linux |
| `--folders FILE` | top-level folders listed in FILE, one per line, `#` comments allowed |
| `--limit N` | at most N files (for a pilot) |
| `--force` | files already done, again |

- **`WHEN`** is a date (`2019-09-01`), a local time (`2019-09-01T08:30`) or an age (`30d`, `12h`,
  `2w`).
- **Date boundaries:** *after* includes the date given; *before* excludes it.
- **Combining:** flags combine with AND.

**Old folders with recent dates.** Folders whose file dates don't show the records' age (for
example, old records copied recently) are picked up by a second run with `--folders` into the same
output:

```zsh
scripts/run.sh --modified-before 2020-11-06
scripts/run.sh --folders ~/Documents/inkwell-key/old-folders.txt
```

**Rules for the folder list:**
- Keep the file outside the output folder.
- Errors name the line number, not the folder.

## Permitted names and places

Names of staff and officials, and the institution's own places, can stay visible. List them in
CSVs in the key folder:

```csv
name,aliases,emails,phones
Ada Lovelace,"Augusta Ada King;A. Lovelace",ada@example.org,(555) 555-0100
```
```csv
place,aliases
Springfield,"Springfield, Ohio"
```

Then point the settings at them:
- **Names:** set `INKWELL_PERMITTED_NAMES_FILE`.
  - Full names are matched in any order, ignoring case and titles.
  - A first name or surname on its own stays redacted.
  - Emails and phone numbers stay visible only if `INKWELL_PERMITTED_CONTACTS=true`.
- **Places:** set `INKWELL_PERMITTED_PLACES_FILE`.
  - Only detected locations are affected.
  - Entries can't contain digits, so street addresses and postal codes stay redacted.
  - Birthplace and address fields stay redacted.
  - A permitted town still shows when it appears in someone's address outside a labeled field.
- **Record subjects always win.** An entry that shares a word with any registry person's name is
  dropped at startup. Dropped entries are listed in `permitted-conflicts.csv` and
  `permitted-place-conflicts.csv`.

**Drafting the lists.** `scripts/run.sh --suggest-permitted` writes two drafts to the key folder:
`permitted-suggestions-<time>.csv` and `permitted-places-suggestions-<time>.csv`.

- **Names:** full names found in the files of at least `--min-students` people (default 3).
- **Places:** places found in the files of at least `--min-place-share`% of all people (default
  8). Hometowns recur too, so this threshold is higher.
- **Not suggested:**
  - form labels, initials-only names and abbreviations;
  - places that appear mostly in birthplace or address fields.
- **Always review the drafts.** Detection is imprecise, and streets, buildings and OCR fragments
  slip through. Add recurring junk words to `INKWELL_EXTRA_NAME_NOISE`.

## Running

**Dry run:**
- `--dry-run` does all the work but writes only `dryrun-<time>.csv` to the key folder (or
  `--report PATH`).
- The report lists **original paths**, so it is refused inside the input or output folder.
- It takes as long as a real run.

**Pause, stop and resume:**

| To | Do |
|---|---|
| pause | `scripts/run.sh --pause` from another terminal, or Ctrl-Z |
| continue a paused run | `scripts/run.sh --resume`, or `fg` |
| stop | `scripts/run.sh --stop`, or Ctrl-C (saves progress, exits with code 130) |
| continue after a stop or crash | run the same command again |

- **What counts as done:** a file is skipped only if its output exists **and** its key row is
  saved. Anything else is redone.
- **One run at a time:** only one run can use a key folder (`inkwell.lock`).
- **Sleep:** sleep is safe; `run.sh` keeps the Mac awake while it works.

**After a recognizer fix:** `--audit` re-checks existing outputs and deletes failures and images.
The next run redoes just those files.

**Reduced modes:**

| Flags | Effect |
|---|---|
| `--no-withhold` | Protected records are redacted, not withheld, and flagged in the report. |
| `--no-redact` | Non-withheld files are copied **unchanged**. |

- **Warnings:** either mode writes `_inkwell_WARNING.txt`.
- **No mixing:** an output folder keeps one mode; switching needs `--force` on a full run.
- **Not both:** the two flags can't be combined.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | ok |
| `1` | configuration error |
| `2` | some files errored or were leak-blocked (see the report) |
| `3` | nothing selected |
| `130` | stopped |

## Settings

All settings are `INKWELL_*` environment variables. `example.env` lists every one with a short
comment.

**Site-specific settings:**
- `INKWELL_GROUP_DIR_REGEX`: which top-level folders keep their names (year groups).
- `INKWELL_EXTRA_ID_LABELS`: extra labels that come before an ID.
- `INKWELL_EXTRA_NAME_NOISE`: words in folder names that are not names.

## Limits: a person still needs to review

- **OCR:** handwriting and poor scans OCR badly, and text that isn't recognized can't be redacted.
  Check files the report flags `low_ocr_confidence`.
- **Names and places:** every detected name and place is redacted, except permitted ones.
- **Withholding:** the classifier is keyword-based and leans toward withholding too much. Tune
  `classify.py` or `INKWELL_WITHHOLD_THRESHOLD`.
- **Saved web pages:** links between saved HTML pages and their assets break once paths are
  pseudonymized.

## Releasing

1. Run `uv version X.Y.Z`.
2. Rename `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z]`, then merge to `main`.
3. Run `scripts/release.sh --dry-run X.Y.Z`, then `scripts/release.sh X.Y.Z`.
   - The script runs every check, then tags.
   - The `release` workflow builds, verifies and publishes the release and the image.
