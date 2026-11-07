"""--folders FILE: select only the listed top-level folders (combines with date flags)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from inkwell import cli
from inkwell.config import ConfigError

from .conftest import SSN

OLD = 631152000  # 1990-01-01
NEW = 1735689600  # 2025-01-01


def _tree(root: Path) -> None:
    for cohort, folder, when in (
        ("Cohort 1990-91", "Roe, Jane", NEW),  # an old class whose files were re-dated by a later copy
        ("Cohort 1991-92", "Poe, Jasper", NEW),
        ("Cohort 2020-21", "Doe, Kip", OLD),
    ):
        d = root / cohort / folder
        d.mkdir(parents=True)
        f = d / "note.txt"
        f.write_text(f"Note about {folder}, SSN {SSN}.")
        os.utime(f, (when, when))
    loose = root / "loose note.txt"
    loose.write_text("A loose file.")
    os.utime(loose, (OLD, OLD))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {"in": tmp_path / "in", "out": tmp_path / "out", "key": tmp_path / "secret" / "key.csv"}
    paths["in"].mkdir()
    paths["key"].parent.mkdir()
    for k, var in (("in", "INPUT_DIR"), ("out", "OUTPUT_DIR"), ("key", "KEY_FILE")):
        monkeypatch.setenv(f"INKWELL_{var}", str(paths[k]))
    monkeypatch.setenv("INKWELL_WORKERS", "1")
    _tree(paths["in"])
    return paths


def _list(env: dict[str, Path], body: str) -> Path:
    p = env["key"].parent / "folders.txt"
    p.write_text(body)
    return p


def _top_folders(out: Path) -> set[str]:
    return {p.relative_to(out).parts[0] for p in out.rglob("*.txt") if not p.name.startswith("_inkwell")}


def test_read_folder_list(env: dict[str, Path]) -> None:
    p = _list(env, "# old classes\nCohort 1990-91\n\n  Cohort 1991-92/  \n")
    assert cli.read_folder_list(p, env["in"]) == {"Cohort 1990-91", "Cohort 1991-92"}


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("Cohort 1990-91\nNo Such Folder\n", r"line\(s\) 2 "),
        ("loose note.txt\n", r"line\(s\) 1 "),  # a file, not a folder
        ("../secret\n", r"line\(s\) 1 "),
        ("Cohort 1990-91/Roe, Jane\n", r"line\(s\) 1 "),  # top-level folders only
        ("# only comments\n\n", "no folders"),
    ],
)
def test_bad_folder_list(env: dict[str, Path], body: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match) as exc:
        cli.read_folder_list(_list(env, body), env["in"])
    assert "Roe" not in str(exc.value) and "Such" not in str(exc.value), "names never echoed"


@pytest.mark.slow
def test_folders_alone_ignore_dates(env: dict[str, Path]) -> None:
    p = _list(env, "Cohort 1990-91\nCohort 1991-92\n")
    assert cli.main(["--folders", str(p)]) == 0
    assert _top_folders(env["out"]) == {"Cohort 1990-91", "Cohort 1991-92"}
    summary = json.loads((env["out"] / "_inkwell_summary.json").read_text())
    assert summary["selected"] == 2 and summary["excluded_by_folders"] == 2


@pytest.mark.slow
def test_folders_and_dates_combine(env: dict[str, Path]) -> None:
    p = _list(env, "Cohort 1990-91\nCohort 2020-21\n")
    assert cli.main(["--folders", str(p), "--modified-before", "2000-01-01"]) == 0
    assert _top_folders(env["out"]) == {"Cohort 2020-21"}, "listed AND old enough"


@pytest.mark.slow
def test_folders_add_to_an_existing_output(env: dict[str, Path]) -> None:
    # The intended use: a date-based run first, then the listed old classes into the same output.
    assert cli.main(["--modified-before", "2000-01-01"]) == 0
    tops = _top_folders(env["out"])
    assert "Cohort 2020-21" in tops and not {"Cohort 1990-91", "Cohort 1991-92"} & tops
    p = _list(env, "Cohort 1990-91\nCohort 1991-92\n")
    assert cli.main(["--folders", str(p)]) == 0
    assert {"Cohort 1990-91", "Cohort 1991-92", "Cohort 2020-21"} <= _top_folders(env["out"])


def test_folder_list_inside_output_refused(env: dict[str, Path]) -> None:
    env["out"].mkdir()
    p = env["out"] / "folders.txt"
    p.write_text("Cohort 1990-91\n")
    assert cli.main(["--folders", str(p), "--plan-only"]) == 1


def test_plan_only_honors_folders(env: dict[str, Path], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    p = _list(env, "Cohort 1991-92\n")
    assert cli.main(["--folders", str(p), "--plan-only"]) == 0
    assert "folder list selects 1 of 4 files" in caplog.text
