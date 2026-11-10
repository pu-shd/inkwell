#!/bin/zsh
# Scrub Access databases inside a network-isolated container. Usage: scripts/docker-run-db.sh [--dry-run]
# Reads .env. Permitted names/places files must be inside HOST_KEY_DIR (the key folder).
set -euo pipefail
cd "${0:A:h}/.."
[[ -f .env ]] || { echo ".env not found; cp example.env .env and edit" >&2; exit 1; }
set -a; source ./.env; set +a
for v in INKWELL_DB_INPUT_DIR INKWELL_DB_OUTPUT_DIR HOST_KEY_DIR; do
  [[ -n "${(P)v:-}" ]] || { echo "$v is not set in .env" >&2; exit 1; }
done
[[ -d "$INKWELL_DB_INPUT_DIR" ]] || { echo "INKWELL_DB_INPUT_DIR does not exist" >&2; exit 1; }
key_dir="${HOST_KEY_DIR:A}"

passthru=()
for v in INKWELL_PERMITTED_NAMES_FILE INKWELL_PERMITTED_PLACES_FILE; do
  p="${(P)v:-}"
  [[ -n "$p" ]] || continue
  p="${p:A}"
  [[ "$p" == "$key_dir"/* ]] || { echo "$v must be inside HOST_KEY_DIR to use it in Docker" >&2; exit 1; }
  passthru+=(-e "$v=/data/key/${p#$key_dir/}")
done
for v in INKWELL_PERMITTED_CONTACTS INKWELL_SCORE_THRESHOLD INKWELL_ENTITIES INKWELL_EXTRA_ID_LABELS \
         INKWELL_EXTRA_NAME_NOISE INKWELL_GROUP_DIR_REGEX; do
  [[ -n "${(P)v:-}" ]] && passthru+=(-e "$v=${(P)v}")
done

mkdir -p "$INKWELL_DB_OUTPUT_DIR" "$key_dir"
chmod 700 "$key_dir"
docker-compose build scrub-db
docker-compose run --rm --user "$(id -u):$(id -g)" "${passthru[@]}" scrub-db "$@"
