"""Permitted names (#2): listed people's full names stay visible; record subjects always win."""

from __future__ import annotations

import csv
import io
import json
import os
import stat
from collections.abc import Iterator
from email.message import EmailMessage
from pathlib import Path

import fitz
import pytest
from docx import Document
from openpyxl import Workbook

from inkwell import cli
from inkwell.config import ConfigError, load_config
from inkwell.engine import Findings, Scrubber
from inkwell.handlers import Ctx, process
from inkwell.leakcheck import extract_text
from inkwell.permitted import PermittedNames, load_permitted

from .conftest import SSN, STUDENT, png_bytes, text_image, text_pdf

HEADER = "name,aliases,emails,phones\n"
ADA = "Ada Lovelace"


def _write(path: Path, body: str) -> Path:
    path.write_text(HEADER + body)
    return path


@pytest.fixture
def permitted_file(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "permitted.csv",
        'Ada Lovelace,"Augusta Ada King;A. Lovelace",ada@example.org,(609) 555-0100\nCharles Babbage,,,\n',
    )


@pytest.fixture
def permit(scrubber: Scrubber, permitted_file: Path) -> Iterator[Scrubber]:
    scrubber.set_permitted(PermittedNames(load_permitted(permitted_file)))
    yield scrubber
    scrubber.set_permitted(PermittedNames([]))


def _red(sc: Scrubber, text: str) -> tuple[str, Findings]:
    f = Findings()
    return sc.redact_text(text, f), f


# --- loading and configuration -------------------------------------------------------


