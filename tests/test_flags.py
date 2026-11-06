"""CLI flags: date windows, full dry run, and redaction/withholding switches."""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import stat
from email.message import EmailMessage
from pathlib import Path

import fitz
import pytest

from inkwell import cli
from inkwell.handlers import Ctx, process

from .conftest import SSN, text_pdf

Y1990, Y2000, Y2010 = 631152000, 946684800, 1262304000
TRANSCRIPT = ["OFFICIAL TRANSCRIPT", "Cumulative GPA 3.20", "Credits attempted 32"]


def _tree(root: Path) -> dict[str, Path]:
    stu = root / "Cohort 1990-91" / "Fernhollow, Quintavius"
    stu.mkdir(parents=True)
    files = {
        "old": stu / "old note.txt",
        "mid": stu / "transcript.pdf",
        "new": stu / "new note.txt",
    }
    files["old"].write_text(f"Quintavius Fernhollow SSN {SSN}")
    files["mid"].write_bytes(text_pdf(TRANSCRIPT))
    files["new"].write_text(f"Second note, SSN {SSN}")
    for key, t in (("old", Y1990), ("mid", Y2000), ("new", Y2010)):
        os.utime(files[key], (t, t))
    return files


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {"in": tmp_path / "in", "out": tmp_path / "out", "key": tmp_path / "secret" / "key.csv"}
    paths["in"].mkdir()
    paths["key"].parent.mkdir()
    monkeypatch.setenv("INKWELL_INPUT_DIR", str(paths["in"]))
    monkeypatch.setenv("INKWELL_OUTPUT_DIR", str(paths["out"]))
    monkeypatch.setenv("INKWELL_KEY_FILE", str(paths["key"]))
    monkeypatch.setenv("INKWELL_WORKERS", "1")
    monkeypatch.setenv("INKWELL_DPI", "150")
    return paths


def _outputs(out: Path) -> list[Path]:
    return sorted(p for p in out.rglob("D-*") if p.is_file())


def _report(out: Path) -> list[dict[str, str]]:
    with (out / "_inkwell_report.csv").open() as fh:
        return list(csv.DictReader(fh))


# --- date windows -------------------------------------------------------------


def test_parse_when() -> None:
    now = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.UTC)
    assert cli.parse_when("2019-09-01", now) == dt.datetime(2019, 9, 1).astimezone()
    assert cli.parse_when("2019-09-01T08:30", now) == dt.datetime(2019, 9, 1, 8, 30).astimezone()
    assert cli.parse_when("30d", now) == now - dt.timedelta(days=30)
    assert cli.parse_when("12h", now) == now - dt.timedelta(hours=12)
    for bad in ("yesterday", "2019-13-01", "-3d", ""):
        with pytest.raises(ValueError):
            cli.parse_when(bad, now)


def test_window_selection() -> None:
    w = cli.Window(
        modified_after=dt.datetime.fromtimestamp(Y1990 + 1, dt.UTC),
        modified_before=dt.datetime.fromtimestamp(Y2010, dt.UTC),
    )

    def job(mtime: int, birth: int | None = None) -> cli.Job:
        return cli.Job("s", "r", "p", mtime * 10**9, 0, birth_ns=None if birth is None else birth * 10**9)

    assert not w.matches(job(Y1990))
    assert w.matches(job(Y2000))
    assert not w.matches(job(Y2010)), "'before' is exclusive"
    wc = cli.Window(created_after=dt.datetime.fromtimestamp(Y2000, dt.UTC))
    assert wc.matches(job(Y1990, birth=Y2000)), "'after' is inclusive"
    assert not wc.matches(job(Y2010, birth=Y1990))
    with pytest.raises(cli.ConfigError, match="creation time"):
        wc.matches(job(Y2000, birth=None))


