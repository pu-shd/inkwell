#!/bin/zsh
# Scrub Access databases natively. Usage: scripts/run-db.sh [--dry-run]
# Reads .env (INKWELL_DB_INPUT_DIR, INKWELL_DB_OUTPUT_DIR, INKWELL_KEY_FILE, permitted files).
# Never modifies the databases. Safe to re-run: IDs are kept in the key folder.
set -euo pipefail
cd "${0:A:h}/.."

[[ -f .env ]] || { echo ".env not found; run scripts/setup.sh first" >&2; exit 1; }
set -a; source ./.env; set +a
[[ -x .venv/bin/inkwell-db ]] || { echo ".venv missing; run scripts/setup.sh" >&2; exit 1; }
command -v mdb-export >/dev/null || { echo "missing: mdbtools (brew install mdbtools)" >&2; exit 1; }

mkdir -p "${INKWELL_KEY_FILE:h}"
chmod 700 "${INKWELL_KEY_FILE:h}"
exec .venv/bin/inkwell-db "$@"
