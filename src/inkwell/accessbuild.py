"""Assemble the package a Windows VM turns into a rebuilt Access database (access/guest/build.ps1).

Inputs: the audited, sanitized export of the original (schema.json, manifest.json, objects/) and
the scrubbed SQLite written by inkwell-db. The package (build.json plus objects/) keeps the
original's tables, fields and properties; removed columns stay and are empty; the record key
column (SSN) holds the record ID; the people table gains StudentID (the registry ID).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .accessdb import PEOPLE_TABLE, SUBJECT_COLUMN
from .config import ConfigError

DB_TEXT = 10
DB_MEMO = 12
TEXT_MAX = 255
STUDENT_ID_FIELD = "StudentID"
# Access-internal tables that are not part of the database's design.
SKIP_TABLES = frozenset({"TempMSysAccessObjects"})

# VBA references: the modern library for each old one (by GUID); built-in ones come with Access.
ACE_DAO = {"Name": "DAO", "Guid": "{4AC9E1DA-5BAD-4AC7-86E3-24F4CDCECA28}", "Major": 12, "Minor": 0}
ADO_61 = {"Name": "ADODB", "Guid": "{B691E011-1797-432E-907A-4D8C69339129}", "Major": 6, "Minor": 1}
_MODERN = {
    "{00025E01-0000-0000-C000-000000000046}": ACE_DAO,  # Microsoft DAO 3.x Object Library
    "{4AC9E1DA-5BAD-4AC7-86E3-24F4CDCECA28}": ACE_DAO,
}
_ADO_NAMES = frozenset({"ADODB", "ADOR"})

__all__ = ["PEOPLE_TABLE", "build_package"]


def _references(recorded: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], str]:
    if recorded is None:
        # The export did not record them; the code uses DAO (Database) and ADO (ADODB.*).
        return [ACE_DAO, ADO_61], "default (the export did not record references): DAO and ADO"
    out: list[dict[str, Any]] = []
    for r in recorded:
        if r.get("BuiltIn"):
            continue
        guid = str(r.get("Guid", "")).upper()
        modern = _MODERN.get(guid) or (ADO_61 if r.get("Name") in _ADO_NAMES else None)
        ref = dict(modern) if modern else {k: r[k] for k in ("Name", "Guid", "Major", "Minor")}
        if ref not in out:
            out.append(ref)
    return out, "recorded in the export, mapped to current libraries"


def _sqlite_tables(db: Path) -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        out = {}
        for n in names:
            cur = con.execute(f'SELECT * FROM "{n.replace(chr(34), chr(34) * 2)}"')  # noqa: S608
            out[n] = ([d[0] for d in cur.description], cur.fetchall())
        return out
    finally:
        con.close()


def _student_id_field() -> dict[str, Any]:
    return {
        "Name": STUDENT_ID_FIELD,
        "Type": DB_TEXT,
        "Size": 11,
        "Attributes": 2,
        "Required": False,
        "AllowZeroLength": False,
        "DefaultValue": "",
        "ValidationRule": "",
        "ValidationText": "",
        "Properties": {"Description": "Registry student ID (S-...), links to the scrubbed documents"},
    }


def build_package(sanitized: Path, scrubbed_db: Path, package: Path) -> dict[str, int]:
    """Write `package`/build.json and `package`/objects. Returns the row count per table."""
    audit = sanitized / "audit.json"
    if not audit.is_file():
        raise ConfigError("the export has no audit.json; audit it before building")
    findings = json.loads(audit.read_text(encoding="utf-8")).get("findings")
    if findings:
        raise ConfigError(f"the audit of the export has {len(findings)} findings; resolve them first")

    schema = json.loads((sanitized / "schema.json").read_text(encoding="utf-8-sig"))
    manifest = json.loads((sanitized / "manifest.json").read_text(encoding="utf-8-sig"))
    data = _sqlite_tables(scrubbed_db)
    by_name = {t["Name"]: t for t in schema["Tables"] if t["Name"] not in SKIP_TABLES}

    unknown = [n for n in data if n not in by_name]
    if unknown:
        raise ConfigError(f"scrubbed tables not in the original schema: {', '.join(unknown)}")

    tables, counts, size_changes = [], {}, []
    for name, table in by_name.items():
        fields = []
        for f in table["Fields"]:
            f = {**f, "Properties": dict(f.get("Properties", {}))}
            if f["Name"] == SUBJECT_COLUMN:
                f["Properties"].pop("InputMask", None)  # 000-00-0000 would reject R- record IDs
            fields.append(f)
        if name == PEOPLE_TABLE:
            fields.append(_student_id_field())
        field_names = [f["Name"] for f in fields]

        columns, rows = data.get(name, ([], []))
        extra = [c for c in columns if c not in ("record_id", "student_id") and c not in field_names]
        if extra:
            raise ConfigError(
                "scrubbed columns not in the original schema: " + ", ".join(f"{name}.{c}" for c in extra)
            )
        out_rows = []
        for row in rows:
            values = dict(zip(columns, row, strict=True))
            out_row = []
            for fname in field_names:
                if fname == SUBJECT_COLUMN:
                    out_row.append(values.get("record_id"))
                elif fname == STUDENT_ID_FIELD and name == PEOPLE_TABLE:
                    out_row.append(values.get("student_id"))
                else:
                    out_row.append(values.get(fname))
            out_rows.append(out_row)
        # Scrubbing can lengthen text (a name becomes <KNOWN_STUDENT_NAME>): widen to fit, up to
        # Text's 255 characters, then Memo.
        for k, f in enumerate(fields):
            if f["Type"] != DB_TEXT:
                continue
            longest = max((len(v) for r in out_rows if isinstance(v := r[k], str)), default=0)
            if longest > int(f["Size"]):
                new_type, new_size = (DB_TEXT, longest) if longest <= TEXT_MAX else (DB_MEMO, 0)
                size_changes.append(
                    {"Table": name, "Field": f["Name"], "From": f["Size"], "To": new_size or "Memo"}
                )
                fields[k] = {**f, "Type": new_type, "Size": new_size}
        tables.append({**table, "Fields": fields, "Rows": out_rows})
        counts[name] = len(out_rows)

    package.mkdir(parents=True, exist_ok=True)
    objects = package / "objects"
    if objects.exists():
        shutil.rmtree(objects)
    shutil.copytree(sanitized / "objects", objects)
    build = {
        "Source": schema.get("Source"),
        "DatabaseProperties": schema.get("DatabaseProperties", {}),
        "Tables": tables,
        "Relations": schema.get("Relations", []),
        "Objects": manifest,
        "SizeChanges": size_changes,
    }
    build["References"], build["ReferencesSource"] = _references(schema.get("References"))
    (package / "build.json").write_text(json.dumps(build, indent=1, ensure_ascii=False), encoding="utf-8")
    return counts
