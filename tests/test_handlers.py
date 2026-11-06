from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable
from email.message import EmailMessage

import fitz
import pytest
from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image

from inkwell.handlers import Ctx, process
from inkwell.leakcheck import extract_text, find_leaks

from .conftest import SSN, STUDENT, png_bytes, scanned_pdf, text_image, text_pdf

DIGITS = re.compile(r"\d{3}\D?\d{2}\D?\d{4}")


def _assert_clean(text: str) -> None:
    assert SSN not in text
    assert "6789" not in text
    assert "Fernhollow" not in text and "FERNHOLLOW" not in text
    assert "Quintavius" not in text


# --- text / html ------------------------------------------------------------


def test_text(ctx: Ctx) -> None:
    out = process("notes.txt", f"Student {STUDENT}, SSN {SSN}, likes chess.".encode(), ctx)
    assert out.status == "redacted" and out.data is not None
    txt = out.data.decode()
    _assert_clean(txt)
    assert "likes chess" in txt


def test_html_text_attrs_scripts(ctx: Ctx) -> None:
    html = (
        "<html><head><title>Record of Quintavius Fernhollow</title>"
        "<script>var ssn='123-45-6789';</script></head><body>"
        "<table><tr><td>SSN:</td><td>123-45-6789</td></tr></table>"
        '<input value="Quintavius Fernhollow"><a href="mailto:qf@example.com">mail</a>'
        "<!-- note about Quintavius --></body></html>"
    )
    out = process("page.htm", html.encode(), ctx)
    assert out.status == "redacted" and out.data
    s = out.data.decode()
    _assert_clean(s)
    assert "<script" not in s and "qf@example.com" not in s


# --- email ------------------------------------------------------------------


def _eml() -> bytes:
    m = EmailMessage()
    m["From"] = "Zebulon Pryce <zp@example.edu>"
    m["To"] = f"{STUDENT} <qf@example.edu>"
    m["Subject"] = f"Housing for {STUDENT}"
    m["Message-ID"] = "<abc@example.edu>"
    m["Received"] = "from mail.example.edu (10.1.2.3)"
    m.set_content(f"Dear Quintavius,\nYour SSN {SSN} is on file.\nRegards, Zebulon")
    m.add_attachment(
        text_pdf([f"Name: {STUDENT}", f"SSN: {SSN}"]),
        maintype="application",
        subtype="pdf",
        filename=f"{STUDENT} housing form.pdf",
    )
    return m.as_bytes()


def test_eml(ctx: Ctx) -> None:
    out = process("message.eml", _eml(), ctx)
    assert out.status == "redacted" and out.data, out.notes
    full = extract_text("x.eml", out.data)
    _assert_clean(full)
    assert "Zebulon" not in full, "sender display name should seed ad-hoc redaction"
    assert "10.1.2.3" not in full and "Message-ID" not in full
    assert "housing form" not in out.data.decode(errors="replace")
    assert "DOC_000001_att01.pdf" in out.data.decode(errors="replace")
    assert any(k[1].endswith("_att01.pdf") for k in out.keys)


def test_eml_withheld_when_about_grades(ctx: Ctx) -> None:
    m = EmailMessage()
    m["From"] = "a@example.edu"
    m["Subject"] = "Your official transcript"
    m.set_content("Attached is your official transcript with cumulative GPA 3.9.")
    out = process("g.eml", m.as_bytes(), ctx)
    assert out.status == "withheld"
    assert out.category == "transcript_or_grades"
    assert out.data is not None and b"GPA" not in out.data and b"3.9" not in out.data


# --- docx / xlsx ------------------------------------------------------------


def test_docx_runs_tables_props(ctx: Ctx) -> None:
    d = Document()
    p = d.add_paragraph("Social Security Number: ")
    p.add_run("123-45")
    p.add_run("-6789")  # split across runs
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "Student name:"
    t.cell(0, 1).text = STUDENT
    d.sections[0].header.paragraphs[0].text = f"File of {STUDENT}"
    d.core_properties.author = STUDENT
    buf = io.BytesIO()
    d.save(buf)
    out = process("f.docx", buf.getvalue(), ctx)
    assert out.status == "redacted" and out.data
    _assert_clean(extract_text("f.docx", out.data))


