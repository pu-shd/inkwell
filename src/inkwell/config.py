"""Configuration read from environment variables; validated at startup (fail fast)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ENTITIES = (
    # Presidio built-ins
    "PERSON",
    "LOCATION",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "US_ITIN",
    "US_PASSPORT",
    "US_BANK_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
    # inkwell custom recognizers
    "SSN_LIKE",
    "STUDENT_ID",
    "DATE_OF_BIRTH",
    "PLACE_OF_BIRTH",
    "MAIDEN_NAME",
    "STREET_ADDRESS",
    "GOV_ID",
    "KNOWN_STUDENT_NAME",
)


# Top-level grouping folders (cohort / class / year) kept verbatim in the output; the one
# capturing group is the year label recorded in the registry. Default: names ending "2019-20".
DEFAULT_GROUP_DIR_REGEX = r"^(?:.*\D)?(\d{4}-\d{2})$"


log = logging.getLogger("inkwell")


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


def _from_key_folder(env: dict[str, str], key_dir: Path) -> None:
    """Fill key-shaping variables that are not set at all from the key folder's settings.json,
    so a key folder carries its own settings to a new machine. A set variable (even empty) wins
    and is then checked against the record as before."""
    from . import settings  # settings imports this module

    path = key_dir / settings.SETTINGS_NAME
    if not path.exists():
        return
    recorded = settings.load(path)["settings"]
    assert isinstance(recorded, dict)
    taken = []
    for name, value in recorded.items():
        if name in env or not name.startswith("INKWELL_") or value is None:
            continue
        if isinstance(value, dict):  # a permitted file, recorded by its path in the key folder
            file = key_dir / str(value.get("file", ""))
            if not file.is_file() or not value.get("file"):
                raise ConfigError(
                    f"{name} was recorded as {value.get('file')}, which is not in the key folder; "
                    f"put the file there or set {name}"
                )
            env[name] = str(file)
        elif isinstance(value, bool):
            env[name] = "true" if value else "false"
        elif isinstance(value, list):
            env[name] = ",".join(map(str, value))
        else:
            env[name] = str(value)
        taken.append(name)
    if taken:
        log.info("from the key folder's %s: %s", settings.SETTINGS_NAME, ", ".join(sorted(taken)))


@dataclass(frozen=True)
class Config:
    input_dir: Path
    output_dir: Path
    key_file: Path
    workers: int = 4
    dpi: int = 300
    jpeg_quality: int = 70
    score_threshold: float = 0.35
    spacy_model: str = "en_core_web_lg"
    entities: tuple[str, ...] = DEFAULT_ENTITIES
    withhold_threshold: int = 3
    max_zip_bytes: int = 2 * 1024**3
    tesseract_lang: str = "eng"
    group_dir_regex: str = DEFAULT_GROUP_DIR_REGEX
    extra_id_labels: tuple[str, ...] = ()  # institution-specific ID labels, e.g. "XUID"
    extra_name_noise: tuple[str, ...] = ()  # words in folder names that are not names
    permitted_names_file: Path | None = None  # people whose full names stay visible
    permitted_contacts: bool = False  # also keep their listed emails / phone numbers
    permitted_places_file: Path | None = None  # institutional places that stay visible
    extra: dict[str, str] = field(default_factory=dict)


def _int(env: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not lo <= val <= hi:
        raise ConfigError(f"{name} must be in [{lo}, {hi}], got {val}")
    return val


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def load_config(env: dict[str, str] | None = None, *, require_output: bool = True) -> Config:
    env = dict(os.environ if env is None else env)
    missing = [n for n in ("INKWELL_INPUT_DIR", "INKWELL_OUTPUT_DIR", "INKWELL_KEY_FILE") if not env.get(n)]
    if missing:
        raise ConfigError(f"missing required environment variable(s): {', '.join(missing)}")

    input_dir = Path(env["INKWELL_INPUT_DIR"]).expanduser().resolve()
    output_dir = Path(env["INKWELL_OUTPUT_DIR"]).expanduser().resolve()
    key_file = Path(env["INKWELL_KEY_FILE"]).expanduser().resolve()

    if not input_dir.is_dir():
        raise ConfigError("INKWELL_INPUT_DIR does not exist or is not a directory")
    if output_dir == input_dir or _is_within(output_dir, input_dir):
        raise ConfigError("INKWELL_OUTPUT_DIR must not be the input directory or inside it")
    if _is_within(input_dir, output_dir):
        raise ConfigError("INKWELL_INPUT_DIR must not be inside INKWELL_OUTPUT_DIR")
    if _is_within(key_file, output_dir):
        raise ConfigError(
            "INKWELL_KEY_FILE must be outside INKWELL_OUTPUT_DIR (it maps pseudonyms to real names)"
        )
    if _is_within(key_file, input_dir):
        raise ConfigError("INKWELL_KEY_FILE must be outside INKWELL_INPUT_DIR")
    if require_output and output_dir.exists() and not output_dir.is_dir():
        raise ConfigError("INKWELL_OUTPUT_DIR exists and is not a directory")
    if not key_file.parent.is_dir():
        raise ConfigError("directory containing INKWELL_KEY_FILE does not exist")
    _from_key_folder(env, key_file.parent)

    entities: tuple[str, ...] = DEFAULT_ENTITIES
    if env.get("INKWELL_ENTITIES"):
        entities = tuple(e.strip() for e in env["INKWELL_ENTITIES"].split(",") if e.strip())
        unknown = set(entities) - set(DEFAULT_ENTITIES)
        if unknown:
            raise ConfigError(f"INKWELL_ENTITIES has unknown entity types: {sorted(unknown)}")
        if not entities:
            raise ConfigError("INKWELL_ENTITIES is empty")

    threshold_raw = env.get("INKWELL_SCORE_THRESHOLD", "0.35")
    try:
        threshold = float(threshold_raw)
    except ValueError as exc:
        raise ConfigError("INKWELL_SCORE_THRESHOLD must be a number") from exc
    if not 0.0 < threshold <= 1.0:
        raise ConfigError("INKWELL_SCORE_THRESHOLD must be in (0, 1]")

    group_rx = env.get("INKWELL_GROUP_DIR_REGEX") or DEFAULT_GROUP_DIR_REGEX
    try:
        compiled = re.compile(group_rx)
    except re.error as exc:
        raise ConfigError(f"INKWELL_GROUP_DIR_REGEX is not a valid regex: {exc}") from exc
    if compiled.groups != 1:
        raise ConfigError("INKWELL_GROUP_DIR_REGEX must have exactly one capturing group (the year)")

    labels = tuple(x.strip() for x in env.get("INKWELL_EXTRA_ID_LABELS", "").split(",") if x.strip())
    for lab in labels:
        if not re.fullmatch(r"[A-Za-z0-9 .#-]{2,40}", lab):
            raise ConfigError(f"INKWELL_EXTRA_ID_LABELS has an invalid label: {lab!r}")
    noise = tuple(x.strip().lower() for x in env.get("INKWELL_EXTRA_NAME_NOISE", "").split(",") if x.strip())
    for word in noise:
        if not re.fullmatch(r"[a-z'’]{1,40}", word):
            raise ConfigError(f"INKWELL_EXTRA_NAME_NOISE has an invalid word: {word!r}")

    permitted: Path | None = None
    if env.get("INKWELL_PERMITTED_NAMES_FILE"):
        permitted = Path(env["INKWELL_PERMITTED_NAMES_FILE"]).expanduser().resolve()
        if not permitted.is_file():
            raise ConfigError("INKWELL_PERMITTED_NAMES_FILE does not exist or is not a file")
        if _is_within(permitted, output_dir):
            raise ConfigError(
                "INKWELL_PERMITTED_NAMES_FILE must be outside INKWELL_OUTPUT_DIR (it lists names)"
            )
        if _is_within(permitted, input_dir):
            raise ConfigError("INKWELL_PERMITTED_NAMES_FILE must be outside INKWELL_INPUT_DIR")
    places: Path | None = None
    if env.get("INKWELL_PERMITTED_PLACES_FILE"):
        places = Path(env["INKWELL_PERMITTED_PLACES_FILE"]).expanduser().resolve()
        if not places.is_file():
            raise ConfigError("INKWELL_PERMITTED_PLACES_FILE does not exist or is not a file")
        if _is_within(places, output_dir):
            raise ConfigError("INKWELL_PERMITTED_PLACES_FILE must be outside INKWELL_OUTPUT_DIR")
        if _is_within(places, input_dir):
            raise ConfigError("INKWELL_PERMITTED_PLACES_FILE must be outside INKWELL_INPUT_DIR")
    contacts_raw = env.get("INKWELL_PERMITTED_CONTACTS", "false").strip().lower()
    if contacts_raw not in ("true", "false", "1", "0", "yes", "no"):
        raise ConfigError("INKWELL_PERMITTED_CONTACTS must be true or false")

    return Config(
        input_dir=input_dir,
        output_dir=output_dir,
        key_file=key_file,
        workers=_int(env, "INKWELL_WORKERS", 4, 1, 64),
        dpi=_int(env, "INKWELL_DPI", 300, 150, 600),
        jpeg_quality=_int(env, "INKWELL_JPEG_QUALITY", 70, 30, 95),
        score_threshold=threshold,
        spacy_model=env.get("INKWELL_SPACY_MODEL", "en_core_web_lg"),
        entities=entities,
        withhold_threshold=_int(env, "INKWELL_WITHHOLD_THRESHOLD", 3, 1, 100),
        max_zip_bytes=_int(env, "INKWELL_MAX_ZIP_BYTES", 2 * 1024**3, 1024, 16 * 1024**3),
        tesseract_lang=env.get("INKWELL_TESSERACT_LANG", "eng"),
        group_dir_regex=group_rx,
        extra_id_labels=labels,
        extra_name_noise=noise,
        permitted_names_file=permitted,
        permitted_contacts=contacts_raw in ("true", "1", "yes"),
        permitted_places_file=places,
    )
