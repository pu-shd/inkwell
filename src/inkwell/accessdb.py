"""inkwell-db: scrub Microsoft Access databases of student records into SQLite and CSV.

Every table and column has a declared treatment in POLICY. A column without one stops the run
before anything is written, so a schema change can never leak a new field.

Record subjects get the IDs they already have in the registry (the people in the scrubbed
documents). People found only in a database are added to the registry, flagged for review.

Exit codes: 0 ok, 1 configuration error, 2 a database was leak-blocked (not written),
3 no databases found.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .config import Config, ConfigError, load_config
from .ids import is_valid, new_id
from .mdb import SUFFIXES, Column, MdbError, MdbFile, Row, Source, require_tools
from .permitted import PermittedNames, PermittedPerson, load_permitted, load_places, write_people
from .permitted import name_key as permitted_key
from .registry import Registry, name_key

if TYPE_CHECKING:
    from .engine import Scrubber
    from .recognizers import KnownNameRecognizer

log = logging.getLogger("inkwell.db")

# --- column policy ---------------------------------------------------------------------------

KEY = "key"  # per-person record key: becomes record_id + student_id
DROP = "drop"  # direct or indirect identifier: not in the output
WITHHOLD = "withhold"  # protected record (grades, scores, funding, demographics): not in the output
KEEP = "keep"  # copied; record subjects' names and contact/ID patterns in it are replaced
# Codes, numbers and dates. A code-shaped value ("ECO 525", "MA", "1994-09-01") is copied even if a
# word in it is also someone's name (uppercase ECO for "Eco"); any other value is treated as KEEP.
CODE = "code"
FACULTY = "faculty"  # staff names, copied; a record subject's full name in it is replaced
TEXT = "text"  # free text: full scrub (names, places, contacts, IDs), as for documents

SUBJECT_COLUMN = "SSN"  # the record key in this database; already masked, but still linkable
PEOPLE_TABLE = "Current Info Permanent Address Personal Info"
FIRST, MIDDLE, LAST = "FirstName", "MiddleName", "LastName"


def _cols(action: str, *names: str) -> dict[str, str]:
    return dict.fromkeys(names, action)


POLICY: dict[str, dict[str, str]] = {
    "Academic Information": {
        SUBJECT_COLUMN: KEY,
        **_cols(
            CODE, "Degree", "YearOfEntry", "YearOfStudy", "ProgramOfStudy", "DateOfChange", "ReadmitAction"
        ),
        **_cols(KEEP, "Award-1", "Award-2", "Award-3", "Award-4", "Award-5"),
        **_cols(FACULTY, "Advisor(s)-1", "Advisor(s)-2", "AdvisorChange-1", "AdvisorChange-2"),
        "Comments": TEXT,
    },
    "AI/ESL Information": {
        SUBJECT_COLUMN: KEY,
        **_cols(CODE, "AITraining", "AITrainingDate"),
        **_cols(
            WITHHOLD,
            "ESLEvaluation",
            "ESLEvaluationDate",
            "ESLTraining",
            "ESLTrainingDate",
            "ESLTrainingResult",
        ),
        "CourseComments": TEXT,
    },
    "All AI Courses": {
        SUBJECT_COLUMN: KEY,
        **_cols(CODE, "AICourseNumber", "AIYear", "AISemester"),
        "Instructor": FACULTY,
    },
    "All Courses": {
        SUBJECT_COLUMN: KEY,
        **_cols(CODE, "CourseNumber", "Semesters", "YearTaken"),
        "Grade": WITHHOLD,
    },
    "CIV 509/510 Research Seminar": {
        SUBJECT_COLUMN: KEY,
        **_cols(CODE, "CIV509SeminarDate", "CIV510SeminarDate"),
        **_cols(TEXT, "CIV509SeminarTopic", "CIV510SeminarTopic"),
    },
    "Course Comments": {SUBJECT_COLUMN: KEY, "CourseComments": TEXT},
    PEOPLE_TABLE: {
        SUBJECT_COLUMN: KEY,
        **_cols(
            DROP,
            LAST,
            FIRST,
            MIDDLE,
            "StreetorAptNumber",
            "2nd Address",
            "City",
            "State",
            "ZipCode",
            "PhoneNumber",
            "E-mail",
            "OfficeRoomNumber",
            "OfficePhoneNumber",
            "HomeAddress",
            "HomeCity",
            "HomeState",
            "HomeZipCode",
            "HomeCountry",
            "HomePhoneNumber",
            "DateOfBirth",
            "PhoneGivenOut",
        ),
        **_cols(
            WITHHOLD,
            "Gender",
            "MaritalStatus",
            "MinorityStatus",
            "Citizenship",
            "VisaStatus",
            "PermanentResident",
            "SupportS",
            "SupportI",
            "SupportR",
            "SupportF",
        ),
        **_cols(CODE, "UgInstitution", "Status", "ApptBegin", "ApptEnd"),
    },
    "Employment and Forwarding Address": {
        SUBJECT_COLUMN: KEY,
        **_cols(
            KEEP,
            "Employer/Institution",
            "Employer",
            "Position",
            "EmployerCity",
            "EmployerCountry",
            "Second Employer",
            "Second Employer Position",
            "Current Employer",
            "Current Position",
        ),
        "EmployerState": CODE,
        **_cols(
            DROP,
            "EmployerStreet",
            "EmployerZipCode",
            "EmployerPhoneNumber",
            "ForwardingAddress",
            "ForwardingStreet",
            "ForwardingCity",
            "ForwardingState",
            "ForwardingZipCode",
            "ForwardingCountry",
            "ForwardingPhoneNumber",
        ),
    },
    "General Exams RAC Meetings MSE MEngr PhD FPO": {
        SUBJECT_COLUMN: KEY,
        **_cols(CODE, "GE-YearTaken", "GE-MonthTaken", "MSE-TimeToMasters", "MEngr-Program"),
        **_cols(CODE, "PhD-TimeToDegree", "PhD-TimeToFPO", "FPO-Completed"),
        **_cols(CODE, *(f"RAC-{k}-{i}" for i in range(1, 5) for k in ("Date", "ReportSubmitted"))),
        **_cols(WITHHOLD, "GE-EEWR", "GE-MMS", "GE-SOR"),
        # Thesis titles are unique and public: next to a pseudonym they name the person.
        **_cols(DROP, "MSE-ThesisTitle", "PhD-ThesisTitle"),
        **_cols(FACULTY, "FPO-Committee-1", "FPO-Committee-2", "FPO-Committee-3", "FPO-Committee-4"),
    },
    "Program of Study Choices": {"ProgramOfStudy": CODE},
    "Support Information (currently not in use)": {SUBJECT_COLUMN: KEY, "Fill In Later": WITHHOLD},
    "Admissions": {
        SUBJECT_COLUMN: KEY,
        **_cols(WITHHOLD, "RankOrderNumber", "GREVerbal", "GREQuantitative", "GREAnalytical"),
        **_cols(
            WITHHOLD,
            "TOEFLScore",
            "TSE",
            "TWE",
            "UGGPAMajor",
            "UGGPAOverall",
            "GradGPAMajor",
            "GradGPAOverall",
        ),
        **_cols(
            KEEP, "UGSchool", "UGFieldOfStudy", "UGDegree", "GradSchool", "GradFieldOfStudy", "GradDegree"
        ),
        **_cols(CODE, "UGGraduationDate", "GradGraduationDate"),
    },
}
OUTPUT_ACTIONS = (KEY, KEEP, CODE, FACULTY, TEXT)
NAME_LABEL = "<KNOWN_STUDENT_NAME>"
# Identifier patterns replaced in KEEP and CODE cells (no NER: names there are handled by the registry).
PATTERN_ENTITIES = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "SSN_LIKE",
    "STUDENT_ID",
    "STREET_ADDRESS",
    "GOV_ID",
    "US_ITIN",
    "US_PASSPORT",
    "US_BANK_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
)
_CODE_CHARS = re.compile(r"[A-Z0-9\s.,:;/()+&#-]+")


def is_code(value: str) -> bool:
    """Digits, punctuation and uppercase words of at most four letters only."""
    v = value.strip()
    return bool(_CODE_CHARS.fullmatch(v)) and all(len(w) <= 4 for w in re.findall(r"[A-Z]+", v))


def check_policy(schema: dict[str, list[Column]]) -> None:
    """Refuse a database with a table or column that has no declared treatment."""
    unknown = [
        f"{t}.{c.name}" for t, cols in schema.items() for c in cols if POLICY.get(t, {}).get(c.name) is None
    ]
    if unknown:
        raise ConfigError(
            "columns without a treatment in the inkwell-db policy (add them to POLICY): " + ", ".join(unknown)
        )
    if PEOPLE_TABLE not in schema:
        raise ConfigError(f"the database has no {PEOPLE_TABLE!r} table, so record subjects cannot be named")


def _sql_type(mdb_type: str) -> str:
    t = mdb_type.lower()
    if t in {"integer", "int", "long", "byte", "boolean", "bool", "smallint", "bigint"}:
        return "INTEGER"
    if t in {"real", "double", "float", "numeric", "decimal", "money", "single"}:
        return "REAL"
    return "TEXT"


def _value(text: str | None, sql_type: str) -> object:
    if text is None:
        return None
    if sql_type == "INTEGER" and re.fullmatch(r"-?\d+", text):
        return int(text)
    if sql_type == "REAL" and re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text):
        return float(text)
    return text


# --- key folder: db-key.csv ------------------------------------------------------------------

DBKEY_NAME = "db-key.csv"
DBKEY_HEADER = ("record_token", "record_id", "student_id", "match", "databases", "db_name")


@dataclass
class KeyRow:
    record_token: str  # the source's record key (the masked "SSN")
    record_id: str
    student_id: str  # "" if the record has no named person
    match: str  # exact | partial | new | ambiguous | orphan, or as edited by hand
    databases: set[str] = field(default_factory=set)
    db_name: str = ""


class DbKey:
    def __init__(self, path: Path, registry: Registry) -> None:
        self.path = path
        self.rows: dict[str, KeyRow] = {}
        if not path.exists():
            return
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if tuple(reader.fieldnames or ()) != DBKEY_HEADER:
                raise ConfigError(f"{path.name} has unexpected format")
            for line, r in enumerate(reader, start=2):
                rid, sid = r["record_id"], r["student_id"]
                if not (rid.startswith("R-") and is_valid(rid)):
                    raise ConfigError(f"{path.name} line {line}: invalid record_id")
                if sid and not (sid.startswith("S-") and is_valid(sid)):
                    raise ConfigError(f"{path.name} line {line}: invalid student_id")
                if sid and sid not in registry.students:
                    raise ConfigError(f"{path.name} line {line}: student_id is not in registry.csv")
                if not r["record_token"] or r["record_token"] in self.rows:
                    raise ConfigError(f"{path.name} line {line}: missing or repeated record_token")
                dbs = {d for d in r["databases"].split(";") if d}
                self.rows[r["record_token"]] = KeyRow(
                    r["record_token"], rid, sid, r["match"], dbs, r["db_name"]
                )

    @property
    def record_ids(self) -> set[str]:
        return {r.record_id for r in self.rows.values()}

    def save(self) -> None:
        _write_private_csv(
            self.path,
            DBKEY_HEADER,
            (
                (r.record_token, r.record_id, r.student_id, r.match, ";".join(sorted(r.databases)), r.db_name)
                for r in sorted(self.rows.values(), key=lambda x: x.record_id)
            ),
        )


def _write_private_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".dbkey-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- reading ---------------------------------------------------------------------------------


@dataclass
class Database:
    path: Path
    stem: str  # output name
    source: Source
    schema: dict[str, list[Column]] = field(default_factory=dict)
    _rows: dict[str, list[Row]] = field(default_factory=dict)

    def rows(self, table: str) -> list[Row]:
        if table not in self._rows:
            self._rows[table] = self.source.rows(table)
        return self._rows[table]


def find_databases(input_dir: Path) -> list[Path]:
    return sorted(p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in SUFFIXES)


def output_stem(path: Path) -> str:
    return re.sub(r"\s+", " ", path.stem).strip() or "database"


def _safe_name(table: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", table).strip() or "table"


@dataclass(frozen=True)
class Person:
    first: str
    middle: str
    last: str

    @property
    def short(self) -> str:
        return " ".join(x for x in (self.first, self.last) if x)

    @property
    def full(self) -> str:
        return " ".join(x for x in (self.first, self.middle, self.last) if x)


def collect_people(dbs: Sequence[Database]) -> dict[str, Person]:
    people: dict[str, Person] = {}
    for db in dbs:
        for r in db.rows(PEOPLE_TABLE):
            token = r.get(SUBJECT_COLUMN)
            if not token or token in people:
                continue
            p = Person(*(re.sub(r"\s+", " ", r.get(c) or "").strip() for c in (FIRST, MIDDLE, LAST)))
            if p.full:
                people[token] = p
    return people


def collect_tokens(dbs: Sequence[Database]) -> dict[str, set[str]]:
    """Every record key in any table, with the databases it appears in."""
    tokens: dict[str, set[str]] = {}
    for db in dbs:
        for table, cols in db.schema.items():
            if any(POLICY[table][c.name] == KEY for c in cols):
                for r in db.rows(table):
                    if r.get(SUBJECT_COLUMN):
                        tokens.setdefault(r[SUBJECT_COLUMN] or "", set()).add(db.stem)
    return tokens


# --- matching people to the registry ---------------------------------------------------------

NEW_NOTE = "added by inkwell-db; no document folder"


class Matcher:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.keys = {sid: {k for k in s.keys() if len(k) >= 2} for sid, s in registry.students.items()}

    def match(self, p: Person) -> tuple[str, list[str]]:
        keys = {k for k in (name_key(p.short), name_key(p.full)) if len(k) >= 2}
        if not keys:
            return "none", []
        exact = sorted(sid for sid, ks in self.keys.items() if keys & ks)
        if exact:
            return ("exact" if len(exact) == 1 else "ambiguous"), exact
        partial = sorted(
            sid for sid, ks in self.keys.items() if any(k <= q or q <= k for k in ks for q in keys)
        )
        if partial:
            return ("partial" if len(partial) == 1 else "ambiguous"), partial
        return "none", []

    def add(self, p: Person, note: str) -> str:
        s = self.registry.add_person(p.short or p.full, {p.full}, note)
        self.keys[s.student_id] = {k for k in s.keys() if len(k) >= 2}
        return s.student_id


def assign(
    registry: Registry, dbkey: DbKey, people: dict[str, Person], tokens: dict[str, set[str]]
) -> Counter[str]:
    """Give every record key a record ID and (if it names a person) a registry ID. Rows already
    in db-key.csv keep theirs, including hand edits."""
    matcher = Matcher(registry)
    taken = dbkey.record_ids
    stats: Counter[str] = Counter()
    for token in sorted(tokens):
        prev = dbkey.rows.get(token)
        if prev is not None:
            prev.databases |= tokens[token]
            stats["kept"] += 1
            continue
        rid = new_id("record", taken)
        taken.add(rid)
        p = people.get(token)
        if p is None:
            dbkey.rows[token] = KeyRow(token, rid, "", "orphan", set(tokens[token]))
            stats["orphan"] += 1
            continue
        kind, cands = matcher.match(p)
        if kind in ("exact", "partial"):
            sid = cands[0]
        else:
            note = NEW_NOTE + (f"; name also matches {', '.join(cands)}" if cands else "")
            sid = matcher.add(p, note)
            kind = "ambiguous" if cands else "new"
        dbkey.rows[token] = KeyRow(token, rid, sid, kind, set(tokens[token]), p.full)
        stats[kind] += 1
    return stats


def student_table(registry: Registry, dbkey: DbKey, people: dict[str, Person]) -> list[tuple[str, list[str]]]:
    """Registry names plus each person's names as the database spells them."""
    names: dict[str, set[str]] = {sid: set(n) for sid, n in registry.name_table()}
    for token, row in dbkey.rows.items():
        p = people.get(token)
        if row.student_id and p is not None:
            names.setdefault(row.student_id, set()).update(n for n in (p.short, p.full) if n)
    return [(sid, sorted(n)) for sid, n in sorted(names.items())]


