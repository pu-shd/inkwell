"""Building the package that a VM turns into a rebuilt Access database. Synthetic data only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from inkwell.accessbuild import PEOPLE_TABLE, build_package
from inkwell.config import ConfigError

TEXT, LONG, DATE = 10, 4, 8


def _field(name: str, typ: int = TEXT, size: int = 50, **props: str) -> dict[str, object]:
    return {
        "Name": name, "Type": typ, "Size": size, "Attributes": 2, "OrdinalPosition": 0, "Required": False,
        "AllowZeroLength": False, "DefaultValue": "", "ValidationRule": "", "ValidationText": "",
        "Properties": props,
    }  # fmt: skip


def _schema() -> dict[str, object]:
    ssn = {"InputMask": "000\\-00\\-0000;;_", "Description": "Was SSN"}
    return {
        "Source": "x.mdb",
        "DatabaseProperties": {"AppTitle": "Grad DB", "StartupForm": "Startup"},
        "Tables": [
            {"Name": PEOPLE_TABLE, "Properties": {}, "Indexes": [],
             "Fields": [_field("SSN", size=15, **ssn), _field("LastName"), _field("Status"),
                        _field("ApptBegin", DATE, 8)]},
            {"Name": "All Courses", "Properties": {}, "Indexes": [],
             "Fields": [_field("SSN", size=15, **ssn), _field("CourseNumber"), _field("Grade")]},
            {"Name": "TempMSysAccessObjects", "Properties": {}, "Indexes": [],
             "Fields": [_field("Data", 17)]},
        ],
        "Relations": [], "Queries": [],
    }  # fmt: skip


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    sanitized = tmp_path / "sanitized"
    (sanitized / "objects" / "forms").mkdir(parents=True)
    (sanitized / "schema.json").write_text(json.dumps(_schema()))
    (sanitized / "manifest.json").write_text(
        json.dumps([{"Kind": "forms", "Name": "Startup", "File": "objects/forms/001.txt"}])
    )
    (sanitized / "objects" / "forms" / "001.txt").write_text("Begin Form\nEnd\n")
    (sanitized / "audit.json").write_text(json.dumps({"findings": []}))
    db = tmp_path / "scrubbed.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        f'CREATE TABLE "{PEOPLE_TABLE}" (record_id TEXT, student_id TEXT, Status TEXT, ApptBegin TEXT)'
    )
    con.execute(
        f'INSERT INTO "{PEOPLE_TABLE}" VALUES (?, ?, ?, ?)', ("R-AAAAAAAA0", "S-0000000AJ", "1", None)
    )
    con.execute(
        f'INSERT INTO "{PEOPLE_TABLE}" VALUES (?, ?, ?, ?)', ("R-BBBBBBBB1", None, "2", "1994-09-01 00:00:00")
    )
    con.execute('CREATE TABLE "All Courses" (record_id TEXT, student_id TEXT, CourseNumber TEXT)')
    con.execute('INSERT INTO "All Courses" VALUES (?, ?, ?)', ("R-AAAAAAAA0", "S-0000000AJ", "ORF 522"))
    con.commit()
    con.close()
    return sanitized, db, tmp_path / "package"


def _load(package: Path) -> dict[str, object]:
    return json.loads((package / "build.json").read_text())


def test_tables_keep_original_fields_with_removed_ones_empty(inputs: tuple[Path, Path, Path]) -> None:
    build_package(*inputs)
    pkg = _load(inputs[2])
    tables = {t["Name"]: t for t in pkg["Tables"]}  # type: ignore[union-attr]
    assert "TempMSysAccessObjects" not in tables
    courses = tables["All Courses"]
    assert [f["Name"] for f in courses["Fields"]] == ["SSN", "CourseNumber", "Grade"]
    assert courses["Rows"] == [["R-AAAAAAAA0", "ORF 522", None]]  # Grade was withheld: empty


def test_ssn_holds_the_record_id_and_people_gain_student_id(inputs: tuple[Path, Path, Path]) -> None:
    build_package(*inputs)
    people = next(t for t in _load(inputs[2])["Tables"] if t["Name"] == PEOPLE_TABLE)  # type: ignore[union-attr]
    names = [f["Name"] for f in people["Fields"]]
    assert names == ["SSN", "LastName", "Status", "ApptBegin", "StudentID"]
    student = people["Fields"][-1]
    assert student["Type"] == TEXT and student["Size"] == 11
    assert people["Rows"] == [
        ["R-AAAAAAAA0", None, "1", None, "S-0000000AJ"],
        ["R-BBBBBBBB1", None, "2", "1994-09-01 00:00:00", None],
    ]


def test_ssn_input_masks_are_dropped(inputs: tuple[Path, Path, Path]) -> None:
    build_package(*inputs)
    for t in _load(inputs[2])["Tables"]:  # type: ignore[union-attr]
        ssn = next(f for f in t["Fields"] if f["Name"] == "SSN")
        assert "InputMask" not in ssn["Properties"]
        assert ssn["Properties"]["Description"] == "Was SSN"


def test_objects_and_database_properties_travel_along(inputs: tuple[Path, Path, Path]) -> None:
    build_package(*inputs)
    package = inputs[2]
    assert (package / "objects" / "forms" / "001.txt").read_text() == "Begin Form\nEnd\n"
    pkg = _load(package)
    assert pkg["Objects"] == [{"Kind": "forms", "Name": "Startup", "File": "objects/forms/001.txt"}]
    assert pkg["DatabaseProperties"]["StartupForm"] == "Startup"  # type: ignore[index]


def test_a_scrubbed_column_unknown_to_the_schema_is_refused(inputs: tuple[Path, Path, Path]) -> None:
    sanitized, db, package = inputs
    con = sqlite3.connect(db)
    con.execute('ALTER TABLE "All Courses" ADD COLUMN Mystery TEXT')
    con.commit()
    con.close()
    with pytest.raises(ConfigError, match=r"All Courses\.Mystery"):
        build_package(sanitized, db, package)


def test_a_scrubbed_table_unknown_to_the_schema_is_refused(inputs: tuple[Path, Path, Path]) -> None:
    sanitized, db, package = inputs
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE "Scratch" (record_id TEXT)')
    con.commit()
    con.close()
    with pytest.raises(ConfigError, match="Scratch"):
        build_package(sanitized, db, package)


def test_an_object_that_failed_the_audit_is_refused(inputs: tuple[Path, Path, Path]) -> None:
    sanitized, db, package = inputs
    (sanitized / "audit.json").write_text(json.dumps({"findings": [{"object": "forms/Startup", "line": 3}]}))
    with pytest.raises(ConfigError, match="audit"):
        build_package(sanitized, db, package)


def test_a_missing_audit_is_refused(inputs: tuple[Path, Path, Path]) -> None:
    sanitized, db, package = inputs
    (sanitized / "audit.json").unlink()
    with pytest.raises(ConfigError, match="audit"):
        build_package(sanitized, db, package)


def test_text_fields_widen_to_fit_scrubbed_values(inputs: tuple[Path, Path, Path]) -> None:
    # Regression: replacing a name with <KNOWN_STUDENT_NAME> made Award-1 longer than its Text(50).
    sanitized, db, package = inputs
    con = sqlite3.connect(db)
    con.execute('ALTER TABLE "All Courses" ADD COLUMN Grade TEXT')
    con.execute('UPDATE "All Courses" SET CourseNumber = ?', ("x" * 60,))
    con.commit()
    con.close()
    build_package(sanitized, db, package)
    pkg = _load(package)
    courses = next(t for t in pkg["Tables"] if t["Name"] == "All Courses")  # type: ignore[union-attr]
    field = next(f for f in courses["Fields"] if f["Name"] == "CourseNumber")
    assert field["Type"] == TEXT and field["Size"] == 60
    assert pkg["SizeChanges"] == [{"Table": "All Courses", "Field": "CourseNumber", "From": 50, "To": 60}]


def test_text_beyond_255_becomes_memo(inputs: tuple[Path, Path, Path]) -> None:
    sanitized, db, package = inputs
    con = sqlite3.connect(db)
    con.execute('UPDATE "All Courses" SET CourseNumber = ?', ("y" * 300,))
    con.commit()
    con.close()
    build_package(sanitized, db, package)
    courses = next(t for t in _load(package)["Tables"] if t["Name"] == "All Courses")  # type: ignore[union-attr]
    field = next(f for f in courses["Fields"] if f["Name"] == "CourseNumber")
    assert field["Type"] == 12 and field["Size"] == 0


def test_references_default_to_dao_and_ado_when_not_recorded(inputs: tuple[Path, Path, Path]) -> None:
    # The first export did not record references; the code needs DAO (Database) and ADO.
    build_package(*inputs)
    pkg = _load(inputs[2])
    names = [r["Name"] for r in pkg["References"]]  # type: ignore[union-attr]
    assert names == ["DAO", "ADODB"]
    assert "default" in pkg["ReferencesSource"]  # type: ignore[operator]


def test_recorded_references_map_to_modern_libraries(inputs: tuple[Path, Path, Path]) -> None:
    sanitized, db, package = inputs
    schema = json.loads((sanitized / "schema.json").read_text())
    schema["References"] = [
        {
            "Name": "VBA",
            "Guid": "{000204EF-0000-0000-C000-000000000046}",
            "Major": 4,
            "Minor": 0,
            "BuiltIn": True,
        },
        {
            "Name": "DAO",
            "Guid": "{00025E01-0000-0000-C000-000000000046}",
            "Major": 5,
            "Minor": 0,
            "BuiltIn": False,
        },
        {
            "Name": "ADODB",
            "Guid": "{2A75196C-D9EB-4129-B803-931327F72D5C}",
            "Major": 2,
            "Minor": 8,
            "BuiltIn": False,
        },
        {
            "Name": "Office",
            "Guid": "{2DF8D04C-5BFA-101B-BDE5-00AA0044DE52}",
            "Major": 2,
            "Minor": 5,
            "BuiltIn": False,
        },
    ]
    (sanitized / "schema.json").write_text(json.dumps(schema))
    build_package(sanitized, db, package)
    refs = _load(package)["References"]
    assert [(r["Name"], r["Guid"], r["Major"], r["Minor"]) for r in refs] == [  # type: ignore[union-attr]
        ("DAO", "{4AC9E1DA-5BAD-4AC7-86E3-24F4CDCECA28}", 12, 0),
        ("ADODB", "{B691E011-1797-432E-907A-4D8C69339129}", 6, 1),
        ("Office", "{2DF8D04C-5BFA-101B-BDE5-00AA0044DE52}", 2, 5),
    ]
