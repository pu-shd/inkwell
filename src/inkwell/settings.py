"""The settings that shaped a key folder, recorded beside it in settings.json.

Registry matches, IDs and redactions depend on these settings, so they belong with the key, not
only in a local .env. Machine settings (paths, workers, DPI) are not recorded.

Modes:
* ``record``: refuse a run whose settings differ from the record unless ``accept`` is set; then
  the old record is kept as settings-<time>.json and the new one written. Writes the first record.
* ``strict``: refuse a difference; never write (e.g. --audit).
* ``warn``: log a difference; never write (dry runs, planning).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

from . import __version__
from .config import Config, ConfigError

log = logging.getLogger("inkwell")

SETTINGS_NAME = "settings.json"
FORMAT = 1
Mode = Literal["record", "strict", "warn"]


def _file(path: Path | None, key_dir: Path) -> dict[str, str] | None:
    if path is None:
        return None
    try:
        name = str(path.relative_to(key_dir))
    except ValueError:
        name = path.name
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ConfigError(f"{name} cannot be read: {exc.strerror}") from None
    return {"file": name, "sha256": digest}


def snapshot(cfg: Config) -> dict[str, object]:
    """The settings that change who the people are, their IDs, or what is redacted."""
    key_dir = cfg.key_file.parent
    return {
        "INKWELL_EXTRA_NAME_NOISE": sorted(cfg.extra_name_noise),
        "INKWELL_EXTRA_ID_LABELS": sorted(cfg.extra_id_labels),
        "INKWELL_GROUP_DIR_REGEX": cfg.group_dir_regex,
        "INKWELL_ENTITIES": sorted(cfg.entities),
        "INKWELL_SCORE_THRESHOLD": cfg.score_threshold,
        "INKWELL_WITHHOLD_THRESHOLD": cfg.withhold_threshold,
        "INKWELL_SPACY_MODEL": cfg.spacy_model,
        "INKWELL_TESSERACT_LANG": cfg.tesseract_lang,
        "INKWELL_PERMITTED_CONTACTS": cfg.permitted_contacts,
        "INKWELL_PERMITTED_NAMES_FILE": _file(cfg.permitted_names_file, key_dir),
        "INKWELL_PERMITTED_PLACES_FILE": _file(cfg.permitted_places_file, key_dir),
    }


def _same(name: str, old: object, new: object) -> bool:
    if name.endswith("_FILE") and isinstance(old, dict) and isinstance(new, dict):
        return old.get("sha256") == new.get("sha256")  # the contents count, not where the file is
    return old == new


def _show(value: object) -> str:
    if isinstance(value, dict):
        return f"{value.get('file')} (sha256 {str(value.get('sha256'))[:12]}…)"
    if isinstance(value, list):
        return ",".join(map(str, value)) or "(none)"
    return "(none)" if value is None else str(value)


def differences(old: dict[str, object], new: dict[str, object]) -> list[str]:
    return [
        f"{k}: recorded {_show(old.get(k))}, now {_show(new.get(k))}"
        for k in sorted(set(old) | set(new))
        if not _same(k, old.get(k), new.get(k))
    ]


def load(path: Path) -> dict[str, object]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path.name} cannot be read: {exc}") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or not isinstance(doc.get("settings"), dict):
        raise ConfigError(f"{path.name} has unexpected format")
    return doc


def save(path: Path, settings: dict[str, object], tool: str) -> None:
    doc = {
        "format": FORMAT,
        "recorded": dt.datetime.now().isoformat(timespec="seconds"),
        "recorded_by": tool,
        "inkwell_version": __version__,
        "settings": settings,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def check(cfg: Config, *, tool: str, mode: Mode, accept: bool = False) -> None:
    """Compare this run's settings with the key folder's record (see the module docstring)."""
    path = cfg.key_file.parent / SETTINGS_NAME
    new = snapshot(cfg)
    if not path.exists():
        if mode == "record":
            save(path, new, tool)
            log.info("recorded this key folder's settings in %s", SETTINGS_NAME)
        else:
            log.warning("no %s in the key folder yet; a real run records it", SETTINGS_NAME)
        return
    doc = load(path)
    diffs = differences(doc["settings"], new)  # type: ignore[arg-type]
    if doc.get("inkwell_version") != __version__:
        log.info("settings were recorded by inkwell %s; this is %s", doc.get("inkwell_version"), __version__)
    if not diffs:
        return
    msg = f"settings differ from the ones recorded with this key ({SETTINGS_NAME}): " + "; ".join(diffs)
    if mode == "warn":
        log.warning("%s", msg)
        return
    if mode == "record" and accept:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        os.replace(path, path.with_name(f"settings-{stamp}.json"))
        save(path, new, tool)
        log.warning("accepted new settings; the old record is settings-%s.json. %s", stamp, msg)
        return
    hint = "--accept-settings records the new ones" if mode == "record" else "this mode cannot change them"
    raise ConfigError(f"{msg}. Use the recorded settings, or {hint}")
