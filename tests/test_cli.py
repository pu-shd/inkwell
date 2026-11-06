"""End-to-end: real process pool, real files, mtime preservation, no PII in output tree."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from inkwell import cli

from .conftest import SSN, text_pdf

OLD = 631152000  # 1990-01-01


def _tree(root: Path) -> None:
    stu = root / "Cohort 1990-91" / "Fernhollow, Quintavius"
    stu.mkdir(parents=True)
    (stu / "Quintavius notes.txt").write_text(f"Quintavius Fernhollow SSN {SSN}")
    (stu / "offer letter.pdf").write_bytes(text_pdf(["Dear Quintavius Fernhollow,", "Welcome."]))
    (stu / "app.js").write_text("x")
    for p in (stu / "Quintavius notes.txt", stu / "offer letter.pdf", stu):
        os.utime(p, (OLD, OLD))


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


@pytest.mark.slow
def test_end_to_end(env: dict[str, Path]) -> None:
    import hashlib
    import re
    import stat

    _tree(env["in"])
    assert cli.main([]) == 0
    out = env["out"]
    files = sorted(p for p in out.rglob("*") if p.is_file() and not p.name.startswith("_inkwell"))
    names = [str(p.relative_to(out)) for p in files]
    assert len(names) == 2, names
    assert all(re.fullmatch(r"Cohort 1990-91/S-\w{9}/D-\w{11}\.(txt|pdf)", n) for n in names), names

    for p in files:
        assert "Fernhollow" not in str(p) and "Quintavius" not in str(p)
        blob = p.read_bytes()
        assert b"Fernhollow" not in blob and SSN.encode() not in blob, p.name
        assert p.stat().st_mtime == OLD, f"mtime not preserved on {p.name}"
    student_dir = files[0].parent
    assert student_dir.stat().st_mtime == OLD
    sid = student_dir.name

    txt = next(p for p in files if p.suffix == ".txt").read_text()
    assert f"<{sid}>" in txt, "the subject's own name becomes their registry ID"

    with (out / "_inkwell_report.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert {r["status"] for r in rows} == {"redacted", "dropped"}
    assert len(rows) == 3

    # key: owners, mentions and both hashes; registry: one row for the person
    key = env["key"]
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    with key.open() as fh:
        krows = {r["original_path"]: r for r in csv.DictReader(fh)}
    for p in files:
        rel = str(p.relative_to(out))
        row = next(r for r in krows.values() if r["pseudonym_path"] == rel.rsplit(".", 1)[0])
        src = env["in"] / row["original_path"]
        assert row["sha256_original"] == hashlib.sha256(src.read_bytes()).hexdigest()
        assert row["sha256_output"] == hashlib.sha256(p.read_bytes()).hexdigest()
        assert row["student_ids"] == sid
    notes_row = next(r for k, r in krows.items() if k.endswith("Quintavius notes.txt"))
    assert notes_row["mentions"] == sid
    registry = env["key"].parent / "registry.csv"
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    with registry.open() as fh:
        reg_rows = list(csv.DictReader(fh))
    assert [r["student_id"] for r in reg_rows] == [sid]
    assert reg_rows[0]["canonical_name"] == "Quintavius Fernhollow"

    # re-run is safe, skips done work, and keeps every identifier
    before = key.read_text()
    assert cli.main([]) == 0
    with (out / "_inkwell_report.csv").open() as fh:
        assert len(list(csv.DictReader(fh))) == 4  # only the dropped file is re-listed
    assert key.read_text() == before


def test_dry_run_writes_nothing(env: dict[str, Path]) -> None:
    _tree(env["in"])
    assert cli.main(["--dry-run"]) == 0
    assert not env["out"].exists()
    assert not env["key"].exists()


def test_empty_input_is_failure(env: dict[str, Path]) -> None:
    assert cli.main([]) == 3


def test_bad_config_exit_1(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKWELL_OUTPUT_DIR", str(env["in"] / "x"))
    assert cli.main([]) == 1


def test_write_replaces_stale_outputs_of_same_stem(tmp_path: Path) -> None:
    # A file withheld on one run and redacted on the next must not leave the old output behind.
    d = tmp_path / "STUDENT_0001"
    d.mkdir()
    (d / "DOC_000001.WITHHELD.txt").write_text("old")
    (d / "DOC_0000010.pdf").write_text("different stem, keep")
    job = cli.Job("src", "rel", "STUDENT_0001/DOC_000001", OLD * 10**9, OLD * 10**9)
    cli._write(d / "DOC_000001.pdf", b"%PDF-new", job)
    assert sorted(p.name for p in d.iterdir()) == ["DOC_000001.pdf", "DOC_0000010.pdf"]
    assert (d / "DOC_000001.pdf").stat().st_mtime == OLD


def test_remove_outputs_on_failure(tmp_path: Path) -> None:
    from inkwell.config import Config

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out", key_file=tmp_path / "k.csv")
    d = cfg.output_dir / "STUDENT_0001"
    d.mkdir(parents=True)
    (d / "DOC_000001.eml").write_text("from an earlier, now-failing run")
    cli._remove_outputs(cfg, cli.Job("s", "r", "STUDENT_0001/DOC_000001", 0, 0))
    assert list(d.iterdir()) == []


def test_audit_deletes_leaky_outputs_and_images(env: dict[str, Path]) -> None:
    # After a recognizer fix, outputs written by the older code are re-checked; failures (and
    # images, which have no text layer to check) are deleted so the next run redoes them.
    _tree(env["in"])
    assert cli.main([]) == 0
    out = env["out"]
    written = sorted(p for p in out.rglob("D-*") if p.is_file())
    assert len(written) == 2
    txt = next(p for p in written if p.suffix == ".txt")
    txt.write_text("left over: ___________Quintavius Fernhollow")  # simulate an old, leaky output
    img = txt.with_name("D-0000000000Z.png")
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
    assert cli.main(["--audit"]) == 0
    remaining = sorted(p.name for p in out.rglob("D-*") if p.is_file())
    assert txt.name not in remaining and img.name not in remaining
    assert len(remaining) == 1  # the clean PDF stays
    assert cli.main([]) == 0  # resume rebuilds the deleted text output
    assert txt.exists() and "Fernhollow" not in txt.read_text()


def test_audit_flags_containers_with_images() -> None:
    import io
    import zipfile
    from email.message import EmailMessage

    m = EmailMessage()
    m.set_content("hi")
    m.add_attachment(b"\x89PNG\r\n\x1a\n" + b"\0" * 20, maintype="image", subtype="png", filename="a.png")
    assert cli._contains_image("x.eml", m.as_bytes())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/media/image1.jpeg", b"\xff\xd8\xff" + b"\0" * 10)
    assert cli._contains_image("x.zip", buf.getvalue())
    plain = EmailMessage()
    plain.set_content("no images")
    assert not cli._contains_image("y.eml", plain.as_bytes())


def test_contains_image_terminates_on_header_like_bodies() -> None:
    # Regression: a body that itself looks like e-mail headers (a quoted message) was sniffed
    # as eml and recursed into forever.
    # First line is not a header, so the parser puts the whole file in the body: the "part"
    # payload equals the input and sniffs as eml again.
    body = b"Forwarded note\nFrom: c@x\nTo: d@x\nSubject: quoted\n\ntext\n"
    assert not cli._contains_image("q.eml", body)
