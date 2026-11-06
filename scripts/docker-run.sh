#!/bin/zsh
# Scrub records inside a network-isolated container. Usage: scripts/docker-run.sh [--dry-run] [--limit N]
set -euo pipefail
cd "${0:A:h}/.."
[[ -f .env ]] || { echo ".env not found; cp example.env .env and edit" >&2; exit 1; }
set -a; source ./.env; set +a
mkdir -p "$HOST_OUTPUT_DIR" "$HOST_KEY_DIR"
chmod 700 "$HOST_KEY_DIR"
docker-compose build scrub
docker-compose run --rm --user "$(id -u):$(id -g)" scrub "$@"