# --- faculty ---------------------------------------------------------------------------------

_FACULTY_SPLIT = re.compile(r"\s*(?:/|&|;|\+|\n|\band\b)\s*")
_FACULTY_NAME = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ'’. ,-]+")


def faculty_people(values: Iterable[str]) -> list[PermittedPerson]:
    """Full names (two or more words) in faculty columns, in the permitted-names form."""
    names: dict[frozenset[str], str] = {}
    for v in values:
        for part in _FACULTY_SPLIT.split(v):
            part = re.sub(r"\s+", " ", part).strip(" .,")
            if _FACULTY_NAME.fullmatch(part or "-") and len(permitted_key(part)) >= 2:
                names.setdefault(permitted_key(part), part)
    return [PermittedPerson(n) for n in sorted(names.values())]


# --- scrubbing -------------------------------------------------------------------------------


def _pick(spans: list[tuple[int, int, bool]]) -> list[tuple[int, int, bool]]:
    """Non-overlapping matches, longest (and full names) first."""
    chosen: list[tuple[int, int, bool]] = []
    for s, e, full in sorted(spans, key=lambda x: (-(x[1] - x[0]), not x[2], x[0])):
        if all(e <= cs or s >= ce for cs, ce, _ in chosen):
            chosen.append((s, e, full))
    return sorted(chosen)