@pytest.mark.slow
def test_modified_window_end_to_end(env: dict[str, Path]) -> None:
    _tree(env["in"])
    assert cli.main(["--modified-after", "1995-01-01", "--modified-before", "2005-01-01"]) == 0
    outs = _outputs(env["out"])
    # only the year-2000 file (the transcript) is selected; it is withheld whole
    assert len(outs) == 1 and outs[0].name.endswith(".WITHHELD.txt"), outs
    import json

    summary = json.loads((env["out"] / "_inkwell_summary.json").read_text())
    assert summary["excluded_by_window"] == 2


def test_created_window_fails_fast_without_birthtime(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(env["in"])
    monkeypatch.setattr(cli, "_birthtime_ns", lambda _st: None)
    assert cli.main(["--created-after", "2000-01-01", "--plan-only"]) == 1


def test_bad_window_arguments(env: dict[str, Path]) -> None:
    _tree(env["in"])
    assert cli.main(["--modified-after", "nonsense", "--plan-only"]) == 1
    assert cli.main(["--modified-after", "2010-01-01", "--modified-before", "2000-01-01", "--plan-only"]) == 1


# --- dry run ------------------------------------------------------------------------


@pytest.mark.slow
def test_dry_run_reports_without_writing(env: dict[str, Path]) -> None:
    files = _tree(env["in"])
    before = {p: p.stat().st_mtime_ns for p in files.values()}
    assert cli.main(["--dry-run"]) == 0
    assert not env["out"].exists(), "dry run must not create the output"
    assert not env["key"].exists() and not (env["key"].parent / "registry.csv").exists()
    reports = sorted(env["key"].parent.glob("dryrun-*.csv"))
    assert len(reports) == 1
    assert stat.S_IMODE(reports[0].stat().st_mode) == 0o600
    with reports[0].open() as fh:
        rows = {Path(r["original_path"]).name: r for r in csv.DictReader(fh)}
    assert rows["old note.txt"]["would_status"] == "redacted"
    assert rows["transcript.pdf"]["would_status"] == "withheld"
    assert rows["transcript.pdf"]["category"] == "transcript_or_grades"
    assert '"KNOWN_STUDENT_NAME"' in rows["old note.txt"]["entities"]
    assert rows["old note.txt"]["pseudonym_is_new"] == "yes"
    assert SSN not in reports[0].read_text(), "reports never contain values"
    assert {p: p.stat().st_mtime_ns for p in files.values()} == before


def test_dry_run_report_inside_output_refused(env: dict[str, Path]) -> None:
    _tree(env["in"])
    assert cli.main(["--dry-run", "--report", str(env["out"] / "r.csv")]) == 1
    assert cli.main(["--dry-run", "--report", str(env["in"] / "r.csv")]) == 1


def test_plan_only_writes_nothing(env: dict[str, Path]) -> None:
    _tree(env["in"])
    assert cli.main(["--plan-only"]) == 0
    assert not env["out"].exists() and not env["key"].exists()
    assert not list(env["key"].parent.iterdir())


# --- redaction / withholding switches ----------------------------------------------


def test_both_disabled_refused(env: dict[str, Path]) -> None:
    _tree(env["in"])
    assert cli.main(["--no-redact", "--no-withhold"]) == 1
    assert not env["out"].exists()


@pytest.mark.slow
def test_no_withhold_redacts_protected_records_and_warns(env: dict[str, Path]) -> None:
    _tree(env["in"])
    assert cli.main(["--no-withhold"]) == 0
    rows = {r["kind"] + r["status"]: r for r in _report(env["out"])}
    assert "pdfwithheld" not in rows and "pdfredacted" in rows
    assert "flagged:transcript_or_grades" in rows["pdfredacted"]["notes"]
    assert not list(env["out"].rglob("*.WITHHELD.txt"))
    warning = (env["out"] / "_inkwell_WARNING.txt").read_text()
    assert "WITHHOLDING DISABLED" in warning
    for p in _outputs(env["out"]):
        assert SSN.encode() not in p.read_bytes()


@pytest.mark.slow
def test_no_redact_copies_unchanged_but_withholds_and_warns(env: dict[str, Path]) -> None:
    files = _tree(env["in"])
    assert cli.main(["--no-redact"]) == 0
    outs = _outputs(env["out"])
    txts = [p for p in outs if p.suffix == ".txt" and not p.name.endswith(".WITHHELD.txt")]
    assert sorted(p.read_bytes() for p in txts) == sorted(files[k].read_bytes() for k in ("old", "new")), (
        "non-withheld documents are copied byte-for-byte"
    )
    assert len(list(env["out"].rglob("*.WITHHELD.txt"))) == 1
    warning = (env["out"] / "_inkwell_WARNING.txt").read_text()
    assert "REDACTION DISABLED" in warning and "personally identifiable" in warning
    statuses = {r["status"] for r in _report(env["out"])}
    assert statuses == {"copied", "withheld"}, statuses
    assert all(p.stat().st_mtime in (Y1990, Y2010) for p in txts)


@pytest.mark.slow
def test_mode_change_requires_full_force(env: dict[str, Path]) -> None:
    _tree(env["in"])
    assert cli.main([]) == 0
    assert not (env["out"] / "_inkwell_WARNING.txt").exists()
    assert cli.main(["--no-withhold"]) == 1, "silently mixing modes in one output is refused"
    assert cli.main(["--no-withhold", "--force", "--limit", "1"]) == 1
    assert cli.main(["--no-withhold", "--force"]) == 0
    assert (env["out"] / "_inkwell_WARNING.txt").exists()
    assert cli.main(["--force"]) == 0
    assert not (env["out"] / "_inkwell_WARNING.txt").exists(), "warning removed with full mode"


# --- handler level --------------------------------------------------------------------


def _multi_page_pdf(pages: list[list[str]]) -> bytes:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page(width=612, height=792)
        for i, line in enumerate(lines):
            page.insert_text(fitz.Point(72, 72 + 20 * i), line, fontsize=12)
    return doc.tobytes()


def test_no_redact_pdf_keeps_original_pages_and_withholds_flagged(ctx: Ctx) -> None:
    import dataclasses

    nctx = dataclasses.replace(ctx, redact=False)
    pdf = _multi_page_pdf([[f"Welcome. SSN: {SSN}"], TRANSCRIPT])
    out = process("p.pdf", pdf, nctx)
    assert out.status == "copied" and out.data, "unredacted output is never labelled redacted"
    with fitz.open(stream=out.data, filetype="pdf") as d:
        assert d.page_count == 2
        assert SSN in d[0].get_text() and not d[0].get_images(), "original page, not rasterized"
        assert "WITHHELD" in d[1].get_text() and "3.20" not in d[1].get_text()


def test_no_redact_email_withholds_only_flagged_attachment(ctx: Ctx) -> None:
    import dataclasses
    from email import policy
    from email.parser import BytesParser

    nctx = dataclasses.replace(ctx, redact=False)
    m = EmailMessage()
    m["From"] = "Zebulon Pryce <zp@example.edu>"
    m["Subject"] = "documents"
    m.set_content(f"See attached. SSN {SSN}")
    m.add_attachment(text_pdf(TRANSCRIPT), maintype="application", subtype="pdf", filename="t.pdf")
    out = process("m.eml", m.as_bytes(), nctx)
    assert out.status == "copied" and out.data
    msg = BytesParser(policy=policy.default).parsebytes(out.data)
    assert "Zebulon Pryce" in str(msg["From"]), "headers untouched without redaction"
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None and SSN in body.get_content()
    att = next(msg.iter_attachments())
    assert b"WITHHELD" in att.get_payload(decode=True).upper()


def test_no_withhold_flags_instead_of_withholding(ctx: Ctx) -> None:
    import dataclasses

    wctx = dataclasses.replace(ctx, withhold=False)
    out = process("t.pdf", text_pdf(TRANSCRIPT), wctx)
    assert out.status == "redacted" and out.data
    assert out.category == "transcript_or_grades"
    assert any(n.startswith("flagged:transcript_or_grades") for n in out.notes)
    assert io.BytesIO(out.data).getvalue().startswith(b"%PDF")
