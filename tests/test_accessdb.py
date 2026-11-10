"""inkwell-db end to end, on a synthetic Access source. Synthetic identities only."""

from __future__ import annotations

import csv
import json
import sqlite3
import stat
from pathlib import Path

import pytest

from inkwell import accessdb
from inkwell.accessdb import (
    CODE,
    DROP,
    FACULTY,
    KEEP,
    PEOPLE_TABLE,
    POLICY,
    TEXT,
    WITHHOLD,
    Dropped,
    LeakCheck,
    check_policy,
    faculty_people,
    is_code,
    replace_names,
)
from inkwell.config import ConfigError
from inkwell.ids import is_valid
from inkwell.mdb import Column, Row
from inkwell.recognizers import KnownNameRecognizer
from inkwell.registry import Registry, Student

from .conftest import OTHER_SID, SID, SSN

TOK_Q = "QxTkvPmRa"  # Quintavius Fernhollow: in the registry (exact match)
TOK_B = "BwzLqNdYe"  # Brindlewick Osterhaud: database only (new)
TOK_T = "HmVtRcXuo"  # Tamsin Okonkwo: registry has "Tamsin Okonkwo-Reyes" (partial)
TOK_O = "OrPhNtKen"  # a course row with no person
TAMSIN_SID = "S-2222222CW"
EXAMS = "General Exams RAC Meetings MSE MEngr PhD FPO"

SECRETS = [
    "Quintavius",
    "Fernhollow",
    "Brindlewick",
    "Osterhaud",
    "Ottoline",
    "Tamsin",
    "Okonkwo",
    TOK_Q,
    TOK_B,
    TOK_T,
    TOK_O,
    "qfern@example.edu",
    "555-0199",
    "Elmwood",
    SSN,
    "Stochastic Lattices of the Upper Delaware",
]


def _c(name: str, typ: str = "varchar", pk: bool = False) -> Column:
    return Column(name, typ, pk)


def _schema() -> dict[str, list[Column]]:
    return {
        PEOPLE_TABLE: [
            _c("SSN", pk=True),
            _c("LastName"),
            _c("FirstName"),
            _c("MiddleName"),
            _c("E-mail"),
            _c("HomePhoneNumber"),
            _c("StreetorAptNumber"),
            _c("Gender"),
            _c("Status"),
            _c("ApptBegin", "DateTime"),
        ],
        "Academic Information": [
            _c("SSN", pk=True),
            _c("Degree"),
            _c("Advisor(s)-1"),
            _c("Award-1"),
            _c("Comments"),
            _c("ReadmitAction", "INTEGER"),
        ],
        "All Courses": [_c("SSN"), _c("CourseNumber"), _c("Grade")],
        EXAMS: [_c("SSN", pk=True), _c("PhD-ThesisTitle"), _c("FPO-Committee-1"), _c("PhD-TimeToDegree")],
    }


def _rows() -> dict[str, list[Row]]:
    return {
        PEOPLE_TABLE: [
            {
                "SSN": TOK_Q,
                "LastName": "Fernhollow",
                "FirstName": "Quintavius",
                "MiddleName": None,
                "E-mail": "qfern@example.edu",
                "HomePhoneNumber": "(609) 555-0199",
                "StreetorAptNumber": "12 Elmwood Lane",
                "Gender": "M",
                "Status": "G",
                "ApptBegin": "1994-09-01 00:00:00",
            },
            {
                "SSN": TOK_B,
                "LastName": "Osterhaud",
                "FirstName": "Brindlewick",
                "MiddleName": "Q",
                "E-mail": None,
                "HomePhoneNumber": None,
                "StreetorAptNumber": None,
                "Gender": "F",
                "Status": "C",
                "ApptBegin": None,
            },
            {
                "SSN": TOK_T,
                "LastName": "Okonkwo",
                "FirstName": "Tamsin",
                "MiddleName": None,
                "E-mail": None,
                "HomePhoneNumber": None,
                "StreetorAptNumber": None,
                "Gender": None,
                "Status": "C",
                "ApptBegin": None,
            },
        ],
        "Academic Information": [
            {
                "SSN": TOK_Q,
                "Degree": "P",
                "Advisor(s)-1": "Ada Lovelace",
                "Award-1": "Osterhaud Prize",
                "Comments": f"Quintavius Fernhollow met Ada Lovelace; SSN {SSN}; call (609) 555-0142.",
                "ReadmitAction": "0",
            },
            {
                "SSN": TOK_B,
                "Degree": "M",
                "Advisor(s)-1": "Lovelace/Babbage",
                "Award-1": None,
                "Comments": "Brindlewick asked about funding.",
                "ReadmitAction": "1",
            },
        ],
        "All Courses": [
            {"SSN": TOK_Q, "CourseNumber": "ORF 522", "Grade": "A+"},
            {"SSN": TOK_O, "CourseNumber": "ORF 523", "Grade": "B"},
        ],
        EXAMS: [
            {
                "SSN": TOK_Q,
                "PhD-ThesisTitle": "Stochastic Lattices of the Upper Delaware",
                "FPO-Committee-1": "Ottoline Fernhollow",
                "PhD-TimeToDegree": "5.0",
            }
        ],
    }


