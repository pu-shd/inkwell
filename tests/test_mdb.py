"""mdbtools wrapper: schema and CSV parsing, argv, errors. subprocess is mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from inkwell import mdb
from inkwell.ids import is_valid, new_id
from inkwell.mdb import Column, MdbError, MdbFile, parse_export, parse_schema
from inkwell.registry import Registry

DDL = """-- MDB Tools comment
CREATE TABLE `People Info`
 (
\t`SSN`\t\t\tvarchar,
\t`Last Name`\t\t\tvarchar,
\t`ApptBegin`\t\t\tDateTime,
\t`Readmit`\t\t\tINTEGER
\t, PRIMARY KEY (`SSN`)
);
CREATE INDEX `People Info_Zip_idx` ON `People Info` (`Last Name`);
CREATE TABLE `AI/ESL Information`
 (
\t`SSN`\t\t\tvarchar,
\t`Comments`\t\t\tvarchar
);
"""

EXPORT = 'SSN,Comments\n"AbCdEfGhJ","line one\nline ""two"", with comma"\n"KmNpQrStV",\n'


def test_parse_schema_reads_tables_columns_types_and_keys() -> None:
    got = parse_schema(DDL)
    assert list(got) == ["People Info", "AI/ESL Information"]
    assert got["People Info"] == [
        Column("SSN", "varchar", True),
        Column("Last Name", "varchar", False),
        Column("ApptBegin", "DateTime", False),
        Column("Readmit", "INTEGER", False),
    ]


def test_parse_schema_rejects_garbage() -> None:
    with pytest.raises(MdbError, match="could not be parsed"):
        parse_schema("CREATE TABLE (")


def test_parse_export_handles_multiline_quotes_and_nulls() -> None:
    rows = parse_export(EXPORT, ["SSN", "Comments"])
    assert rows == [
        {"SSN": "AbCdEfGhJ", "Comments": 'line one\nline "two", with comma'},
        {"SSN": "KmNpQrStV", "Comments": None},
    ]


def test_parse_export_rejects_a_header_that_differs_from_the_schema() -> None:
    with pytest.raises(MdbError, match="do not match"):
        parse_export(EXPORT, ["SSN", "Notes"])


def test_parse_export_rejects_ragged_rows() -> None:
    with pytest.raises(MdbError, match="wrong number"):
        parse_export('SSN,Comments\n"a"\n', ["SSN", "Comments"])


class _FakeRun:
    def __init__(self, outputs: dict[str, str], fail: str = "") -> None:
        self.outputs, self.fail, self.calls = outputs, fail, []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, kw))
        assert kw.get("shell") is not True
        if argv[0] == self.fail:
            raise subprocess.CalledProcessError(3, argv, b"", b"secret row data")
        return subprocess.CompletedProcess(argv, 0, self.outputs[argv[0]].encode(), b"")


def test_mdbfile_runs_fixed_argv_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRun(
        {"mdb-schema": DDL, "mdb-tables": "People Info\nAI/ESL Information\n", "mdb-export": EXPORT}
    )
    monkeypatch.setattr(mdb.subprocess, "run", fake)
    path = Path("/data/Grad  DB; rm -rf.mdb")
    f = MdbFile(path)
    assert f.rows("AI/ESL Information")[0]["SSN"] == "AbCdEfGhJ"
    export = next(argv for argv, _ in fake.calls if argv[0] == "mdb-export")
    assert export[-2:] == [str(path), "AI/ESL Information"]  # path and table are single arguments
    assert "-D" in export and "-T" in export


def test_mdbfile_error_hides_tool_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mdb.subprocess, "run", _FakeRun({"mdb-tables": ""}, fail="mdb-schema"))
    with pytest.raises(MdbError) as exc:
        MdbFile(Path("x.mdb")).columns()
    assert "exit code 3" in str(exc.value)
    assert "secret" not in str(exc.value)


def test_mdbfile_refuses_table_list_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mdb.subprocess, "run", _FakeRun({"mdb-schema": DDL, "mdb-tables": "People Info\n", "mdb-export": ""})
    )
    with pytest.raises(MdbError, match="disagree"):
        MdbFile(Path("x.mdb")).columns()


def test_require_tools_names_the_missing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mdb.shutil, "which", lambda t: None if t == "mdb-export" else "/usr/bin/" + t)
    with pytest.raises(MdbError, match="mdb-export"):
        mdb.require_tools()


def test_record_ids_are_valid_and_distinct() -> None:
    ids = {new_id("record") for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("R-") and is_valid(i) for i in ids)
    assert not is_valid("R-0000000AJ"[:-1] + "0")


def test_registry_add_person_has_no_folder_and_a_note(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "registry.csv")
    s = reg.add_person("Brindlewick  Osterhaud", {"Brindlewick Q Osterhaud"}, "from a database")
    assert s.canonical_name == "Brindlewick Osterhaud"
    assert s.folders == set() and s.needs_review == "from a database"
    reg.save()
    again = Registry(tmp_path / "registry.csv")
    assert again.students[s.student_id].aliases == {"Brindlewick Q Osterhaud"}