def test_xlsx_pii_columns(ctx: Ctx) -> None:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Fernhollow"
    ws.append(["Student Name", "SSN", "Advisor notes"])
    ws.append([STUDENT, SSN, "doing fine"])
    ws.append(["Other Person", 987654321, "ok"])
    wb.properties.creator = STUDENT
    buf = io.BytesIO()
    wb.save(buf)
    out = process("s.xlsx", buf.getvalue(), ctx)
    assert out.status == "redacted" and out.data
    text = extract_text("s.xlsx", out.data)
    _assert_clean(text)
    assert "987654321" not in text
    assert "doing fine" in text
    assert load_workbook(io.BytesIO(out.data)).worksheets[0].title == "Sheet1"


# --- PDF / image ------------------------------------------------------------


def test_text_pdf_flattened(ctx: Ctx, make_ocr: Callable[[bytes], str]) -> None:
    pdf = text_pdf([f"Student Name: {STUDENT}", f"SSN: {SSN}", "Department of Operations Research"])
    out = process("a.pdf", pdf, ctx)
    assert out.status == "redacted" and out.data
    with fitz.open(stream=out.data, filetype="pdf") as d:
        assert not any(v for v in d.metadata.values() if v and "Fernhollow" in str(v))
        page = d[0]
        assert page.get_images(), "page must be flattened to an image"
        layer = page.get_text()
    _assert_clean(layer)
    assert "Operations" in layer, "invisible text layer keeps non-PII words searchable"
    _assert_clean(make_ocr(out.data))


@pytest.mark.slow
def test_scanned_pdf(ctx: Ctx, make_ocr: Callable[[bytes], str]) -> None:
    pdf = scanned_pdf([f"SSN: {SSN}", f"Name: {STUDENT}", "Department of Mathematics"])
    out = process("scan.pdf", pdf, ctx)
    assert out.status == "redacted" and out.data
    assert out.findings.counts, "OCR path must find entities"
    ocr = make_ocr(out.data)
    _assert_clean(ocr)
    assert "Mathematics" in ocr


def test_image(ctx: Ctx, make_ocr: Callable[[bytes], str]) -> None:
    img = text_image([f"Social Security: {SSN}", "Welcome reception at noon"])
    out = process("p.png", png_bytes(img), ctx)
    assert out.status == "redacted" and out.data
    assert out.data.startswith(b"\x89PNG")
    _assert_clean(make_ocr(out.data))
    red = Image.open(io.BytesIO(out.data)).convert("L")
    assert min(red.getdata()) == 0  # boxes drawn


def test_withheld_pdf(ctx: Ctx) -> None:
    pdf = text_pdf(["OFFICIAL TRANSCRIPT", "Cumulative GPA 3.20", "Credits attempted 32"])
    out = process("t.pdf", pdf, ctx)
    assert out.status == "withheld" and out.ext == ".WITHHELD.txt"
    assert out.data is not None and b"3.20" not in out.data


# --- zip / failure modes ----------------------------------------------------


def test_zip(ctx: Ctx) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{STUDENT}/notes.txt", f"SSN {SSN}")
        z.writestr("__MACOSX/._junk", b"x")
        z.writestr("lib.js", "var a=1")
    out = process("bundle.zip", buf.getvalue(), ctx)
    assert out.status == "redacted" and out.data
    with zipfile.ZipFile(io.BytesIO(out.data)) as z:
        names = z.namelist()
        assert len(names) == 1 and "Fernhollow" not in names[0]
        _assert_clean(z.read(names[0]).decode())
    assert any("m2:dropped" in n for n in out.notes)


def test_unsupported_emits_nothing(ctx: Ctx) -> None:
    out = process("old.doc", b"\xd0\xcf\x11\xe0" + b"\0" * 100, ctx)
    assert out.status == "unsupported" and out.data is None


def test_corrupt_fails_closed(ctx: Ctx) -> None:
    out = process("bad.pdf", b"%PDF-1.4 garbage " + SSN.encode(), ctx)
    assert out.status == "error" and out.data is None
    assert all(SSN not in n for n in out.notes)


