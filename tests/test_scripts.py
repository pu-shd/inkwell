"""Docker run scripts carry every key-shaping setting from .env into the container."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ZSH = shutil.which("zsh")  # installed in the test image; macOS has it


def _docker_env(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert ZSH is not None, "zsh is required for these tests"
    script = (
        'set -euo pipefail; source scripts/docker-env.sh; inkwell_docker_env; print -rl -- "${docker_env[@]}"'
    )
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("INKWELL_", "HOST_"))}
    return subprocess.run(
        [ZSH, "-f", "-c", script], cwd=ROOT, env={**clean, **env}, capture_output=True, text=True
    )


def test_settings_and_permitted_files_are_passed_to_the_container(tmp_path: Path) -> None:
    key = tmp_path / "key"
    (key / "lists").mkdir(parents=True)
    res = _docker_env(
        {
            "HOST_KEY_DIR": str(key),
            "INKWELL_PERMITTED_NAMES_FILE": str(key / "lists" / "names fixed.csv"),
            "INKWELL_PERMITTED_PLACES_FILE": str(key / "places.csv"),
            "INKWELL_EXTRA_NAME_NOISE": "graduate,grad,mse",
            "INKWELL_EXTRA_ID_LABELS": "PUID,PU ID",
        }
    )
    assert res.returncode == 0, res.stderr
    pairs = res.stdout.splitlines()
    assert pairs[0::2] == ["-e"] * (len(pairs) // 2)
    assert set(pairs[1::2]) == {
        "INKWELL_PERMITTED_NAMES_FILE=/data/key/lists/names fixed.csv",
        "INKWELL_PERMITTED_PLACES_FILE=/data/key/places.csv",
        "INKWELL_EXTRA_NAME_NOISE=graduate,grad,mse",
        "INKWELL_EXTRA_ID_LABELS=PUID,PU ID",
    }


def test_a_permitted_file_outside_the_key_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "key").mkdir()
    res = _docker_env(
        {"HOST_KEY_DIR": str(tmp_path / "key"), "INKWELL_PERMITTED_NAMES_FILE": str(tmp_path / "names.csv")}
    )
    assert res.returncode != 0
    assert "INKWELL_PERMITTED_NAMES_FILE must be inside HOST_KEY_DIR" in res.stderr


@pytest.mark.parametrize("script", ["docker-run.sh"])
def test_both_docker_scripts_pass_the_settings(script: str) -> None:
    text = (ROOT / "scripts" / script).read_text()
    assert "source scripts/docker-env.sh" in text
    assert "inkwell_docker_env" in text
    assert '"${docker_env[@]}"' in text


def test_docker_run_refuses_a_missing_input_folder(tmp_path: Path) -> None:
    # A bind mount of a missing folder would create it empty, and the run would see no records.
    assert ZSH is not None
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    for name in ("docker-run.sh", "docker-env.sh"):
        shutil.copy(ROOT / "scripts" / name, work / "scripts" / name)
    missing = tmp_path / "no such records"
    (work / ".env").write_text(
        f'HOST_INPUT_DIR="{missing}"\n'
        f'HOST_OUTPUT_DIR="{tmp_path / "out"}"\n'
        f'HOST_KEY_DIR="{tmp_path / "key"}"\n'
    )
    stub = tmp_path / "bin"
    stub.mkdir()
    called = tmp_path / "docker-compose-called"
    (stub / "docker-compose").write_text(f"#!/bin/sh\ntouch '{called}'\nexit 99\n")
    (stub / "docker-compose").chmod(0o755)
    env = {"PATH": f"{stub}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    res = subprocess.run(
        [ZSH, "-f", str(work / "scripts" / "docker-run.sh")], capture_output=True, text=True, env=env
    )
    assert res.returncode != 0
    assert "HOST_INPUT_DIR does not exist" in res.stderr
    assert not called.exists()
    assert not missing.exists()
    assert not (tmp_path / "out").exists()
