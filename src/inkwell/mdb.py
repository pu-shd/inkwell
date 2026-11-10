"""Read Microsoft Access (.mdb/.accdb) tables with mdbtools.

mdbtools opens files read-only. Commands run with a fixed argv and no shell; the database path is
a single argument. Errors name the tool and its exit code only, never its output (it may hold
record data).
"""

from __future__ import annotations

import csv
import io
import shutil
import sqlite3
import subprocess  # noqa: S404 - fixed argv, no shell
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

TOOLS = ("mdb-tables", "mdb-schema", "mdb-export")
DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SUFFIXES = (".mdb", ".accdb")

Row = dict[str, str | None]


class MdbError(RuntimeError):
    """mdbtools is missing, failed, or returned something unexpected."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # as mdb-schema writes it for SQLite: varchar, INTEGER, DateTime, ...
    pk: bool = False


class Source(Protocol):
    """One Access database: its tables, their columns, and their rows."""

    def columns(self) -> dict[str, list[Column]]: ...

    def rows(self, table: str) -> list[Row]: ...


def require_tools() -> None:
    missing = [t for t in TOOLS if shutil.which(t) is None]
    if missing:
        raise MdbError(f"mdbtools not found ({', '.join(missing)}); install it (brew install mdbtools)")


def _run(argv: list[str]) -> str:
    try:
        res = subprocess.run(argv, capture_output=True, check=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        raise MdbError(f"{argv[0]} failed with exit code {exc.returncode}") from None
    except OSError as exc:
        raise MdbError(f"{argv[0]} could not run: {exc.strerror}") from None
    return res.stdout.decode("utf-8", "replace")


def parse_schema(ddl: str) -> dict[str, list[Column]]:
    """Tables and columns from mdb-schema's SQLite DDL, in file order."""
    con = sqlite3.connect(":memory:")
    try:
        try:
            con.executescript(ddl)
        except sqlite3.Error as exc:
            raise MdbError(f"mdb-schema output could not be parsed: {exc}") from None
        tables = [
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid")
        ]
        return {
            t: [
                Column(name, typ, bool(pk))
                for name, typ, pk in con.execute(
                    "SELECT name, type, pk FROM pragma_table_info(?) ORDER BY cid", (t,)
                )
            ]
            for t in tables
        }
    finally:
        con.close()


def parse_export(text: str, expected: list[str]) -> list[Row]:
    """Rows from mdb-export CSV (quote-doubling, quoted fields may span lines). Empty is None."""
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None:
        return []
    if header != expected:
        raise MdbError("mdb-export columns do not match the schema")
    rows: list[Row] = []
    for rec in reader:
        if len(rec) != len(header):
            raise MdbError("mdb-export returned a row with the wrong number of fields")
        rows.append({k: (v if v != "" else None) for k, v in zip(header, rec, strict=True)})
    return rows


class MdbFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._columns: dict[str, list[Column]] | None = None

    def columns(self) -> dict[str, list[Column]]:
        if self._columns is None:
            # Indexes are kept: without them mdb-schema leaves out the primary keys.
            ddl = _run(
                ["mdb-schema", "--indexes", "--no-relations", "--no-comments", str(self.path), "sqlite"]
            )
            self._columns = parse_schema(ddl)
            listed = [t for t in _run(["mdb-tables", "-1", str(self.path)]).splitlines() if t]
            if sorted(listed) != sorted(self._columns):
                raise MdbError("mdb-tables and mdb-schema disagree about the table list")
        return self._columns

    def rows(self, table: str) -> list[Row]:
        cols = self.columns()[table]
        out = _run(
            ["mdb-export", "-D", DATE_FORMAT, "-T", DATETIME_FORMAT, "-b", "strip", str(self.path), table]
        )
        return parse_export(out, [c.name for c in cols])
