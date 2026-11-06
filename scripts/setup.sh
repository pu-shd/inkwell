#!/bin/zsh
# Create the per-project .venv and install locked dependencies. Safe to re-run.
set -euo pipefail
cd "${0:A:h}/.."

for tool in uv tesseract; do
  command -v "$tool" >/dev/null || { echo "missing: $tool (brew install $tool)" >&2; exit 1; }
done
tesseract --list-langs 2>/dev/null | grep -qx eng || { echo "tesseract 'eng' language data missing" >&2; exit 1; }

[[ -d .venv ]] || uv venv --python 3.12 .venv
UV_PROJECT_ENVIRONMENT=.venv uv sync --frozen
[[ -f .env ]] || { cp example.env .env; chmod 600 .env; echo "created .env from example.env - review it"; }
echo "setup complete"