def replace_names(known: KnownNameRecognizer, text: str, sid: str, *, full_only: bool) -> tuple[str, int]:
    """Replace record subjects' names: a full name becomes the person's ID (as in documents), a
    single name word becomes <KNOWN_STUDENT_NAME>. Returns the text and the number replaced."""
    spans = _pick([m for m in known.matches(text) if m[2] or not full_only])
    out = text
    for s, e, full in reversed(spans):
        label = NAME_LABEL
        if full:
            rid = known.resolve(text[s:e], (sid,) if sid else ())
            label = f"<{rid}>" if rid else NAME_LABEL
        out = out[:s] + label + out[e:]
    return out, len(spans)


@dataclass
class TableOut:
    name: str
    columns: list[tuple[str, str, str]]  # (output name, SQL type, action)
    pk: str | None
    rows: list[list[object]] = field(default_factory=list)


@dataclass
class DbResult:
    stem: str
    tables: list[TableOut] = field(default_factory=list)
    replaced: Counter[str] = field(default_factory=Counter)  # "table.column" -> names replaced
    faculty_shared_word: Counter[str] = field(default_factory=Counter)
    entities: Counter[str] = field(default_factory=Counter)
    leaks: list[dict[str, object]] = field(default_factory=list)
    status: str = "pending"


