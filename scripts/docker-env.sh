# Sourced by scripts/docker-run*.sh after .env is loaded. inkwell_docker_env fills docker_env with
# the -e flags that carry the key-shaping settings into the container (see settings.json). Permitted
# files must be inside HOST_KEY_DIR, which the container mounts at /data/key.

docker_env=()

inkwell_docker_env() {
  local key_dir="${HOST_KEY_DIR:A}" v p
  docker_env=()
  for v in INKWELL_PERMITTED_NAMES_FILE INKWELL_PERMITTED_PLACES_FILE; do
    p="${(P)v:-}"
    [[ -n "$p" ]] || continue
    p="${p:A}"
    if [[ "$p" != "$key_dir"/* ]]; then
      print -u2 "$v must be inside HOST_KEY_DIR to use it in Docker"
      return 1
    fi
    docker_env+=(-e "$v=/data/key/${p#$key_dir/}")
  done
  for v in INKWELL_EXTRA_NAME_NOISE INKWELL_EXTRA_ID_LABELS INKWELL_GROUP_DIR_REGEX INKWELL_ENTITIES \
           INKWELL_SCORE_THRESHOLD INKWELL_WITHHOLD_THRESHOLD INKWELL_SPACY_MODEL INKWELL_TESSERACT_LANG \
           INKWELL_PERMITTED_CONTACTS; do
    if [[ -n "${(P)v:-}" ]]; then
      docker_env+=(-e "$v=${(P)v}")
    fi
  done
}
