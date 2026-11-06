#!/bin/zsh
# Lint, type-check and test. Default: in Docker (as CI does). --local uses .venv.
set -euo pipefail
cd "${0:A:h}/.."

if [[ "${1:-}" == "--local" ]]; then
  .venv/bin/ruff check src tests
  .venv/bin/ruff format --check src tests
  .venv/bin/mypy src
  .venv/bin/pytest -q
else
  docker-compose build test
  docker-compose run --rm test
fi