class DbScrubber:
    def __init__(self, scrubber: Scrubber, known: KnownNameRecognizer, dbkey: DbKey) -> None:
        self.scrubber = scrubber
        self.known = known
        self.ids = {t: (r.record_id, r.student_id) for t, r in dbkey.rows.items()}
        self._text_cache: dict[tuple[str, str], tuple[str, Counter[str]]] = {}
        self._pattern_cache: dict[str, tuple[str, Counter[str]]] = {}

    def _patterns(self, value: str) -> tuple[str, Counter[str]]:
        hit = self._pattern_cache.get(value)
        if hit is None:
            from .engine import clean_text, resolve_overlaps

            text = clean_text(value)
            ents = [e for e in PATTERN_ENTITIES if e in self.scrubber.entities]
            found = self.scrubber.analyzer.analyze(
                text=text, language="en", entities=ents, score_threshold=self.scrubber.threshold
            )
            results = resolve_overlaps(list(found)) if ents else []
            for r in sorted(results, key=lambda x: x.start, reverse=True):
                text = text[: r.start] + f"<{r.entity_type}>" + text[r.end :]
            hit = self._pattern_cache[value] = (text, Counter(r.entity_type for r in results))
        return hit

    def _text(self, value: str, sid: str) -> tuple[str, Counter[str]]:
        hit = self._text_cache.get((value, sid))
        if hit is None:
            from .engine import Findings

            f: Findings = Findings()
            out = self.scrubber.redact_text(value, f, context_ids=(sid,) if sid else ())
            hit = self._text_cache[(value, sid)] = (out, f.counts)
        return hit

    def table(self, db: Database, table: str, res: DbResult) -> TableOut:
        cols = db.schema[table]
        policy = POLICY[table]
        out_cols: list[tuple[str, str, str]] = []
        pk: str | None = None
        for c in cols:
            action = policy[c.name]
            if action == KEY:
                out_cols += [("record_id", "TEXT", KEY), ("student_id", "TEXT", KEY)]
                pk = "record_id" if c.pk else pk
            elif action in OUTPUT_ACTIONS:
                out_cols.append((c.name, _sql_type(c.type), action))
        t = TableOut(table, out_cols, pk)
        for r in db.rows(table):
            token = r.get(SUBJECT_COLUMN) or ""
            rid, sid = self.ids.get(token, ("", ""))
            row: list[object] = []
            for c in cols:
                action, v = policy[c.name], r.get(c.name)
                if action == KEY:
                    row += [rid or None, sid or None]
                    continue
                if action not in OUTPUT_ACTIONS:
                    continue
                sql = _sql_type(c.type)
                if v is None or sql != "TEXT":
                    row.append(_value(v, sql))
                    continue
                where = f"{table}.{c.name}"
                if action == TEXT:
                    v, counts = self._text(v, sid)
                    res.entities.update(counts)
                elif action == FACULTY:
                    v, n = replace_names(self.known, v, sid, full_only=True)
                    res.replaced[where] += n
                    if self.known.find_any(v):
                        res.faculty_shared_word[where] += 1
                else:  # KEEP, CODE
                    if not (action == CODE and is_code(v)):
                        v, n = replace_names(self.known, v, sid, full_only=False)
                        res.replaced[where] += n
                    v, counts = self._patterns(v)
                    res.entities.update(counts)
                row.append(v)
            t.rows.append(row)
        return t


