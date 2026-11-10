#!/bin/zsh
# Scrub records inside a network-isolated container. Usage: scripts/docker-run.sh [--dry-run] [--limit N]
# Reads .env. Permitted names/places files must be inside HOST_KEY_DIR (the key folder).
set -euo pipefail
cd "${0:A:h}/.."
[[ -f .env ]] || { echo ".env not found; cp example.env .env and edit" >&2; exit 1; }
set -a; source ./.env; set +a
for v in HOST_INPUT_DIR HOST_OUTPUT_DIR HOST_KEY_DIR; do
  [[ -n "${(P)v:-}" ]] || { echo "$v is not set in .env" >&2; exit 1; }
done
# A bind mount of a missing folder would create it empty and the run would find no records.
[[ -d "$HOST_INPUT_DIR" ]] || { echo "HOST_INPUT_DIR does not exist" >&2; exit 1; }
source scripts/docker-env.sh
inkwell_docker_env
mkdir -p "$HOST_OUTPUT_DIR" "$HOST_KEY_DIR"
chmod 700 "$HOST_KEY_DIR"
docker-compose build scrub
docker-compose run --rm --user "$(id -u):$(id -g)" "${docker_env[@]}" scrub "$@"
