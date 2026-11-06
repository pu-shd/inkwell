"""The four identifier guarantees: random IDs, person-level registry, content fingerprints,
and in-text pseudonyms."""

from __future__ import annotations

import io
import re
import stat
from pathlib import Path, PurePosixPath

import fitz
from PIL import Image

from inkwell.engine import Findings, Scrubber
from inkwell.handlers import Ctx, process
from inkwell.ids import is_valid, new_id
from inkwell.pseudonym import PseudonymMap
from inkwell.registry import Registry

from .conftest import OTHER_SID, SID, STUDENT, png_bytes, text_image

CLASS = "Cohort 1990-91"

# --- 1. random IDs ------------------------------------------------------------


def test_ids_valid_random_and_typo_detecting() -> None:
    ids = [new_id("student") for _ in range(2000)]
    assert len(set(ids)) == 2000
    assert all(re.fullmatch(r"S-[0-9A-HJKMNP-TV-Z]{9}", i) and is_valid(i) for i in ids)
    assert is_valid(SID) and is_valid(OTHER_SID)
    sid = ids[0]
    for pos in range(2, len(sid)):
        for ch in "0123456789ABCDEFGHJKMNPQRSTVWXY":  # Z omitted: 0<->Z is the documented blind spot
            if ch != sid[pos] and {ch, sid[pos]} != {"0", "Z"}:
                assert not is_valid(sid[:pos] + ch + sid[pos + 1 :]), (sid, pos, ch)
    assert not is_valid("S-123")
    assert is_valid(new_id("doc")) and new_id("doc").startswith("D-")


def test_ids_do_not_follow_alphabetical_order(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "registry.csv")
    names = [
        f"{first} {last}"
        for first, last in zip(
            "Aaron Bea Cy Di Ed Flo Gus Hal Ivy Jo Kip Lou".split(),
            "Able Bell Cole Dunn Eve Fenn Gale Hope Isle Jett King Lark".split(),
            strict=True,
        )
    ]
    ids = [reg.add_folder(CLASS, n).student_id for n in sorted(names)]
    # Sorted names must not yield sorted IDs (chance of a false failure: 1 in 12! = 1 in 479M).
    assert ids != sorted(ids)
    assert len(set(ids)) == len(ids)


# --- 2. person-level registry --------------------------------------------------


def test_registry_merges_folders_and_preserves_edits(tmp_path: Path) -> None:
    path = tmp_path / "registry.csv"
    reg = Registry(path)
    a = reg.add_folder("Cohort 1990-91", "Roe, Jane")
    b = reg.add_folder("Cohort 1992-93", "Jane Roe")  # re-enrolled
    c = reg.add_folder("Cohort 1990-91", "Jasper Poe")
    assert a.student_id == b.student_id != c.student_id
    assert a.class_years == {"1990-91", "1992-93"} and len(a.folders) == 2
    assert "more than one group folder" in a.needs_review
    assert reg.match_loose("JaneRoe") == [a.student_id]  # camel-case zip name
    assert reg.match_loose("Jane Roe Records  xtra info") == [a.student_id]
    assert reg.match_loose("Roe") == []  # a lone surname is not enough
    a.institution_id = "960000000"
    a.aliases.add("Janie Roe")
    reg.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    reg2 = Registry(path)
    s = reg2.students[a.student_id]
    assert s.institution_id == "960000000" and "Janie Roe" in s.aliases
    assert reg2.add_folder("Cohort 1990-91", "Roe, Jane").student_id == a.student_id