# --- leak check ------------------------------------------------------------------------------

_EMAIL_RX = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[A-Za-z]{2,}")


class LeakCheck:
    """Independent re-check of the written output. Any hit blocks the whole database."""

    def __init__(self, known: KnownNameRecognizer, tokens: Iterable[str], dropped: Dropped) -> None:
        from .leakcheck import LABELED_SSN_RX, SSN_RX

        self.ssn = (SSN_RX, LABELED_SSN_RX)
        self.known = known
        toks = sorted({t for t in tokens if len(t) >= 4}, key=len, reverse=True)
        self.tokens = re.compile("|".join(map(re.escape, toks))) if toks else None
        self.dropped = dropped

    def cell(self, text: str, action: str) -> list[str]:
        issues: list[str] = []
        if any(rx.search(text) for rx in self.ssn):
            issues.append("ssn_pattern")
        if self.tokens is not None and self.tokens.search(text):
            issues.append("record_token")
        if _EMAIL_RX.search(text):
            issues.append("email")
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 10 and any(p in digits for p in self.dropped.phones):
            issues.append("dropped_phone")
        low = text.lower()
        if any(s in low for s in self.dropped.streets):
            issues.append("dropped_address")
        if action == FACULTY:
            names = self.known.find_full_name(text)
        else:
            names = not (action == CODE and is_code(text)) and self.known.find_any(text)
        if names:
            issues.append("known_student_name")
        return issues