class FakeSource:
    def __init__(self, schema: dict[str, list[Column]], rows: dict[str, list[Row]]) -> None:
        self._schema, self._rows = schema, rows

    def columns(self) -> dict[str, list[Column]]:
        return self._schema

    def rows(self, table: str) -> list[Row]:
        return [dict(r) for r in self._rows.get(table, [])]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {"in": tmp_path / "in", "out": tmp_path / "out", "key": tmp_path / "secret" / "key.csv"}
    paths["in"].mkdir()
    paths["key"].parent.mkdir()
    (paths["in"] / "Grad   DB.mdb").write_bytes(b"not read: the source is faked")
    reg = Registry(paths["key"].parent / "registry.csv")
    reg.students[SID] = Student(SID, "Quintavius Fernhollow", {"Fernhollow, Quintavius"}, {"1990-91"})
    reg.students[OTHER_SID] = Student(OTHER_SID, "Ottoline Fernhollow", {"Fernhollow, Ottoline"})
    reg.students[TAMSIN_SID] = Student(TAMSIN_SID, "Tamsin Okonkwo-Reyes", {"Okonkwo-Reyes, Tamsin"})
    reg.save()
    monkeypatch.setenv("INKWELL_DB_INPUT_DIR", str(paths["in"]))
    monkeypatch.setenv("INKWELL_DB_OUTPUT_DIR", str(paths["out"]))
    monkeypatch.setenv("INKWELL_KEY_FILE", str(paths["key"]))
    monkeypatch.setattr(accessdb, "require_tools", lambda: None)
    source = FakeSource(_schema(), _rows())
    monkeypatch.setattr(accessdb, "open_source", lambda p: source)
    return paths


def _table(out: Path, table: str) -> list[dict[str, object]]:
    con = sqlite3.connect(out / "Grad DB.sqlite")
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')]  # noqa: S608
    finally:
        con.close()


def _dbkey(key_dir: Path) -> dict[str, dict[str, str]]:
    with (key_dir / "db-key.csv").open(newline="") as fh:
        return {r["record_token"]: r for r in csv.DictReader(fh)}


def _all_output_text(out: Path) -> str:
    return "\n".join(
        p.read_bytes().decode("utf-8", "replace") + p.name for p in out.rglob("*") if p.is_file()
    )


# --- policy ----------------------------------------------------------------------------------