def test_load_valid(permitted_file: Path) -> None:
    people = load_permitted(permitted_file)
    assert [p.name for p in people] == ["Ada Lovelace", "Charles Babbage"]
    assert people[0].aliases == ("Augusta Ada King", "A. Lovelace")
    assert people[0].emails == ("ada@example.org",) and people[0].phones == ("(609) 555-0100",)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("Lovelace,,,\n", "full name"),
        (",,,\n", "full name"),
        ("Ada Lovelace,,not-an-email,\n", "email"),
    ],
)
def test_load_rejects_bad_rows(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_permitted(_write(tmp_path / "p.csv", body))


def test_load_rejects_bad_header(tmp_path: Path) -> None:
    (tmp_path / "p.csv").write_text("who,else\nAda Lovelace,x\n")
    with pytest.raises(ConfigError, match="header"):
        load_permitted(tmp_path / "p.csv")


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    (tmp_path / "in").mkdir(exist_ok=True)
    env = {
        "INKWELL_INPUT_DIR": str(tmp_path / "in"),
        "INKWELL_OUTPUT_DIR": str(tmp_path / "out"),
        "INKWELL_KEY_FILE": str(tmp_path / "key.csv"),
    }
    env.update(over)
    return env


def test_config(tmp_path: Path, permitted_file: Path) -> None:
    cfg = load_config(_env(tmp_path, INKWELL_PERMITTED_NAMES_FILE=str(permitted_file)))
    assert cfg.permitted_names_file == permitted_file.resolve() and cfg.permitted_contacts is False
    assert load_config(_env(tmp_path, INKWELL_PERMITTED_CONTACTS="true")).permitted_contacts is True
    with pytest.raises(ConfigError, match="INKWELL_PERMITTED_NAMES_FILE"):
        load_config(_env(tmp_path, INKWELL_PERMITTED_NAMES_FILE=str(tmp_path / "missing.csv")))
    (tmp_path / "out").mkdir()
    inside = _write(tmp_path / "out" / "p.csv", "Ada Lovelace,,,\n")
    with pytest.raises(ConfigError, match="outside INKWELL_OUTPUT_DIR"):
        load_config(_env(tmp_path, INKWELL_PERMITTED_NAMES_FILE=str(inside)))
    with pytest.raises(ConfigError, match="INKWELL_PERMITTED_CONTACTS"):
        load_config(_env(tmp_path, INKWELL_PERMITTED_CONTACTS="maybe"))


# --- matching --------------------------------------------------------------------------


def test_permitted_full_names_stay(permit: Scrubber) -> None:
    out, f = _red(permit, "Signed by Ada Lovelace. Copy to Lovelace, Ada and Dr. Ada Lovelace's office.")
    assert out.count("Ada Lovelace") == 2 and "Lovelace, Ada" in out, out
    assert f.counts["PERMITTED_NAME"] >= 3


def test_aliases_stay_partial_names_do_not(permit: Scrubber) -> None:
    out, _ = _red(permit, "Augusta Ada King approved. Later, Lovelace met Zebulon Pryce.")
    assert "Augusta Ada King" in out
    assert "Zebulon" not in out and "Pryce" not in out, "unlisted people are still redacted"
    assert "Lovelace" not in out, "a lone surname of a permitted person is still redacted"


def test_record_subject_always_wins(scrubber: Scrubber, tmp_path: Path) -> None:
    from .conftest import SID

    people = load_permitted(_write(tmp_path / "p.csv", f"{STUDENT},,,\n{ADA},,,\n"))
    kept, conflicts = PermittedNames.without_students(people, scrubber.known)
    assert [p.name for p in conflicts] == [STUDENT] and [p.name for p in kept] == [ADA]
    try:
        scrubber.set_permitted(PermittedNames(people))  # even if a conflict slipped through
        out, _ = _red(scrubber, f"{STUDENT} met {ADA}.")
        assert STUDENT not in out and f"<{SID}>" in out and ADA in out
    finally:
        scrubber.set_permitted(PermittedNames([]))


def test_contacts_redacted_unless_enabled(scrubber: Scrubber, permitted_file: Path) -> None:
    text = f"{ADA} (ada@example.org, (609) 555-0100); other: bob@example.org"
    people = load_permitted(permitted_file)
    try:
        scrubber.set_permitted(PermittedNames(people))
        out, _ = _red(scrubber, text)
        assert ADA in out and "ada@example.org" not in out and "555-0100" not in out
        scrubber.set_permitted(PermittedNames(people, allow_contacts=True))
        out, f = _red(scrubber, text)
        assert "ada@example.org" in out and "(609) 555-0100" in out
        assert "bob@example.org" not in out, "only listed contacts are kept"
        assert f.counts["PERMITTED_CONTACT"] == 2
    finally:
        scrubber.set_permitted(PermittedNames([]))


# --- every format -------------------------------------------------------------------------


def test_email_headers_and_body(permit: Scrubber, ctx: Ctx) -> None:
    m = EmailMessage()
    m["From"] = f"{ADA} <ada@example.org>"
    m["To"] = f"{STUDENT} <qf@example.edu>"
    m["Subject"] = f"Note from {ADA}"
    m.set_content(f"Dear Quintavius,\n{ADA} confirms. SSN {SSN}.\n")
    out = process("m.eml", m.as_bytes(), ctx)
    assert out.status == "redacted" and out.data
    text = extract_text("m.eml", out.data)
    assert f"From: {ADA} <redacted@invalid>" in text, "name kept, address redacted by default"
    assert f"Subject: Note from {ADA}" in text and f"{ADA} confirms" in text
    assert "Fernhollow" not in text and SSN not in text and "ada@example.org" not in text


def test_docx_xlsx_html(permit: Scrubber, ctx: Ctx) -> None:
    d = Document()
    d.add_paragraph(f"Reviewed by {ADA} for {STUDENT}")
    buf = io.BytesIO()
    d.save(buf)
    out = process("f.docx", buf.getvalue(), ctx)
    assert out.data
    t = extract_text("f.docx", out.data)
    assert ADA in t and "Fernhollow" not in t

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["Reviewer", "Notes"])
    ws.append([ADA, f"discussed {STUDENT}"])
    xb = io.BytesIO()
    wb.save(xb)
    out = process("s.xlsx", xb.getvalue(), ctx)
    assert out.data
    t = extract_text("s.xlsx", out.data)
    assert ADA in t and "Fernhollow" not in t

    html = f"<html><body><p>{ADA} wrote about {STUDENT}</p></body></html>"
    out = process("p.html", html.encode(), ctx)
    assert out.data
    t = out.data.decode()
    assert ADA in t and "Fernhollow" not in t


