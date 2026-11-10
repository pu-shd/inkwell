"""Person-level registry of record subjects, kept beside the key file (never in the output).

One row per person, not per folder: someone whose records sit in several group folders,
or in a zip/folder at the top level, gets a single ID. Operators may edit ``canonical_name``,
``aliases`` and ``institution_id``; edits are preserved across runs.
"""

from __future__ import annotations

import csv
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_GROUP_DIR_REGEX
from .ids import new_id
from .recognizers import name_variants

REG_HEADER = (
    "student_id",
    "canonical_name",
    "aliases",
    "class_years",
    "folders",
    "institution_id",
    "needs_review",
)


def name_key(raw: str) -> frozenset[str]:
    """Order-insensitive identity of a name: 'Roe, Jane' == 'Jane Roe'."""
    _, tokens = name_variants(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw))
    return frozenset(t.lower() for t in tokens)


@dataclass
class Student:
    student_id: str
    canonical_name: str
    aliases: set[str] = field(default_factory=set)
    class_years: set[str] = field(default_factory=set)
    folders: set[str] = field(default_factory=set)
    institution_id: str = ""  # optional, filled in by hand (e.g. a university ID)
    needs_review: str = ""

    def names(self) -> set[str]:
        return {self.canonical_name, *self.aliases}

    def keys(self) -> set[frozenset[str]]:
        return {k for k in (name_key(n) for n in self.names()) if k}


class Registry:
    def __init__(self, path: Path, group_dir_regex: str = DEFAULT_GROUP_DIR_REGEX) -> None:
        self.path = path
        self.group_rx = re.compile(group_dir_regex)
        self.students: dict[str, Student] = {}
        if path.exists():
            with path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                if tuple(reader.fieldnames or ()) != REG_HEADER:
                    raise ValueError("registry file has unexpected format")
                for row in reader:
                    s = Student(
                        row["student_id"],
                        row["canonical_name"],
                        _split(row["aliases"]),
                        _split(row["class_years"]),
                        _split(row["folders"]),
                        row["institution_id"],
                        row["needs_review"],
                    )
                    self.students[s.student_id] = s

    # -- lookup ---------------------------------------------------------------

    def by_key(self, key: frozenset[str]) -> Student | None:
        if not key:
            return None
        for s in self.students.values():
            if key in s.keys():
                return s
        return None

    def match_loose(self, raw: str) -> list[str]:
        """IDs of people whose full name is contained in a loose (top-level) item name,
        e.g. 'JaneRoe.zip' or '<Name> Records  xtra info'."""
        key = name_key(raw)
        if len(key) < 2:
            return []
        hits = [
            s.student_id for s in self.students.values() if any(len(k) >= 2 and k <= key for k in s.keys())
        ]
        return sorted(hits)

    def folder_owner(self, rel_dir: str) -> str | None:
        for s in self.students.values():
            if rel_dir in s.folders:
                return s.student_id
        return None

    # -- building -------------------------------------------------------------

    def is_group_dir(self, name: str) -> bool:
        return self.group_rx.match(name) is not None

    def add_folder(self, class_dir: str, folder_name: str) -> Student:
        rel = f"{class_dir}/{folder_name}"
        owner = self.folder_owner(rel)
        if owner:
            return self.students[owner]
        year_m = self.group_rx.match(class_dir)
        year = year_m.group(1) if year_m else ""
        key = name_key(folder_name)
        s = self.by_key(key)
        if s is None:
            sid = new_id("student", self.students)
            s = Student(sid, _display(folder_name))
            self.students[sid] = s
        else:
            s.needs_review = "same name found in more than one group folder; split if different people"
        s.aliases.add(folder_name)
        s.folders.add(rel)
        if year:
            s.class_years.add(year)
        return s

    def add_person(self, name: str, aliases: set[str] | None = None, note: str = "") -> Student:
        """A new person with no document folder (e.g. found only in a database)."""
        sid = new_id("student", self.students)
        display = re.sub(r"\s+", " ", name).strip()
        s = Student(sid, display, {a for a in (aliases or set()) if a and a != display}, needs_review=note)
        self.students[sid] = s
        return s

    def build(self, input_dir: Path) -> None:
        for cls in sorted(input_dir.iterdir()):
            if cls.is_dir() and self.is_group_dir(cls.name):
                for stu in sorted(cls.iterdir()):
                    if stu.is_dir():
                        self.add_folder(cls.name, stu.name)

    # -- export ---------------------------------------------------------------

    def name_table(self) -> list[tuple[str, list[str]]]:
        return [(s.student_id, sorted(s.names())) for s in self.students.values()]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".reg-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(REG_HEADER)
                for s in sorted(self.students.values(), key=lambda x: x.student_id):
                    w.writerow(
                        [
                            s.student_id,
                            s.canonical_name,
                            ";".join(sorted(s.aliases)),
                            ";".join(sorted(s.class_years)),
                            ";".join(sorted(s.folders)),
                            s.institution_id,
                            s.needs_review,
                        ]
                    )
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _split(v: str) -> set[str]:
    return {x for x in (v or "").split(";") if x}


def _display(folder_name: str) -> str:
    if "," in folder_name:
        last, _, first = folder_name.partition(",")
        folder_name = f"{first.strip()} {last.strip()}"
    return re.sub(r"\s+", " ", folder_name).strip()