# The production database's columns (names only). POLICY must cover every one of them.
ORFE_COLUMNS = {
    "Academic Information": "SSN Degree YearOfEntry YearOfStudy ProgramOfStudy Advisor(s)-1 Advisor(s)-2 "
    "AdvisorChange-1 AdvisorChange-2 DateOfChange Award-1 Award-2 Award-3 Award-4 Award-5 Comments "
    "ReadmitAction",
    "AI/ESL Information": "SSN AITraining AITrainingDate ESLEvaluation ESLEvaluationDate ESLTraining "
    "ESLTrainingDate ESLTrainingResult CourseComments",
    "All AI Courses": "SSN AICourseNumber AIYear AISemester Instructor",
    "All Courses": "SSN CourseNumber Semesters Grade YearTaken",
    "CIV 509/510 Research Seminar": "SSN CIV509SeminarDate CIV509SeminarTopic CIV510SeminarDate "
    "CIV510SeminarTopic",
    "Course Comments": "SSN CourseComments",
    PEOPLE_TABLE: "SSN LastName FirstName MiddleName StreetorAptNumber |2nd Address| City State ZipCode "
    "PhoneNumber E-mail OfficeRoomNumber OfficePhoneNumber HomeAddress HomeCity HomeState HomeZipCode "
    "HomeCountry HomePhoneNumber Gender DateOfBirth MaritalStatus MinorityStatus Citizenship UgInstitution "
    "VisaStatus PermanentResident PhoneGivenOut SupportS SupportI SupportR SupportF Status ApptBegin ApptEnd",
    "Employment and Forwarding Address": "SSN Employer/Institution Employer Position EmployerStreet "
    "EmployerCity EmployerState EmployerZipCode EmployerCountry |Second Employer| |Second Employer Position| "
    "EmployerPhoneNumber |Current Employer| |Current Position| ForwardingAddress ForwardingStreet "
    "ForwardingCity ForwardingState ForwardingZipCode ForwardingCountry ForwardingPhoneNumber",
    EXAMS: "SSN GE-YearTaken GE-MonthTaken GE-EEWR GE-MMS GE-SOR RAC-Date-1 RAC-ReportSubmitted-1 RAC-Date-2 "
    "RAC-ReportSubmitted-2 RAC-Date-3 RAC-ReportSubmitted-3 RAC-Date-4 RAC-ReportSubmitted-4 MSE-ThesisTitle "
    "MSE-TimeToMasters MEngr-Program PhD-ThesisTitle PhD-TimeToDegree PhD-TimeToFPO FPO-Committee-1 "
    "FPO-Committee-2 FPO-Committee-3 FPO-Committee-4 FPO-Completed",
    "Program of Study Choices": "ProgramOfStudy",
    "Support Information (currently not in use)": "SSN |Fill In Later|",
    "Admissions": "SSN RankOrderNumber GREVerbal GREQuantitative GREAnalytical TOEFLScore TSE TWE UGSchool "
    "UGFieldOfStudy UGDegree UGGraduationDate UGGPAMajor UGGPAOverall GradSchool GradFieldOfStudy GradDegree "
    "GradGraduationDate GradGPAMajor GradGPAOverall",
}


def _names(spec: str) -> list[str]:
    import re

    return [a or b for a, b in re.findall(r"\|([^|]+)\||(\S+)", spec)]


def test_policy_covers_the_production_schema_exactly() -> None:
    schema = {t: [_c(n) for n in _names(spec)] for t, spec in ORFE_COLUMNS.items()}
    check_policy(schema)  # raises on any column without a treatment
    assert {t: sorted(c) for t, c in POLICY.items()} == {
        t: sorted(_names(s)) for t, s in ORFE_COLUMNS.items()
    }


def test_policy_protects_identifiers_and_protected_records() -> None:
    people = POLICY[PEOPLE_TABLE]
    for col in ("FirstName", "LastName", "E-mail", "HomeAddress", "DateOfBirth", "HomePhoneNumber"):
        assert people[col] == DROP
    for col in ("Gender", "MinorityStatus", "Citizenship", "SupportF"):
        assert people[col] == WITHHOLD
    assert POLICY["All Courses"]["Grade"] == WITHHOLD
    assert POLICY["Admissions"]["GREQuantitative"] == WITHHOLD
    assert POLICY[EXAMS]["PhD-ThesisTitle"] == DROP
    assert POLICY[EXAMS]["FPO-Committee-1"] == FACULTY
    assert POLICY["Academic Information"]["Comments"] == TEXT
    assert POLICY["Academic Information"]["Award-1"] == KEEP
    assert POLICY["All Courses"]["CourseNumber"] == CODE


@pytest.mark.parametrize("value", ["ECO 525", "MA", "1994-09-01 00:00:00", "2012 (EE)", "5.0", "F/S"])
def test_code_shapes(value: str) -> None:
    assert is_code(value)


@pytest.mark.parametrize("value", ["Eco 525", "Fall", "WANGS", "Morgan Stanley", ""])
def test_not_code_shapes(value: str) -> None:
    assert not is_code(value) or value == ""