def test_pdf_and_image(permit: Scrubber, ctx: Ctx, make_ocr: object) -> None:
    out = process("a.pdf", text_pdf([f"Approved by {ADA}", f"Student: {STUDENT}", f"SSN: {SSN}"]), ctx)
    assert out.status == "redacted" and out.data
    with fitz.open(stream=out.data, filetype="pdf") as d:
        layer = d[0].get_text()
    assert "Lovelace" in layer and "Fernhollow" not in layer and SSN not in layer
    assert out.findings.counts["PERMITTED_NAME"] >= 1

    img = text_image([f"Approved by {ADA}", f"SSN {SSN}"])
    out = process("p.png", png_bytes(img), ctx)
    assert out.data
    ocr = make_ocr(out.data)  # type: ignore[operator]
    assert "Lovelace" in ocr and "6789" not in ocr


# --- CLI: conflicts and suggestions ------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {"in": tmp_path / "in", "out": tmp_path / "out", "key": tmp_path / "secret" / "key.csv"}
    paths["in"].mkdir()
    paths["key"].parent.mkdir()
    for k, var in (("in", "INPUT_DIR"), ("out", "OUTPUT_DIR"), ("key", "KEY_FILE")):
        monkeypatch.setenv(f"INKWELL_{var}", str(paths[k]))
    monkeypatch.setenv("INKWELL_WORKERS", "1")
    monkeypatch.setenv("INKWELL_DPI", "150")
    return paths


def _subjects(root: Path) -> None:
    for i, (folder, parent) in enumerate(
        [("Roe, Jane", "Hortensia Roe"), ("Poe, Jasper", "Barnaby Poe"), ("Doe, Kip", "Philippa Doe")]
    ):
        d = root / "Cohort 1990-91" / folder
        d.mkdir(parents=True)
        m = EmailMessage()
        m["From"] = f"{ADA} <ada@example.org>"
        m["To"] = f"{folder} <s{i}@example.edu>"
        m["Subject"] = "Welcome"
        m.set_content(f"Welcome. {ADA} and Charles Babbage will meet you. Your parent {parent} called.")
        (d / "welcome.eml").write_bytes(m.as_bytes())


@pytest.mark.slow
def test_cli_conflicts_reported_privately(env: dict[str, Path]) -> None:
    _subjects(env["in"])
    pfile = _write(env["key"].parent / "permitted.csv", f"{ADA},,,\nJane Roe,,,\n")
    os.environ["INKWELL_PERMITTED_NAMES_FILE"] = str(pfile)
    try:
        assert cli.main([]) == 0
    finally:
        del os.environ["INKWELL_PERMITTED_NAMES_FILE"]
    summary = json.loads((env["out"] / "_inkwell_summary.json").read_text())
    assert summary["permitted_conflicts"] == 1
    conflicts = env["key"].parent / "permitted-conflicts.csv"
    assert stat.S_IMODE(conflicts.stat().st_mode) == 0o600 and "Jane Roe" in conflicts.read_text()
    for p in env["out"].rglob("*"):
        if p.is_file():
            blob = p.read_bytes()
            assert b"Jane Roe" not in blob and b"Roe, Jane" not in blob, p.name
    report = (env["out"] / "_inkwell_report.csv").read_text()
    assert "PERMITTED_NAME" in report and ADA not in report


@pytest.mark.slow
def test_suggest_permitted(env: dict[str, Path]) -> None:
    _subjects(env["in"])
    assert cli.main(["--suggest-permitted", "--min-students", "3"]) == 0
    assert not env["out"].exists(), "suggestions never touch the output"
    assert not env["key"].exists(), "nor the key"
    files = sorted(env["key"].parent.glob("permitted-suggestions-*.csv"))
    assert len(files) == 1 and stat.S_IMODE(files[0].stat().st_mode) == 0o600
    with files[0].open() as fh:
        rows = {r["name"]: r for r in csv.DictReader(fh)}
    assert set(rows) == {ADA, "Charles Babbage"}, (
        rows.keys()
    )  # family names appear once; registry people excluded
    assert rows[ADA]["evidence_students"] == "3" and rows[ADA]["emails"] == "ada@example.org"
    # the suggestions file is directly usable as the permitted-names file
    assert [p.name for p in load_permitted(files[0])] == sorted(rows)