def test_pseudonym_layout_uses_registry(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "registry.csv")
    pm = PseudonymMap(tmp_path / "key.csv", reg)
    f1 = pm.file_path(PurePosixPath("Cohort 1990-91/Roe, Jane/Transcript Jane.pdf"), "h1")
    f2 = pm.file_path(PurePosixPath("Cohort 1992-93/Jane Roe/sub/letter.pdf"), "h2")
    f3 = pm.file_path(PurePosixPath("JaneRoe.zip"), "h3")
    sid = next(iter(reg.students))
    assert re.fullmatch(rf"Cohort 1990-91/{sid}/D-\w{{11}}", str(f1)), f1
    assert re.fullmatch(rf"Cohort 1992-93/{sid}/F-\w{{7}}/D-\w{{11}}", str(f2)), f2
    assert re.fullmatch(r"D-\w{11}", str(f3))
    for rel in (
        "Cohort 1990-91/Roe, Jane/Transcript Jane.pdf",
        "Cohort 1992-93/Jane Roe/sub/letter.pdf",
        "JaneRoe.zip",
    ):
        assert pm.owners(rel) == {sid}, rel
    assert "Jane" not in str(f1) + str(f2) + str(f3)


# --- 3. content fingerprints -----------------------------------------------------


def test_moved_file_keeps_doc_id_but_copies_do_not(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "registry.csv")
    key = tmp_path / "key.csv"
    pm = PseudonymMap(key, reg)
    a = pm.file_path(PurePosixPath(f"{CLASS}/Roe, Jane/old name.pdf"), "abc")
    pm.save()
    reg.save()

    pm2 = PseudonymMap(key, Registry(tmp_path / "registry.csv"))
    moved = pm2.file_path(
        PurePosixPath(f"{CLASS}/Roe, Jane/new name.pdf"), "abc", still_exists=lambda _p: False
    )
    assert moved == a, "renamed original must keep its document ID"
    copy = pm2.file_path(PurePosixPath(f"{CLASS}/Roe, Jane/copy.pdf"), "abc", still_exists=lambda _p: True)
    assert copy.name != a.name, "a second live copy must not collide with the first"


def test_key_header_rejects_old_format(tmp_path: Path) -> None:
    key = tmp_path / "key.csv"
    key.write_text("original_path,pseudonym_path\n")
    import pytest

    with pytest.raises(ValueError, match="older inkwell"):
        PseudonymMap(key, Registry(tmp_path / "registry.csv"))


# --- 4. in-text pseudonyms ---------------------------------------------------------


def test_text_names_become_student_ids(scrubber: Scrubber) -> None:
    f = Findings()
    out = scrubber.redact_text(f"Letter for {STUDENT}. Mr. Quintavius Fernhollow agreed.", f)
    assert out.count(f"<{SID}>") == 2, out
    assert f.mentions == {SID}


def test_ambiguous_surname_resolved_by_document_owner(scrubber: Scrubber) -> None:
    # Two registry people share the surname Fernhollow.
    f = Findings()
    assert "<KNOWN_STUDENT_NAME>" in scrubber.redact_text("Ms. Fernhollow called.", f)
    assert not f.mentions
    f2 = Findings()
    out = scrubber.redact_text("Ms. Fernhollow called.", f2, context_ids=[OTHER_SID])
    assert f"<{OTHER_SID}>" in out and f2.mentions == {OTHER_SID}


def test_other_student_mentioned_in_document_is_linked(ctx: Ctx) -> None:
    out = process("n.txt", b"Ottoline Fernhollow will co-advise Quintavius Fernhollow.", ctx)
    assert out.data is not None
    text = out.data.decode()
    assert f"<{OTHER_SID}>" in text and f"<{SID}>" in text
    assert out.findings.mentions == {SID, OTHER_SID}


def test_scanned_name_gets_id_label_and_text_layer(ctx: Ctx) -> None:
    img = text_image([f"Student: {STUDENT}", "Department of Mathematics"])
    pdf = fitz.open()
    page = pdf.new_page(width=612, height=396)
    page.insert_image(page.rect, stream=png_bytes(img))
    out = process("scan.pdf", pdf.tobytes(), ctx)
    assert out.status == "redacted" and out.data
    assert SID in out.findings.mentions
    with fitz.open(stream=out.data, filetype="pdf") as d:
        assert SID in d[0].get_text(), "ID must be searchable in the text layer"
    # the label is drawn: the image has white pixels inside a black region
    out_img = process("p.png", png_bytes(img), ctx)
    assert out_img.data
    px = Image.open(io.BytesIO(out_img.data)).convert("L")
    assert min(px.getdata()) == 0 and SID in out_img.findings.mentions