def test_unknown_column_is_refused() -> None:
    schema = _schema()
    schema["Academic Information"].append(_c("Mystery Notes"))
    with pytest.raises(ConfigError, match=r"Academic Information\.Mystery Notes"):
        check_policy(schema)


def test_unknown_table_is_refused() -> None:
    schema = _schema()
    schema["Scratch"] = [_c("SSN")]
    with pytest.raises(ConfigError, match=r"Scratch\.SSN"):
        check_policy(schema)


def test_database_without_people_table_is_refused() -> None:
    schema = _schema()
    del schema[PEOPLE_TABLE]
    with pytest.raises(ConfigError, match="record subjects cannot be named"):
        check_policy(schema)


# --- units -----------------------------------------------------------------------------------


def test_replace_names_full_name_gets_id_and_single_word_is_generic() -> None:
    known = KnownNameRecognizer([(SID, ["Fernhollow, Quintavius"]), (OTHER_SID, ["Fernhollow, Ottoline"])])
    out, n = replace_names(known, "Quintavius Fernhollow and Quintavius won", "", full_only=False)
    assert out == f"<{SID}> and <KNOWN_STUDENT_NAME> won"
    assert n == 2
    out, n = replace_names(known, "Ada Quintavius", "", full_only=True)
    assert (out, n) == ("Ada Quintavius", 0)


def test_faculty_people_splits_and_keeps_full_names_only() -> None:
    got = sorted(
        p.name
        for p in faculty_people(["Ada Lovelace/Charles Babbage", "Babbage", "Grace Hopper and Lovelace"])
    )
    assert got == ["Ada Lovelace", "Charles Babbage", "Grace Hopper"]


# --- end to end ------------------------------------------------------------------------------


def test_run_writes_scrubbed_sqlite_and_csv(env: dict[str, Path]) -> None:
    assert accessdb.main([]) == 0
    out, key_dir = env["out"], env["key"].parent
    assert (out / "Grad DB.sqlite").is_file()
    assert (out / "Grad DB" / "Academic Information.csv").is_file()
    assert (out / "Grad DB.schema.json").is_file()

    text = _all_output_text(out)
    leaked = [s for s in SECRETS if s in text]
    assert leaked == []

    keys = _dbkey(key_dir)
    assert keys[TOK_Q]["student_id"] == SID and keys[TOK_Q]["match"] == "exact"
    assert keys[TOK_T]["student_id"] == TAMSIN_SID and keys[TOK_T]["match"] == "partial"
    assert keys[TOK_B]["match"] == "new"
    assert keys[TOK_O]["match"] == "orphan" and keys[TOK_O]["student_id"] == ""
    assert all(is_valid(r["record_id"]) and r["record_id"].startswith("R-") for r in keys.values())

    reg = Registry(key_dir / "registry.csv")
    new = reg.students[keys[TOK_B]["student_id"]]
    assert new.canonical_name == "Brindlewick Osterhaud"
    assert "Brindlewick Q Osterhaud" in new.aliases
    assert new.needs_review.startswith("added by inkwell-db")
    assert len(reg.students) == 4

    people = _table(out, PEOPLE_TABLE)
    assert set(people[0]) == {"record_id", "student_id", "Status", "ApptBegin"}
    acad = {r["student_id"]: r for r in _table(out, "Academic Information")}
    q = acad[SID]
    assert q["record_id"] == keys[TOK_Q]["record_id"]
    assert q["Advisor(s)-1"] == "Ada Lovelace"
    assert q["Award-1"] == "<KNOWN_STUDENT_NAME> Prize"
    assert f"<{SID}>" in str(q["Comments"]) and "Ada Lovelace" in str(q["Comments"])
    assert "555-0142" not in str(q["Comments"])
    assert q["ReadmitAction"] == 0
    assert acad[keys[TOK_B]["student_id"]]["Advisor(s)-1"] == "Lovelace/Babbage"
    assert set(_table(out, "All Courses")[0]) == {"record_id", "student_id", "CourseNumber"}
    exams = _table(out, EXAMS)[0]
    assert "PhD-ThesisTitle" not in exams
    assert exams["FPO-Committee-1"] == f"<{OTHER_SID}>"  # a record subject on a committee stays hidden

    schema = json.loads((out / "Grad DB.schema.json").read_text())
    grade = next(c for c in schema["All Courses"] if c["name"] == "Grade")
    assert grade == {
        "name": "Grade",
        "type": "varchar",
        "primary_key": False,
        "action": "withhold",
        "output": [],
    }

    summary = json.loads((out / "_inkwell_db_summary.json").read_text())
    assert summary["databases"]["Grad DB"]["status"] == "written"
    assert summary["records"] == {"exact": 1, "new": 1, "orphan": 1, "partial": 1}

    faculty = (key_dir / "db-faculty.csv").read_text()
    assert "Ada Lovelace" in faculty
    assert "Ottoline" not in faculty
    assert "Ottoline Fernhollow" in (key_dir / "db-faculty-conflicts.csv").read_text()
    for name in ("db-key.csv", "registry.csv", "db-faculty.csv", "db-faculty-conflicts.csv"):
        assert stat.S_IMODE((key_dir / name).stat().st_mode) == 0o600


