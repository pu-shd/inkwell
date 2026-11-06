# Changelog

All notable changes to inkwell. Versions follow [Semantic Versioning](https://semver.org/);
each release is tagged `vX.Y.Z` and published on the GitHub Releases page.

## [Unreleased]

### Added
- Permitted names (#2): `INKWELL_PERMITTED_NAMES_FILE` keeps listed people's full names visible;
  `INKWELL_PERMITTED_CONTACTS` optionally keeps their listed emails and phone numbers;
  `--suggest-permitted [--min-students N]` drafts a list for review.
- Release packaging: wheel and sdist on GitHub Releases with SHA-256 checksums and build
  provenance, a container image on GHCR, `inkwell --version`.

### Fixed
- Text containing unpaired surrogates no longer crashes analysis.

## [0.1.0]

Initial version: OCR-based redaction and flattening of PDFs and images, scrubbing of email,
Office, HTML, text and zip files, per-page withholding, pseudonymous paths and IDs, registry and
key files, leak check, dry run, date selection, and redaction/withholding switches.
