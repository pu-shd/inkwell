"""settings.json: the settings that shaped a key folder are recorded beside it and enforced."""

from __future__ import annotations

import dataclasses
import json
import logging
import stat
from pathlib import Path

import pytest

from inkwell import accessdb, cli, settings
from inkwell.config import Config, ConfigError
from inkwell.settings import SETTINGS_NAME, check, snapshot

from .test_accessdb import env as db_env  # noqa: F401 - fixture
from .test_cli import env as cli_env  # noqa: F401 - fixture


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    key_dir = tmp_path / "key"
    key_dir.mkdir()
    names = key_dir / "permitted.csv"
    names.write_text("name,aliases,emails,phones\nAda Lovelace,,,\n")
    return Config(
        input_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        key_file=key_dir / "key.csv",
        extra_name_noise=("mse", "grad"),
        extra_id_labels=("PUID",),
        permitted_names_file=names,
    )


def _record(cfg: Config) -> dict[str, object]:
    return json.loads((cfg.key_file.parent / SETTINGS_NAME).read_text())


def test_snapshot_names_files_by_key_folder_path_and_hash(cfg: Config) -> None:
    snap = snapshot(cfg)
    assert snap["INKWELL_EXTRA_NAME_NOISE"] == ["grad", "mse"]
    assert snap["INKWELL_EXTRA_ID_LABELS"] == ["PUID"]
    names = snap["INKWELL_PERMITTED_NAMES_FILE"]
    assert isinstance(names, dict) and names["file"] == "permitted.csv" and len(names["sha256"]) == 64
    assert snap["INKWELL_PERMITTED_PLACES_FILE"] is None
    assert not any(k in snap for k in ("INKWELL_INPUT_DIR", "INKWELL_WORKERS", "INKWELL_DPI"))


def test_first_record_run_writes_a_private_record(cfg: Config) -> None:
    check(cfg, tool="inkwell", mode="record")
    path = cfg.key_file.parent / SETTINGS_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    doc = _record(cfg)
    assert doc["format"] == 1 and doc["recorded_by"] == "inkwell"
    assert doc["settings"] == snapshot(cfg)


def test_same_settings_pass_and_leave_the_record_alone(cfg: Config) -> None:
    check(cfg, tool="inkwell", mode="record")
    before = (cfg.key_file.parent / SETTINGS_NAME).read_text()
    check(cfg, tool="inkwell-db", mode="record")
    check(cfg, tool="inkwell", mode="strict")
    assert (cfg.key_file.parent / SETTINGS_NAME).read_text() == before


def test_changed_noise_words_are_refused(cfg: Config) -> None:
    check(cfg, tool="inkwell", mode="record")
    before = (cfg.key_file.parent / SETTINGS_NAME).read_text()
    changed = dataclasses.replace(cfg, extra_name_noise=("grad",))
    with pytest.raises(ConfigError, match=r"INKWELL_EXTRA_NAME_NOISE: recorded grad,mse, now grad.*--accept"):
        check(changed, tool="inkwell", mode="record")
    assert (cfg.key_file.parent / SETTINGS_NAME).read_text() == before


def test_edited_permitted_file_is_refused_but_a_moved_one_is_not(cfg: Config, tmp_path: Path) -> None:
    check(cfg, tool="inkwell", mode="record")
    assert cfg.permitted_names_file is not None
    moved = tmp_path / "elsewhere.csv"
    moved.write_bytes(cfg.permitted_names_file.read_bytes())
    check(dataclasses.replace(cfg, permitted_names_file=moved), tool="inkwell", mode="record")
    cfg.permitted_names_file.write_text("name,aliases,emails,phones\nGrace Hopper,,,\n")
    with pytest.raises(ConfigError, match="INKWELL_PERMITTED_NAMES_FILE"):
        check(cfg, tool="inkwell", mode="record")