def test_leakcheck_detects(scrubber: object) -> None:
    from inkwell.engine import Scrubber

    assert isinstance(scrubber, Scrubber)
    assert find_leaks("a.txt", f"oops {SSN}".encode(), scrubber.known) == ["ssn_pattern"]
    assert find_leaks("a.txt", b"Fernhollow was here", scrubber.known) == ["known_student_name"]
    assert find_leaks("a.txt", b"<PERSON> was here", scrubber.known) == []


def test_words_are_nfkc_normalized(ctx: Ctx) -> None:
    from inkwell.engine import Findings, Word, layer_text

    # The PDF text layer is Latin-1 only; analysis must see exactly what the layer will hold.
    assert layer_text("Deﬁnition") == "Definition"
    w = [Word("Fernhollow王", 0, 0, 10, 10, (1,))]
    assert ctx.scrubber.redact_words(w, Findings()) == [0]


def test_eml_content_id_rewritten(ctx: Ctx) -> None:
    m = EmailMessage()
    m["From"] = "a@example.edu"
    m["Subject"] = "photo"
    m.set_content("see photo")
    m.add_alternative('<html><body><img src="cid:Quintavius_Fernhollow.png@x"></body></html>', subtype="html")
    html_part = m.get_payload()[1]
    html_part.add_related(
        png_bytes(text_image(["hello"], size=(200, 80))), "image", "png", cid="<Quintavius_Fernhollow.png@x>"
    )
    out = process("c.eml", m.as_bytes(), ctx)
    assert out.status == "redacted" and out.data
    full = extract_text("c.eml", out.data)
    _assert_clean(full)
    cid = re.search(r"Content-ID: <([^>]+)>", full)
    assert cid and f"cid:{cid.group(1)}" in full, "html must still reference the renamed cid"


def _multi_page_pdf(pages: list[list[str]]) -> bytes:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page(width=612, height=792)
        for i, line in enumerate(lines):
            page.insert_text(fitz.Point(72, 72 + 20 * i), line, fontsize=12)
    return doc.tobytes()


def test_pdf_withholds_per_page(ctx: Ctx) -> None:
    pdf = _multi_page_pdf(
        [
            ["Welcome to the Department of Operations Research", f"SSN: {SSN}"],
            ["OFFICIAL TRANSCRIPT", "Cumulative GPA 3.20", "Credits attempted 32"],
            ["Housing information for new arrivals"],
        ]
    )
    out = process("packet.pdf", pdf, ctx)
    assert out.status == "redacted" and out.data, out.notes
    assert "withheld_pages=1/3" in out.notes
    assert out.category == "transcript_or_grades"
    with fitz.open(stream=out.data, filetype="pdf") as d:
        assert d.page_count == 3
        p1, p2, p3 = (pg.get_text() for pg in d)
    _assert_clean(p1)
    assert "Operations" in p1
    assert "WITHHELD" in p2 and "3.20" not in p2 and "GPA" not in p2
    assert "Housing" in p3


def test_pdf_all_pages_flagged_is_withheld(ctx: Ctx) -> None:
    pdf = _multi_page_pdf([["OFFICIAL TRANSCRIPT", "Cumulative GPA 3.2"]] * 2)
    out = process("t.pdf", pdf, ctx)
    assert out.status == "withheld" and out.ext == ".WITHHELD.txt"


def test_leakcheck_ignores_date_header() -> None:
    # Regression: names that are also day/month abbreviations matched every Date header.
    from inkwell.recognizers import KnownNameRecognizer

    known = KnownNameRecognizer.from_names(["Tue, Mar"])
    eml = b"Date: Tue, 5 Mar 2019 10:00:00 -0400\nSubject: hi\n\nbody\n"
    assert find_leaks("a.eml", eml, known) == []
    assert find_leaks("a.eml", b"Subject: Mar\n\nbody\n", known) == ["known_student_name"]


def test_html_all_non_presentational_attrs_scrubbed(ctx: Ctx) -> None:
    html = (
        '<html><body><img src="https://example.com/photos/Fernhollow.jpg" style="width:10px">'
        '<div id="Quintavius-Fernhollow" class="msg">hi</div></body></html>'
    )
    out = process("p.html", html.encode(), ctx)
    assert out.status == "redacted" and out.data
    s = out.data.decode()
    _assert_clean(s)
    assert 'style="width:10px"' in s and 'class="msg"' in s


