#!/bin/zsh
# Scrub records natively. Usage: scripts/run.sh [--dry-run] [--limit N] [--force]
# Reads configuration from .env (see example.env). Never modifies the input folder.
set -euo pipefail
cd "${0:A:h}/.."

[[ -f .env ]] || { echo ".env not found; run scripts/setup.sh first" >&2; exit 1; }
set -a; source ./.env; set +a
[[ -x .venv/bin/inkwell ]] || { echo ".venv missing; run scripts/setup.sh" >&2; exit 1; }

mkdir -p "${INKWELL_KEY_FILE:h}"
chmod 700 "${INKWELL_KEY_FILE:h}"
# Keep macOS from sleeping mid-run (the job takes hours); harmless elsewhere.
if command -v caffeinate >/dev/null; then
  exec caffeinate -i .venv/bin/inkwell "$@"
fi
exec .venv/bin/inkwell "$@"