def test_rerun_keeps_ids_and_does_not_duplicate_people(env: dict[str, Path]) -> None:
    key_dir = env["key"].parent
    assert accessdb.main([]) == 0
    first_key, first_reg = (key_dir / "db-key.csv").read_text(), (key_dir / "registry.csv").read_text()
    first_rows = _table(env["out"], "Academic Information")
    assert accessdb.main([]) == 0
    assert (key_dir / "db-key.csv").read_text() == first_key
    assert (key_dir / "registry.csv").read_text() == first_reg
    assert _table(env["out"], "Academic Information") == first_rows


def test_hand_edited_student_id_is_respected(env: dict[str, Path]) -> None:
    key_dir = env["key"].parent
    assert accessdb.main([]) == 0
    path = key_dir / "db-key.csv"
    rows = list(csv.DictReader(path.open(newline="")))
    for r in rows:
        if r["record_token"] == TOK_T:
            r["student_id"], r["match"] = OTHER_SID, "edited"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert accessdb.main([]) == 0
    assert _dbkey(key_dir)[TOK_T]["student_id"] == OTHER_SID


def test_db_key_with_unknown_student_is_a_config_error(env: dict[str, Path]) -> None:
    key_dir = env["key"].parent
    assert accessdb.main([]) == 0
    path = key_dir / "db-key.csv"
    path.write_text(path.read_text().replace(SID, "S-9999999Z0"))
    assert accessdb.main([]) == 1


def test_dry_run_writes_only_a_private_report(env: dict[str, Path]) -> None:
    key_dir = env["key"].parent
    before = (key_dir / "registry.csv").read_text()
    assert accessdb.main(["--dry-run"]) == 0
    assert not env["out"].exists()
    assert (key_dir / "registry.csv").read_text() == before
    assert not (key_dir / "db-key.csv").exists()
    reports = list(key_dir.glob("db-dryrun-*.csv"))
    assert len(reports) == 1
    assert stat.S_IMODE(reports[0].stat().st_mode) == 0o600
    assert not [p for p in key_dir.iterdir() if p.name.startswith(".inkwell-db-")]


def test_leak_check_flags_each_kind_of_leak() -> None:
    known = KnownNameRecognizer([(SID, ["Fernhollow, Quintavius"])])
    check = LeakCheck(known, [TOK_B], Dropped(phones={"6095550199"}, streets={"12 elmwood"}))
    assert check.cell(f"ok {SSN}", KEEP) == ["ssn_pattern"]
    assert check.cell(f"x{TOK_B}x", KEEP) == ["record_token"]
    assert check.cell("write to a@b.org", KEEP) == ["email"]
    assert check.cell("tel 609.555.0199", KEEP) == ["dropped_phone"]
    assert check.cell("near 12  Elmwood".replace("  ", " "), KEEP) == ["dropped_address"]
    assert check.cell("Quintavius", KEEP) == ["known_student_name"]
    assert check.cell("Quintavius", FACULTY) == []  # one word in a faculty column
    assert check.cell("Quintavius Fernhollow", FACULTY) == ["known_student_name"]
    assert check.cell("ECO 525", CODE) == []
    assert check.cell("Ada Lovelace, 1994", KEEP) == []


