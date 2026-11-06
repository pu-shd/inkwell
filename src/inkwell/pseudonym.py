"""Pseudonymous paths and the re-identification key (kept OUTSIDE the output tree).

Layout (group folders match INKWELL_GROUP_DIR_REGEX, e.g. "Cohort 2019-20"):
  <group>/                            kept (not PII)
  <group>/<person>/                   -> S-XXXXXXXXC   (registry ID; same across groups)
  other directories                   -> F-XXXXXXC
  files                               -> D-XXXXXXXXXXC.<ext>

Key rows: original path -> pseudonym, the people the document belongs to, the people it
mentions, and SHA-256 of the original and of the scrubbed output. If an original is moved or
renamed, its content hash recovers the existing pseudonym instead of minting a new one.
"""

from __future__ import annotations

import csv
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .ids import new_id
from .registry import Registry

KEY_HEADER = (
    "original_path",
    "pseudonym_path",
    "student_ids",
    "mentions",
    "sha256_original",
    "sha256_output",
)


@dataclass
class KeyRow:
    pseudo: str
    student_ids: set[str] = field(default_factory=set)
    mentions: set[str] = field(default_factory=set)
    sha_orig: str = ""
    sha_out: str = ""


class PseudonymMap:
    def __init__(self, key_file: Path, registry: Registry) -> None:
        self.key_file = key_file
        self.registry = registry
        self.rows: dict[str, KeyRow] = {}
        self._by_hash: dict[str, str] = {}
        if key_file.exists():
            with key_file.open(newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                header = next(reader, None)
                if header is None or tuple(header) != KEY_HEADER:
                    raise ValueError("key file has unexpected format (from an older inkwell?); move it aside")
                for r in reader:
                    if len(r) != len(KEY_HEADER):
                        continue
                    row = KeyRow(r[1], _split(r[2]), _split(r[3]), r[4], r[5])
                    self.rows[r[0]] = row
                    if row.sha_orig:
                        self._by_hash.setdefault(row.sha_orig, r[0])
        self._taken = {seg for row in self.rows.values() for seg in PurePosixPath(row.pseudo).parts}
        self._taken |= set(registry.students)

    def _new(self, kind: str) -> str:
        ident = new_id(kind, self._taken)
        self._taken.add(ident)
        return ident

    # -- directories ------------------------------------------------------------

    def dir_path(self, rel_dir: PurePosixPath) -> PurePosixPath:
        if str(rel_dir) in ("", "."):
            return PurePosixPath(".")
        key = str(rel_dir) + "/"
        if key in self.rows:
            return PurePosixPath(self.rows[key].pseudo)
        parent = self.dir_path(rel_dir.parent)
        depth = len(rel_dir.parts)
        owners: set[str] = set()
        if depth == 1 and self.registry.is_group_dir(rel_dir.name):
            new = rel_dir.name
        elif depth == 2 and self.registry.is_group_dir(rel_dir.parts[0]):
            student = self.registry.add_folder(rel_dir.parts[0], rel_dir.name)
            new = student.student_id
            owners = {student.student_id}
        else:
            new = self._new("dir")
            owners = set(self.owners_of_dir(rel_dir.parent))
            if depth == 1:  # loose top-level folder, e.g. "<Name> Records xtra info"
                owners |= set(self.registry.match_loose(rel_dir.name))
        out = parent / new
        self.rows[key] = KeyRow(str(out), owners)
        return out

    def owners_of_dir(self, rel_dir: PurePosixPath) -> set[str]:
        if str(rel_dir) in ("", "."):
            return set()
        row = self.rows.get(str(rel_dir) + "/")
        return set(row.student_ids) if row else set()

    # -- files --------------------------------------------------------------------

    def file_path(
        self,
        rel_file: PurePosixPath,
        sha256: str = "",
        still_exists: Callable[[str], bool] = lambda _p: True,
    ) -> PurePosixPath:
        """Pseudonymous path without extension (the handler decides the extension).

        A file whose content hash matches a key row whose original path no longer exists was
        moved or renamed: it keeps that pseudonym's document ID. Identical copies that both
        still exist get distinct IDs."""
        key = str(rel_file)
        if key in self.rows:
            return PurePosixPath(self.rows[key].pseudo)
        parent = self.dir_path(rel_file.parent)
        owners = self.owners_of_dir(rel_file.parent)
        if len(rel_file.parts) == 1:  # loose top-level file, e.g. "JaneRoe.zip"
            owners |= set(self.registry.match_loose(rel_file.stem))
        moved_from = self._by_hash.get(sha256) if sha256 else None
        if moved_from and not still_exists(moved_from):
            name = PurePosixPath(self.rows[moved_from].pseudo).name
            self._by_hash[sha256] = key
        else:
            name = self._new("doc")
        out = parent / name
        self.rows[key] = KeyRow(str(out), owners, sha_orig=sha256)
        if sha256:
            self._by_hash.setdefault(sha256, key)
        return out

    def owners(self, rel_file: str) -> set[str]:
        row = self.rows.get(rel_file)
        return set(row.student_ids) if row else set()

    def record(self, original: str, pseudo: str, owners: set[str] | None = None) -> None:
        """Record a mapping produced inside a worker (zip members, email attachments)."""
        row = self.rows.setdefault(original, KeyRow(pseudo))
        row.pseudo = pseudo
        if owners:
            row.student_ids |= owners

    def set_result(self, rel_file: str, sha_out: str, mentions: set[str]) -> None:
        row = self.rows[rel_file]
        row.sha_out = sha_out
        row.mentions = mentions

    def save(self) -> None:
        """Atomic write with owner-only permissions."""
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.key_file.parent, prefix=".key-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(KEY_HEADER)
                for k in sorted(self.rows):
                    r = self.rows[k]
                    w.writerow(
                        [
                            k,
                            r.pseudo,
                            ";".join(sorted(r.student_ids)),
                            ";".join(sorted(r.mentions)),
                            r.sha_orig,
                            r.sha_out,
                        ]
                    )
            os.replace(tmp, self.key_file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _split(v: str) -> set[str]:
    return {x for x in (v or "").split(";") if x}


def strip_ext(p: str) -> str:
    return re.sub(r"(\.WITHHELD)?\.[A-Za-z0-9]+$", "", p)
