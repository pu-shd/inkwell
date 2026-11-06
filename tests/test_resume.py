"""Interrupting, pausing and resuming a run."""

from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from inkwell import cli
from inkwell.config import ConfigError

from .conftest import SSN

OLD = 631152000  # 1990-01-01


def _tree(root: Path) -> None:
    for folder in ("Roe, Jane", "Poe, Jasper"):
        d = root / "Cohort 1990-91" / folder
        d.mkdir(parents=True)
        for n in (1, 2):
            f = d / f"note {n}.txt"
            f.write_text(f"Note {n} about {folder.split(', ')[1]} {folder.split(', ')[0]}, SSN {SSN}.")
            os.utime(f, (OLD, OLD))


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


class _InterruptingPool:
    """In-process stand-in for a worker pool: finishes `after` jobs, then Ctrl-C arrives."""

    after = 1

    def __init__(self, processes: int, initializer: Callable[..., None], initargs: tuple[Any, ...], **_: Any):
        initializer(*initargs)

    def __enter__(self) -> _InterruptingPool:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def imap_unordered(
        self, fn: Callable[[Any], Any], items: Iterable[Any], chunksize: int = 1
    ) -> Iterator[Any]:
        for i, item in enumerate(items):
            if i == self.after:
                raise KeyboardInterrupt
            yield fn(item)


class _Ctx:
    Pool = _InterruptingPool


def _outputs(out: Path) -> list[Path]:
    return [p for p in out.rglob("*") if p.is_file() and not p.name.startswith("_inkwell")]


def _key_rows(key: Path) -> list[dict[str, str]]:
    with key.open() as fh:
        return list(csv.DictReader(fh))


@pytest.mark.slow
def test_interrupt_saves_progress_then_rerun_resumes(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as m:
        m.setattr(cli.mp, "get_context", lambda _method: _Ctx)
        assert cli.main([]) == 130, "an interrupted run exits 130"
    # Workers ignore Ctrl-C (only the main process handles it); initializing one in this process
    # must not disable Ctrl-C here.
    assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN
    done = _outputs(env["out"])
    assert len(done) == 1
    # The finished file's key data is saved even though fewer than 50 files completed.
    assert sum(1 for r in _key_rows(env["key"]) if r["sha256_output"]) == 1
    assert not list(env["out"].rglob(".part-*"))
    assert not (env["key"].parent / "inkwell.lock").exists(), "lock released on interrupt"

    assert cli.main([]) == 0, "running the same command again resumes"
    assert len(_outputs(env["out"])) == 4
    summary = json.loads((env["out"] / "_inkwell_summary.json").read_text())
    assert summary["already_done"] == 1 and summary["processed"] == 3
    assert sum(1 for r in _key_rows(env["key"]) if r["sha256_output"]) == 4
    first = done[0]
    assert first.exists() and first.stat().st_mtime_ns == OLD * 10**9, "finished output untouched"


@pytest.mark.slow
def test_stale_partial_files_are_removed(env: dict[str, Path]) -> None:
    stale = env["out"] / "Cohort 1990-91" / ".part-abc123"
    stale.parent.mkdir(parents=True)
    stale.write_text(f"half-written, never leak-checked {SSN}")
    assert cli.main([]) == 0
    assert not stale.exists()


def test_lock(tmp_path: Path) -> None:
    lock = tmp_path / "inkwell.lock"
    cli._acquire_lock(lock)
    assert lock.read_text().strip() == str(os.getpid())
    with pytest.raises(ConfigError, match="already running"):
        cli._acquire_lock(lock)  # a live process holds it
    cli._release_lock(lock)
    assert not lock.exists()
    lock.write_text("999999999\n")  # a crashed run: no such process
    cli._acquire_lock(lock)
    assert lock.read_text().strip() == str(os.getpid())
    cli._release_lock(lock)


def test_concurrent_run_refused(env: dict[str, Path]) -> None:
    lock = env["key"].parent / "inkwell.lock"
    lock.write_text(f"{os.getpid()}\n")
    assert cli.main(["--dry-run"]) == 1
    assert lock.read_text().strip() == str(os.getpid()), "the other run's lock is left alone"


def _state(pid: int) -> str:
    """Process state letter(s): T = stopped (paused). /proc on Linux (slim images have no ps)."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        return stat.read_text().rsplit(")", 1)[1].split()[0]
    return subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()


def _children(pid: int) -> list[int]:
    return [c for c, p in cli._process_table() if p == pid]


def test_pause_resume_stop(env: dict[str, Path]) -> None:
    # A stand-in "run": a parent that handles SIGTERM like inkwell and has one worker child.
    code = (
        "import signal, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "signal.signal(signal.SIGTERM, lambda *a: (child.terminate(), sys.exit(130)))\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        for _ in range(100):
            if _children(proc.pid):
                break
            time.sleep(0.05)
        (child,) = _children(proc.pid)
        (env["key"].parent / "inkwell.lock").write_text(f"{proc.pid}\n")

        assert cli.main(["--pause"]) == 0
        assert _state(proc.pid).startswith("T") and _state(child).startswith("T")
        assert cli.main(["--resume"]) == 0
        assert not _state(proc.pid).startswith("T") and not _state(child).startswith("T")
        assert cli.main(["--pause"]) == 0
        assert cli.main(["--stop"]) == 0, "stop also wakes a paused run so it can exit cleanly"
        assert proc.wait(timeout=10) == 130
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_control_without_a_run(env: dict[str, Path], caplog: pytest.LogCaptureFixture) -> None:
    assert cli.main(["--resume"]) == 1
    assert "run the same command again" in caplog.text, "points to resuming by rerunning"
    (env["key"].parent / "inkwell.lock").write_text("999999999\n")
    assert cli.main(["--pause"]) == 1


def test_control_flags_are_exclusive(env: dict[str, Path]) -> None:
    assert cli.main(["--pause", "--dry-run"]) == 1
    assert cli.main(["--pause", "--stop"]) == 1
