#!/usr/bin/env python3
"""Check built distributions before a release: only allow-listed files, consistent version metadata.

Usage: check_dist.py DIST_DIR VERSION   (exit 0 ok, 1 problems found, 2 usage)

The sdist and wheel are public. Anything not explicitly expected (an .env, a key or registry CSV,
records, local agent files) fails the release instead of shipping.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

MODEL_URL = re.compile(
    r"^en-core-web-lg\s*@\s*https://github\.com/explosion/spacy-models/releases/download/"
    r"en_core_web_lg-[\d.]+/en_core_web_lg-[\d.]+-py3-none-any\.whl$"
)
SDIST_ALLOWED = re.compile(
    r"^(?:PKG-INFO|\.gitignore|pyproject\.toml|README\.md|CHANGELOG\.md|LICENSE|uv\.lock"
    r"|src/inkwell/[a-z_]+\.py|src/inkwell/py\.typed|tests/[a-z_]+\.py)$"
)
WHEEL_PKG_ALLOWED = re.compile(r"^inkwell/(?:[a-z_]+\.py|py\.typed)$")
WHEEL_INFO_ALLOWED = {"METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE"}


def _meta_problems(where: str, text: str, version: str) -> list[str]:
    msg = Parser().parsestr(text)
    out = []
    if msg.get("Name") != "inkwell":
        out.append(f"{where}: Name is {msg.get('Name')!r}")
    if msg.get("Version") != version:
        out.append(f"{where}: Version is {msg.get('Version')!r}, expected {version!r}")
    requires = msg.get_all("Requires-Dist") or []
    if not any(MODEL_URL.match(r.strip()) for r in requires):
        out.append(f"{where}: en-core-web-lg must be a pinned release URL (got {requires!r})")
    return out


def _check_wheel(path: Path, version: str) -> list[str]:
    info = f"inkwell-{version}.dist-info/"
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for n in names:
            ok = WHEEL_PKG_ALLOWED.match(n) or (n.startswith(info) and n[len(info) :] in WHEEL_INFO_ALLOWED)
            if not ok:
                out.append(f"wheel: unexpected member {n}")
        if out:
            return out
        if info + "METADATA" not in names:
            return ["wheel: METADATA missing"]
        out += _meta_problems("wheel", z.read(info + "METADATA").decode(), version)
        eps = z.read(info + "entry_points.txt").decode() if info + "entry_points.txt" in names else ""
        if not re.search(r"^inkwell\s*=\s*inkwell\.cli:main$", eps, re.M):
            out.append("wheel: console script 'inkwell = inkwell.cli:main' missing")
    return out


def _check_sdist(path: Path, version: str) -> list[str]:
    root = f"inkwell-{version}/"
    out: list[str] = []
    with tarfile.open(path) as t:
        members = [m for m in t.getmembers() if not m.isdir()]
        for m in members:
            rel = m.name[len(root) :] if m.name.startswith(root) else None
            if rel is None or not m.isfile() or not SDIST_ALLOWED.match(rel):
                out.append(f"sdist: unexpected member {m.name}")
        if out:
            return out
        pkg = t.extractfile(root + "PKG-INFO")
        if pkg is None:
            return ["sdist: PKG-INFO missing"]
        out += _meta_problems("sdist", pkg.read().decode(), version)
    return out


def check(dist: Path, version: str) -> list[str]:
    wheel = dist / f"inkwell-{version}-py3-none-any.whl"
    sdist = dist / f"inkwell-{version}.tar.gz"
    expected = (wheel, sdist, dist / ".gitignore")  # uv build writes dist/.gitignore
    problems = [f"dist: unexpected file {p.name}" for p in sorted(dist.iterdir()) if p not in expected]
    if not wheel.is_file():
        problems.append(f"dist: expected exactly one wheel {wheel.name}")
    if not sdist.is_file():
        problems.append(f"dist: expected exactly one sdist {sdist.name}")
    if problems:
        return problems
    return _check_wheel(wheel, version) + _check_sdist(sdist, version)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    problems = check(Path(argv[0]), argv[1])
    for p in problems:
        print(f"check_dist: {p}", file=sys.stderr)
    if not problems:
        print(f"check_dist: {argv[0]} ok for {argv[1]}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