def test_text_layer_does_not_merge_adjacent_words(ctx: Ctx) -> None:
    from inkwell.engine import Word
    from inkwell.handlers import _add_page

    doc = fitz.open()
    img = Image.new("RGB", (600, 200), "white")
    words = [Word("X", 100, 100, 130, 140, (1,)), Word("y", 130, 100, 140, 140, (1,))]
    _add_page(doc, img, words, ctx)
    assert "Xy" not in doc[0].get_text()


def test_text_layer_vertical_words_not_truncated(ctx: Ctx) -> None:
    # Regression: vertical text (tall, narrow OCR box) was inserted horizontally with a font
    # sized to the box height; it ran off the page and extraction truncated "Ordinals" to "Ord",
    # a known-name token, so the leak check blocked the file.
    from inkwell.engine import Word
    from inkwell.handlers import _add_page

    doc = fitz.open()
    img = Image.new("RGB", (600, 400), "white")
    words = [Word("Ordinals", 565, 40, 594, 118, (1,)), Word("GEOLOGY", 520, 200, 558, 366, (2,))]
    _add_page(doc, img, words, ctx)
    extracted = sorted(w[4] for w in doc[0].get_text("words"))
    assert extracted == ["GEOLOGY", "Ordinals"], extracted


def test_docx_extended_properties_scrubbed(ctx: Ctx) -> None:
    # Regression: Word keeps the document title in docProps/app.xml <TitlesOfParts> (and
    # Company/Manager); a known name there blocked files.
    d = Document()
    d.add_paragraph("Hello")
    buf = io.BytesIO()
    d.save(buf)
    zin = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == "docProps/app.xml":
                data = data.replace(
                    b"</Properties>",
                    b"<Company>Fernhollow Family Trust</Company><Manager>Quintavius Fernhollow</Manager>"
                    b'<TitlesOfParts><vt:vector size="1" baseType="lpstr"><vt:lpstr>File of Quintavius '
                    b"Fernhollow</vt:lpstr></vt:vector></TitlesOfParts></Properties>",
                )
            zout.writestr(item, data)
    out = process("f.docx", zbuf.getvalue(), ctx)
    assert out.status == "redacted" and out.data
    _assert_clean(extract_text("f.docx", out.data))


def test_names_glued_to_underscores_are_found() -> None:
    # Regression: "_" is a regex word character, so a name on a form blank
    # ("Name:___________Quintavius Fernhollow") escaped the known-name lookarounds; NER missed
    # it in the real document, so the known-name recognizer must match on its own.
    from inkwell.recognizers import KnownNameRecognizer

    rec = KnownNameRecognizer.from_names(["Fernhollow, Quintavius"])
    for text in ("___________Quintavius Fernhollow___", "x_Fernhollow_", "__FERNHOLLOW"):
        hits = rec.analyze(text, ["KNOWN_STUDENT_NAME"])
        assert hits and all(text[h.start : h.end].strip("_") == text[h.start : h.end] for h in hits), text
    assert not rec.find_any("Fernhollowish"), "still a whole-word match"


def test_leakcheck_ignores_presentational_html_attrs(scrubber: object) -> None:
    # Regression: a font name in a style attribute ("Gill Sans") matched a known surname;
    # style is not scrubbed (presentational), so the leak check must not read it either.
    from inkwell.recognizers import KnownNameRecognizer

    known = KnownNameRecognizer.from_names(["Gill Fennimore"])
    html = b'<html><body><p style="font-family:Gill Sans">hello</p></body></html>'
    assert find_leaks("a.html", html, known) == []
    assert find_leaks("a.html", b'<html><body><p title="Gill">x</p></body></html>', known) != []


def test_eml_container_name_params_scrubbed(ctx: Ctx) -> None:
    # Regression guard: name=/filename= parameters on parts that are not replaced as
    # attachments (e.g. a multipart container) must still be scrubbed.
    m = EmailMessage()
    m["From"] = "a@example.edu"
    m["Subject"] = "x"
    m.set_content("hello")
    m.add_alternative("<p>hello</p>", subtype="html")
    m.set_param("name", f"{STUDENT} file")
    out = process("n.eml", m.as_bytes(), ctx)
    assert out.data is not None
    assert b"Fernhollow" not in out.data and b"Quintavius" not in out.data
