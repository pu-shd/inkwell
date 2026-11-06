from __future__ import annotations

from pathlib import Path

import pytest

from inkwell.config import ConfigError, load_config


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    (tmp_path / "in").mkdir(exist_ok=True)
    env = {
        "INKWELL_INPUT_DIR": str(tmp_path / "in"),
        "INKWELL_OUTPUT_DIR": str(tmp_path / "out"),
        "INKWELL_KEY_FILE": str(tmp_path / "key.csv"),
    }
    env.update(over)
    return env


def test_valid_config(tmp_path: Path) -> None:
    cfg = load_config(_env(tmp_path, INKWELL_WORKERS="3"))
    assert cfg.workers == 3
    assert cfg.input_dir == (tmp_path / "in").resolve()


@pytest.mark.parametrize("missing", ["INKWELL_INPUT_DIR", "INKWELL_OUTPUT_DIR", "INKWELL_KEY_FILE"])
def test_missing_required(tmp_path: Path, missing: str) -> None:
    env = _env(tmp_path)
    del env[missing]
    with pytest.raises(ConfigError, match=missing):
        load_config(env)


def test_output_inside_input_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="inside"):
        load_config(_env(tmp_path, INKWELL_OUTPUT_DIR=str(tmp_path / "in" / "out")))


def test_key_inside_output_rejected(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    with pytest.raises(ConfigError, match="outside INKWELL_OUTPUT_DIR"):
        load_config(_env(tmp_path, INKWELL_KEY_FILE=str(tmp_path / "out" / "key.csv")))


def test_key_inside_input_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="outside INKWELL_INPUT_DIR"):
        load_config(_env(tmp_path, INKWELL_KEY_FILE=str(tmp_path / "in" / "key.csv")))


def test_missing_input_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(_env(tmp_path, INKWELL_INPUT_DIR=str(tmp_path / "nope")))


@pytest.mark.parametrize(
    ("var", "val"),
    [
        ("INKWELL_WORKERS", "0"),
        ("INKWELL_WORKERS", "x"),
        ("INKWELL_DPI", "50"),
        ("INKWELL_SCORE_THRESHOLD", "2"),
    ],
)
def test_bad_numbers(tmp_path: Path, var: str, val: str) -> None:
    with pytest.raises(ConfigError, match=var):
        load_config(_env(tmp_path, **{var: val}))


def test_unknown_entity(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown entity"):
        load_config(_env(tmp_path, INKWELL_ENTITIES="PERSON,BOGUS"))
