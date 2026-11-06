"""Release packaging: --version, and the distribution checker run before every release."""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import zipfile
from importlib.metadata import version
from pathlib import Path
from types import ModuleType

import pytest

from inkwell import __version__, cli

ROOT = Path(__file__).resolve().parents[1]
V = "9.8.7"
MODEL = (
    "en-core-web-lg @ https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl"
)


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_dist", ROOT / "scripts" / "check_dist.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_dist"] = mod
    spec.loader.exec_module(mod)
    return mod


check_dist = _load_checker()


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"inkwell {version('inkwell')}"
    assert __version__ == version("inkwell")


def _metadata(v: str = V, requires: tuple[str, ...] = (MODEL,)) -> str:
    lines = ["Metadata-Version: 2.4", "Name: inkwell", f"Version: {v}"]
    lines += [f"Requires-Dist: {r}" for r in requires]
    return "\n".join(lines) + "\n\nreadme\n"


def _wheel(dist: Path, v: str = V, extra: dict[str, str] | None = None, **meta: object) -> Path:
    p = dist / f"inkwell-{v}-py3-none-any.whl"
    di = f"inkwell-{v}.dist-info"
    files = {
        "inkwell/__init__.py": "",
        "inkwell/cli.py": "",
        f"{di}/METADATA": _metadata(v, **meta),  # type: ignore[arg-type]
        f"{di}/WHEEL": "Wheel-Version: 1.0\n",
        f"{di}/entry_points.txt": "[console_scripts]\ninkwell = inkwell.cli:main\n",
        f"{di}/RECORD": "",
        **(extra or {}),
    }
    with zipfile.ZipFile(p, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return p


def _sdist(dist: Path, v: str = V, extra: dict[str, str] | None = None) -> Path:
    p = dist / f"inkwell-{v}.tar.gz"
    files = {
        "PKG-INFO": _metadata(v),
        ".gitignore": "",
        "pyproject.toml": "",
        "README.md": "",
        "CHANGELOG.md": "",
        "uv.lock": "",
        "src/inkwell/__init__.py": "",
        "tests/test_cli.py": "",
        **(extra or {}),
    }
    with tarfile.open(p, "w:gz") as t:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(f"inkwell-{v}/{name}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return p


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    d.mkdir()
    (d / ".gitignore").write_text("*\n")  # as uv build writes it
    return d


def test_clean_dist_passes(dist: Path) -> None:
    _wheel(dist)
    _sdist(dist)
    assert check_dist.check(dist, V) == []


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "src/inkwell/.env.local",
        "key.csv",
        "registry.csv",
        "CLAUDE.md",
        ".claude/settings.json",
        "out (scrubbed)/a.pdf",
        "notes.txt",
        "data.db",
    ],
)
def test_sdist_rejects_anything_unlisted(dist: Path, name: str) -> None:
    _wheel(dist)
    _sdist(dist, extra={name: "x"})
    problems = check_dist.check(dist, V)
    assert problems and name in problems[0]


@pytest.mark.parametrize("name", ["inkwell/key.csv", "inkwell/.env", "secrets.txt", "inkwell/x.pdf"])
def test_wheel_rejects_anything_unlisted(dist: Path, name: str) -> None:
    _wheel(dist, extra={name: "x"})
    _sdist(dist)
    problems = check_dist.check(dist, V)
    assert problems and name in problems[0]


def test_version_mismatch(dist: Path) -> None:
    _wheel(dist, v="1.0.0")
    _sdist(dist, v="1.0.0")
    problems = check_dist.check(dist, V)
    assert any("expected exactly one wheel" in p for p in problems)


def test_metadata_version_mismatch(dist: Path) -> None:
    p = _wheel(dist)
    p.unlink()
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(f"inkwell-{V}.dist-info/METADATA", _metadata("0.0.1"))
        z.writestr(f"inkwell-{V}.dist-info/entry_points.txt", "inkwell = inkwell.cli:main\n")
    _sdist(dist)
    assert any("Version" in p for p in check_dist.check(dist, V))


def test_model_must_be_pinned_url(dist: Path) -> None:
    _wheel(dist, requires=("en-core-web-lg",))
    _sdist(dist)
    assert any("en-core-web-lg" in p for p in check_dist.check(dist, V))


def test_stray_file_in_dist(dist: Path) -> None:
    _wheel(dist)
    _sdist(dist)
    (dist / "key.csv").write_text("x")
    assert any("unexpected file" in p for p in check_dist.check(dist, V))


def test_main_exit_codes(dist: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _wheel(dist)
    _sdist(dist)
    assert check_dist.main([str(dist), V]) == 0
    (dist / "stray").write_text("x")
    assert check_dist.main([str(dist), V]) == 1
    assert "stray" in capsys.readouterr().err
    assert check_dist.main([str(dist)]) == 2


def test_project_version_is_consistent() -> None:
    import tomllib

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert meta["version"] == version("inkwell")
    assert MODEL.replace(" ", "") in [d.replace(" ", "") for d in meta["dependencies"]]
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## [{meta['version']}]" in changelog
