# Changelog

All notable changes to inkwell. Versions follow [Semantic Versioning](https://semver.org/);
each release is tagged `vX.Y.Z` and published on the GitHub Releases page.

## [Unreleased]

### Added
- `inkwell-db` scrubs Microsoft Access databases (`.mdb`, `.accdb`, read with mdbtools) into SQLite
  and CSV with the same registry and IDs as the documents. Every column has a declared treatment
  (key, drop, withhold, keep, code, faculty, text); an unknown column stops the run. People found
  only in a database are added to the registry for review; `db-key.csv` links source records to
  `R-` record IDs. Outputs are re-read and leak-checked before they are published.
- `scripts/run-db.sh` and `scripts/docker-run-db.sh`; the container image includes mdbtools.
- `settings.json` in the key folder records the settings that shape the key (noise words, ID labels,
  year-folder pattern, detection settings, permitted files by SHA-256). `inkwell` and `inkwell-db`
  refuse a real run with different settings unless `--accept-settings` (the old record is kept);
  dry runs warn. `inkwell --save-settings` records them without a run.

## [0.2.0] - 2026-11-06

### Added
- Permitted names (#2): `INKWELL_PERMITTED_NAMES_FILE` keeps listed people's full names visible;
  `INKWELL_PERMITTED_CONTACTS` optionally keeps their listed emails and phone numbers;
  `--suggest-permitted [--min-students N]` drafts a list for review.
- Permitted places: `INKWELL_PERMITTED_PLACES_FILE` keeps listed institutional place names
  visible where the NER found a location (never in labeled personal fields or addresses);
  `--suggest-permitted` also drafts a places list from places that reach a large share of all
  record subjects (`--min-place-share`, default 8%), leaving out form labels, abbreviations and
  places mostly seen as someone's birthplace or address.
- Math is preserved: LaTeX math regions in text formats and math-font text in PDFs are not
  redacted as names, places or IDs (strong identifiers such as SSNs still are).
- `--folders FILE` selects top-level input folders from a list; combines with the date flags.
- Pause, stop and resume: `--pause`, `--resume` and `--stop` control a running inkwell; Ctrl-C
  or `--stop` saves progress and a rerun continues where it stopped. One run per key folder
  (`inkwell.lock`); half-written files from a killed run are removed.
- Release packaging: wheel and sdist on GitHub Releases with SHA-256 checksums and build
  provenance, a container image on GHCR, `inkwell --version`.

### Fixed
- A permitted name or place sharing a word with a registry person's name is now a conflict (stays
  redacted) instead of being kept in email headers and then leak-blocking the whole file.
- Text containing unpaired surrogates no longer crashes analysis.

## [0.1.0]

Initial version: OCR-based redaction and flattening of PDFs and images, scrubbing of email,
Office, HTML, text and zip files, per-page withholding, pseudonymous paths and IDs, registry and
key files, leak check, dry run, date selection, and redaction/withholding switches.