def test_accept_keeps_the_old_record_and_writes_the_new(cfg: Config) -> None:
    check(cfg, tool="inkwell", mode="record")
    old = (cfg.key_file.parent / SETTINGS_NAME).read_text()
    changed = dataclasses.replace(cfg, score_threshold=0.5)
    check(changed, tool="inkwell", mode="record", accept=True)
    assert _record(cfg)["settings"] == snapshot(changed)
    archived = list(cfg.key_file.parent.glob("settings-*.json"))
    assert len(archived) == 1 and archived[0].read_text() == old
    assert stat.S_IMODE(archived[0].stat().st_mode) == 0o600


def test_strict_mode_refuses_even_when_accepting(cfg: Config) -> None:
    check(cfg, tool="inkwell", mode="record")
    with pytest.raises(ConfigError, match="this mode cannot change them"):
        check(dataclasses.replace(cfg, score_threshold=0.5), tool="inkwell", mode="strict", accept=True)


def test_warn_mode_logs_and_writes_nothing(cfg: Config, caplog: pytest.LogCaptureFixture) -> None:
    check(cfg, tool="inkwell", mode="warn")
    assert not (cfg.key_file.parent / SETTINGS_NAME).exists()
    check(cfg, tool="inkwell", mode="record")
    with caplog.at_level(logging.WARNING, logger="inkwell"):
        check(dataclasses.replace(cfg, withhold_threshold=9), tool="inkwell", mode="warn")
    assert "INKWELL_WITHHOLD_THRESHOLD: recorded 3, now 9" in caplog.text


def test_unreadable_record_is_a_config_error(cfg: Config) -> None:
    (cfg.key_file.parent / SETTINGS_NAME).write_text("{not json")
    with pytest.raises(ConfigError, match="cannot be read"):
        check(cfg, tool="inkwell", mode="warn")
    (cfg.key_file.parent / SETTINGS_NAME).write_text('{"format": 99, "settings": {}}')
    with pytest.raises(ConfigError, match="unexpected format"):
        check(cfg, tool="inkwell", mode="warn")


def test_a_new_inkwell_version_alone_is_not_a_difference(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    check(cfg, tool="inkwell", mode="record")
    monkeypatch.setattr(settings, "__version__", "99.0.0")
    check(cfg, tool="inkwell", mode="record")


# --- through the commands --------------------------------------------------------------------


def test_inkwell_save_settings_then_a_changed_run_is_refused(
    cli_env: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_dir = cli_env["key"].parent
    monkeypatch.setenv("INKWELL_EXTRA_NAME_NOISE", "grad,mse")
    assert cli.main(["--save-settings"]) == 0
    assert _record_at(key_dir)["settings"]["INKWELL_EXTRA_NAME_NOISE"] == ["grad", "mse"]
    assert not cli_env["out"].exists()
    monkeypatch.setenv("INKWELL_EXTRA_NAME_NOISE", "grad")
    assert cli.main([]) == 1
    assert not cli_env["out"].exists()
    assert cli.main(["--save-settings"]) == 1
    assert cli.main(["--save-settings", "--accept-settings"]) == 0
    assert _record_at(key_dir)["settings"]["INKWELL_EXTRA_NAME_NOISE"] == ["grad"]


def test_inkwell_flag_combinations_are_refused(cli_env: dict[str, Path]) -> None:  # noqa: F811
    assert cli.main(["--save-settings", "--dry-run"]) == 1
    assert cli.main(["--accept-settings", "--audit"]) == 1


def test_inkwell_db_shares_the_record_with_documents(
    db_env: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_dir = db_env["key"].parent
    assert accessdb.main([]) == 0
    assert _record_at(key_dir)["recorded_by"] == "inkwell-db"
    monkeypatch.setenv("INKWELL_EXTRA_NAME_NOISE", "mse")
    assert accessdb.main(["--dry-run"]) == 0  # a dry run only warns
    registry = (key_dir / "registry.csv").read_text()
    assert accessdb.main([]) == 1
    assert (key_dir / "registry.csv").read_text() == registry
    assert accessdb.main(["--accept-settings", "--dry-run"]) == 1
    assert accessdb.main(["--accept-settings"]) == 0
    assert _record_at(key_dir)["settings"]["INKWELL_EXTRA_NAME_NOISE"] == ["mse"]


def _record_at(key_dir: Path) -> dict[str, dict[str, object]]:
    return json.loads((key_dir / SETTINGS_NAME).read_text())