def test_leak_in_a_kept_column_blocks_the_database(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows["Academic Information"][1]["Award-1"] = f"see record {TOK_Q}"  # no scrubber knows tokens
    monkeypatch.setattr(accessdb, "open_source", lambda p: FakeSource(_schema(), rows))
    assert accessdb.main([]) == 2
    out = env["out"]
    assert not (out / "Grad DB.sqlite").exists()
    assert not (out / "Grad DB").exists()
    assert TOK_Q not in _all_output_text(out)
    summary = json.loads((out / "_inkwell_db_summary.json").read_text())
    db = summary["databases"]["Grad DB"]
    assert db["status"] == "leak_blocked"
    assert {"table": "Academic Information", "column": "Award-1", "reason": "record_token"} in [
        {k: v for k, v in leak.items() if k in ("table", "column", "reason")} for leak in db["leaks"]
    ]
    assert not [p for p in out.iterdir() if p.name.startswith(".inkwell-db-")]


def test_leak_check_catches_a_dropped_address_no_recognizer_sees(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[PEOPLE_TABLE][0]["StreetorAptNumber"] = "12 Elmwood"  # no street suffix
    rows["All Courses"][0]["CourseNumber"] = "ORF 522 met at 12 Elmwood"
    monkeypatch.setattr(accessdb, "open_source", lambda p: FakeSource(_schema(), rows))
    assert accessdb.main([]) == 2
    leaks = json.loads((env["out"] / "_inkwell_db_summary.json").read_text())["databases"]["Grad DB"]["leaks"]
    assert {(leak["column"], leak["reason"]) for leak in leaks} == {("CourseNumber", "dropped_address")}


def test_unknown_column_fails_before_writing(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    schema = _schema()
    schema["All Courses"].append(_c("Instructor Notes"))
    monkeypatch.setattr(accessdb, "open_source", lambda p: FakeSource(schema, _rows()))
    before = (env["key"].parent / "registry.csv").read_text()
    assert accessdb.main([]) == 1
    assert not env["out"].exists()
    assert (env["key"].parent / "registry.csv").read_text() == before


def test_no_databases_exits_3(env: dict[str, Path]) -> None:
    for p in env["in"].iterdir():
        p.unlink()
    assert accessdb.main([]) == 3


def test_missing_configuration_exits_1(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INKWELL_DB_INPUT_DIR")
    assert accessdb.main([]) == 1


def test_output_inside_input_is_refused(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKWELL_DB_OUTPUT_DIR", str(env["in"] / "out"))
    with pytest.raises(ConfigError, match="INKWELL_DB_OUTPUT_DIR must not be"):
        accessdb.db_config()


def test_contact_details_in_a_kept_column_are_replaced_not_blocked(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    schema, rows = _schema(), _rows()
    schema["Employment and Forwarding Address"] = [_c("SSN", pk=True), _c("Employer"), _c("EmployerStreet")]
    rows["Employment and Forwarding Address"] = [
        {
            "SSN": TOK_Q,
            "Employer": "Acme Analytics, 12 Elmwood Lane; contact qfern@example.edu",
            "EmployerStreet": "12 Elmwood Lane",
        }
    ]
    monkeypatch.setattr(accessdb, "open_source", lambda p: FakeSource(schema, rows))
    assert accessdb.main([]) == 0
    employer = str(_table(env["out"], "Employment and Forwarding Address")[0]["Employer"])
    assert employer.startswith("Acme Analytics, ")
    assert "Elmwood" not in employer and "qfern" not in employer
    assert "<EMAIL_ADDRESS>" in employer


def test_course_codes_survive_a_student_named_like_a_prefix(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[PEOPLE_TABLE][1]["FirstName"] = "Eco"  # uppercase form ECO is a course prefix
    rows["All Courses"][0]["CourseNumber"] = "ECO 525"
    rows["All Courses"].append({"SSN": TOK_B, "CourseNumber": "Eco Osterhaud", "Grade": "A"})
    monkeypatch.setattr(accessdb, "open_source", lambda p: FakeSource(_schema(), rows))
    assert accessdb.main([]) == 0
    courses = [r["CourseNumber"] for r in _table(env["out"], "All Courses")]
    assert "ECO 525" in courses
    assert not any("Osterhaud" in str(c) or "Eco " in str(c) for c in courses)