def test_suggest_job_skips_unreadable_file_without_failing(tmp_path: Path, scrubber: Scrubber) -> None:
    # A file that blows up during extraction is counted as skipped, not fatal to the pass.
    cli._W["scrubber"] = scrubber
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 truncated")
    owners, names, ok = cli._suggest_job(cli.Job(str(bad), "bad.pdf", "p", 0, 0, ("S-X",)))
    assert (owners, names) == (("S-X",), []) and ok in (True, False)
    missing = cli._suggest_job(cli.Job(str(tmp_path / "gone.pdf"), "gone.pdf", "p", 0, 0))
    assert missing[2] is False, "unreadable files are reported, not silently ignored"


# --- suggestion precision: shapes that NER reported as PERSON but are not a person's full name ---
NOT_NAMES = [
    "First Name",
    "Middle Name (or other name)",
    "Last (Family) Name",
    "Date Of Birth",
    "Place of Birth",
    "Maiden Name",
    "Mailing Address",
    "Email Address",
    "Cell Phone",
    "P.O. Box",
    "Suffix (e.g., Jr.)",
    "Student Number",
    "summa cum laude",
    "Magna Cum Laude",
    "Ph.D.",
    "M.A.",
    'dir="ltr',
    "Webpage:&nbsp",
    'class="MsoNormal">Hi Ada',
    "Ren√© Descartes",  # mojibake of "René"
    "conÔ¬Årm",
    "J. A.",
    "S.J.",
    "Ada B.",
    "C. Babbage",
    "Dear Ada",
    "Dear Ada Lovelace",  # salutation stripped -> fine, see KEEP; first name alone is not
    "Cai et al.",
    "wil l",
    "of student",
    "Elm Street",
    "Maple Drive",
    "Grand Hotels",
    "Office Of The Registrar",
    "Soon As Possible",
    "Deep Learning",
    "Quartz PDFContext",
    "smtpclient.apple",
    "PT_AGSTARTPAGE_NUI",
    "Ada Sent",
    "C. Babbage A. Lovelace",
    "Ada Lovelace A. B.",
    "Please List Two Contacts.",
]
NOT_NAMES.remove("Dear Ada Lovelace")
KEEP = {
    "Ada Lovelace": "Ada Lovelace",
    "Dear Ada Lovelace": "Ada Lovelace",
    "Dr. Ada Lovelace": "Ada Lovelace",
    "ADA K. LOVELACE": "ADA K. LOVELACE",
    "Charles B. Babbage": "Charles B. Babbage",
    "A. Ada Lovelace": "A. Ada Lovelace",
    "Ada Lovelace's": "Ada Lovelace",
    "Ada Lovelace-King": "Ada Lovelace-King",
    "Ada O'Lovelace": "Ada O'Lovelace",
    "Ada McLovelace": "Ada McLovelace",
    "Ada van Lovelace": "Ada van Lovelace",
    "Ada Lovelace,": "Ada Lovelace",
    "René Descartes": "René Descartes",
    "Ada Lovelace &lt": "Ada Lovelace",  # NER span cut at an escaped "<"
    "Ada Lovelace Dean": "Ada Lovelace",  # trailing title of a signature
    "Ada Lovelace Ph.D.": "Ada Lovelace",
    "Dean Ada Lovelace": "Ada Lovelace",  # a title before a full name is dropped...
    "Dean J. Lovelace": "Dean J. Lovelace",  # ...but kept when it must be the first name
    "Ada d'Lovelace": "Ada d'Lovelace",
    "Ada d' Lovelace": "Ada d' Lovelace",
}


@pytest.mark.parametrize("text", NOT_NAMES)
def test_candidate_rejects_non_names(text: str) -> None:
    from inkwell.permitted import candidate_name

    assert candidate_name(text) is None


@pytest.mark.parametrize(("text", "clean"), KEEP.items())
def test_candidate_keeps_full_names(text: str, clean: str) -> None:
    from inkwell.permitted import candidate_name

    assert candidate_name(text) == clean


def test_candidate_extra_noise_words() -> None:
    from inkwell.permitted import candidate_name

    assert candidate_name("Widget Lovelace") == "Widget Lovelace"
    assert candidate_name("Widget Lovelace", noise=frozenset({"widget"})) is None


def test_variant_key_groups_initials_but_name_key_does_not() -> None:
    from inkwell.permitted import name_key, variant_key

    assert (
        variant_key("Charles B. Babbage") == variant_key("Charles Babbage") == variant_key("Babbage, Charles")
    )
    assert name_key("A. Lovelace") != name_key("Lovelace")  # aliases with initials stay specific