@dataclass
class Dropped:
    """Direct identifiers removed from the output, looked for again in what is written."""

    phones: set[str] = field(default_factory=set)
    streets: set[str] = field(default_factory=set)


def collect_dropped(dbs: Sequence[Database]) -> Dropped:
    d = Dropped()
    for db in dbs:
        for table, cols in db.schema.items():
            for c in cols:
                if POLICY[table][c.name] != DROP:
                    continue
                low = c.name.lower()
                for r in db.rows(table):
                    v = r.get(c.name)
                    if not v:
                        continue
                    if "phone" in low and len(re.sub(r"\D", "", v)) >= 10:
                        d.phones.add(re.sub(r"\D", "", v))
                    elif ("street" in low or "address" in low) and re.search(r"\d", v):
                        norm = re.sub(r"\s+", " ", v).strip().lower()
                        if len(norm) >= 8:
                            d.streets.add(norm)
    return d


# --- writing ---------------------------------------------------------------------------------


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def write_outputs(res: DbResult, dest: Path) -> None:
    """<stem>.sqlite and <stem>/<table>.csv inside dest."""
    con = sqlite3.connect(dest / f"{res.stem}.sqlite")
    try:
        for t in res.tables:
            defs = ", ".join(
                f"{_q(n)} {typ}" + (" PRIMARY KEY" if n == t.pk else "") for n, typ, _ in t.columns
            )
            con.execute(f"CREATE TABLE {_q(t.name)} ({defs})")
            marks = ", ".join("?" for _ in t.columns)
            con.executemany(f"INSERT INTO {_q(t.name)} VALUES ({marks})", t.rows)  # noqa: S608
        con.commit()
    finally:
        con.close()
    csv_dir = dest / res.stem
    csv_dir.mkdir()
    for t in res.tables:
        with (csv_dir / f"{_safe_name(t.name)}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([n for n, _, _ in t.columns])
            w.writerows([["" if v is None else v for v in row] for row in t.rows])


def check_outputs(res: DbResult, dest: Path, check: LeakCheck) -> list[dict[str, object]]:
    """Re-read both written forms and report leaks by table, column, row and reason (no values)."""
    leaks: list[dict[str, object]] = []
    actions = {t.name: {n: a for n, _, a in t.columns} for t in res.tables}

    def scan(table: str, header: Sequence[str], rows: Iterable[Sequence[object]], form: str) -> None:
        for i, row in enumerate(rows, start=1):
            for name, v in zip(header, row, strict=True):
                if v is None or actions[table][name] == KEY:
                    continue
                for reason in check.cell(str(v), actions[table][name]):
                    leaks.append({"form": form, "table": table, "column": name, "row": i, "reason": reason})

    con = sqlite3.connect(f"file:{dest / f'{res.stem}.sqlite'}?mode=ro", uri=True)
    try:
        for t in res.tables:
            cur = con.execute(f"SELECT * FROM {_q(t.name)}")  # noqa: S608
            scan(t.name, [d[0] for d in cur.description], cur.fetchall(), "sqlite")
    finally:
        con.close()
    for t in res.tables:
        with (dest / res.stem / f"{_safe_name(t.name)}.csv").open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            scan(t.name, header, ([v or None for v in row] for row in reader), "csv")
    return leaks


def schema_doc(db: Database) -> dict[str, object]:
    """Source columns and what happened to each; enough to rebuild an Access file later."""
    return {
        t: [
            {
                "name": c.name,
                "type": c.type,
                "primary_key": c.pk,
                "action": POLICY[t][c.name],
                "output": ["record_id", "student_id"]
                if POLICY[t][c.name] == KEY
                else ([c.name] if POLICY[t][c.name] in OUTPUT_ACTIONS else []),
            }
            for c in cols
        ]
        for t, cols in db.schema.items()
    }


def publish(staging: Path, out_dir: Path, names: Sequence[str]) -> None:
    for n in names:
        target = out_dir / n
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        os.replace(staging / n, target)


# --- run -------------------------------------------------------------------------------------


def db_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    missing = [
        n for n in ("INKWELL_DB_INPUT_DIR", "INKWELL_DB_OUTPUT_DIR", "INKWELL_KEY_FILE") if not env.get(n)
    ]
    if missing:
        raise ConfigError(f"missing required environment variable(s): {', '.join(missing)}")
    env["INKWELL_INPUT_DIR"] = env["INKWELL_DB_INPUT_DIR"]
    env["INKWELL_OUTPUT_DIR"] = env["INKWELL_DB_OUTPUT_DIR"]
    try:
        return load_config(env)
    except ConfigError as exc:
        msg = str(exc).replace("INKWELL_INPUT_DIR", "INKWELL_DB_INPUT_DIR")
        raise ConfigError(msg.replace("INKWELL_OUTPUT_DIR", "INKWELL_DB_OUTPUT_DIR")) from None


def open_source(path: Path) -> Source:
    return MdbFile(path)


def run(cfg: Config, dbs: Sequence[Database], *, dry_run: bool = False) -> int:
    from .engine import Scrubber
    from .recognizers import KnownNameRecognizer, configure_name_noise

    configure_name_noise(cfg.extra_name_noise)
    stems = [db.stem for db in dbs]
    if len(set(stems)) != len(stems):
        raise ConfigError("two databases have the same name apart from the extension")
    for db in dbs:
        db.schema = db.source.columns()
        check_policy(db.schema)

    key_dir = cfg.key_file.parent
    registry = Registry(key_dir / "registry.csv", cfg.group_dir_regex)
    dbkey = DbKey(key_dir / DBKEY_NAME, registry)
    people = collect_people(dbs)
    tokens = collect_tokens(dbs)
    match_stats = assign(registry, dbkey, people, tokens)
    log.info("records: %s", dict(sorted(match_stats.items())))

    students = student_table(registry, dbkey, people)
    known = KnownNameRecognizer(students)
    faculty_values = [
        v
        for db in dbs
        for t, cols in db.schema.items()
        for c in cols
        if POLICY[t][c.name] == FACULTY
        for r in db.rows(t)
        if (v := r.get(c.name))
    ]
    faculty = faculty_people(faculty_values)
    listed = load_permitted(cfg.permitted_names_file) if cfg.permitted_names_file else []
    kept, conflicts = PermittedNames.without_students([*listed, *faculty], known)
    faculty_kept, _ = PermittedNames.without_students(faculty, known)
    places = load_places(cfg.permitted_places_file) if cfg.permitted_places_file else []
    places_kept, _ = PermittedNames.places_without_students(places, known)
    log.info(
        "permitted names: %d listed + %d from faculty columns, %d conflicts",
        len(listed),
        len(faculty),
        len(conflicts),
    )

    scrubber = Scrubber(
        spacy_model=cfg.spacy_model,
        entities=cfg.entities,
        score_threshold=cfg.score_threshold,
        students=students,
        extra_id_labels=cfg.extra_id_labels,
    )
    scrubber.set_permitted(PermittedNames(kept, cfg.permitted_contacts, places_kept))
    dbs_scrubber = DbScrubber(scrubber, known, dbkey)
    check = LeakCheck(known, tokens, collect_dropped(dbs))

    if dry_run:
        report = key_dir / f"db-dryrun-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
        _write_private_csv(
            report,
            DBKEY_HEADER,
            (
                (r.record_token, r.record_id, r.student_id, r.match, ";".join(sorted(r.databases)), r.db_name)
                for r in sorted(dbkey.rows.values(), key=lambda x: (x.match, x.db_name))
            ),
        )
        log.info("dry run: record matches written to %s (key folder); nothing else written", report.name)
    else:
        # Key first: an output must never carry IDs the key folder cannot resolve.
        registry.save()
        dbkey.save()
        write_people(key_dir / "db-faculty.csv", ((p.name, "", "", "") for p in faculty_kept))
        write_people(key_dir / "db-faculty-conflicts.csv", ((p.name, "", "", "") for p in conflicts))
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

    results: list[DbResult] = []
    for db in dbs:
        res = DbResult(db.stem)
        res.tables = [dbs_scrubber.table(db, t, res) for t in db.schema]
        # A dry run stages in the private key folder; nothing reaches the output folder.
        staging = Path(tempfile.mkdtemp(prefix=".inkwell-db-", dir=key_dir if dry_run else cfg.output_dir))
        try:
            write_outputs(res, staging)
            res.leaks = check_outputs(res, staging, check)
            (staging / f"{db.stem}.schema.json").write_text(json.dumps(schema_doc(db), indent=2) + "\n")
            if res.leaks:
                res.status = "leak_blocked"
                log.error("%s: %d leak(s) found; NOT written (see summary)", db.stem, len(res.leaks))
            elif dry_run:
                res.status = "ok_dry_run"
            else:
                publish(staging, cfg.output_dir, [f"{db.stem}.sqlite", db.stem, f"{db.stem}.schema.json"])
                res.status = "written"
            log.info("%s: %s (%d tables)", db.stem, res.status, len(res.tables))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        results.append(res)

    summary = {
        "inkwell_version": __version__,
        "finished": dt.datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "records": dict(sorted(match_stats.items())),
        "databases": {r.stem: _summary(r) for r in results},
    }
    if dry_run:
        print(json.dumps(summary, indent=2))
    else:
        (cfg.output_dir / "_inkwell_db_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 2 if any(r.status == "leak_blocked" for r in results) else 0


def _summary(r: DbResult) -> dict[str, object]:
    return {
        "status": r.status,
        "tables": {t.name: len(t.rows) for t in r.tables},
        "names_replaced": dict(sorted((k, v) for k, v in r.replaced.items() if v)),
        "faculty_cells_sharing_a_subject_name_word": dict(sorted(r.faculty_shared_word.items())),
        "text_entities": dict(sorted(r.entities.items())),
        "leaks": r.leaks[:200],
        "leak_count": len(r.leaks),
    }


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="inkwell-db",
        description="Scrub Access databases (INKWELL_DB_INPUT_DIR) into SQLite and CSV "
        "(INKWELL_DB_OUTPUT_DIR), using the registry in the key folder.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything but write: no output, no registry or key changes; record matches go to "
        "db-dryrun-<time>.csv in the key folder",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    from .cli import LOCK_NAME, _acquire_lock, _release_lock

    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    lock: Path | None = None
    try:
        cfg = db_config()
        require_tools()
        paths = find_databases(cfg.input_dir)
        if not paths:
            log.error("no .mdb or .accdb files in INKWELL_DB_INPUT_DIR")
            return 3
        lock = cfg.key_file.parent / LOCK_NAME
        _acquire_lock(lock)
        dbs = [Database(p, output_stem(p), open_source(p)) for p in paths]
        return run(cfg, dbs, dry_run=args.dry_run)
    except (ConfigError, MdbError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        if lock is not None:
            _release_lock(lock)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
