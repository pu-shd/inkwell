from __future__ import annotations

import io
import zipfile

import pytest

from inkwell.classify import classify
from inkwell.sniff import sniff

# --- classify ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("OFFICIAL TRANSCRIPT\nCumulative GPA 3.71  Credits Earned 64", "transcript_or_grades"),
        ("Grade Report Fall 1989: ORF 523 A-, MAT 520 B+ ; GPA 3.5", "transcript_or_grades"),
        ("Student Class Schedule\nMWF 10:00 - 10:50 ORF 405", "class_schedule"),
        ("Notice of disciplinary action: academic integrity violation; sanction imposed", "disciplinary"),
        ("Financial aid award letter: fellowship stipend $32,000 and tuition", "financial_aid"),
        ("IEP meeting notes - Individualized Education Program, 504 plan", "special_education"),
        ("Fingerprint card enclosed for biometric verification", "biometric"),
    ],
)
def test_withheld_categories(text: str, category: str) -> None:
    c = classify(text, threshold=3)
    assert c.withhold, c.scores
    assert c.category == category


def test_benign_not_withheld() -> None:
    c = classify("Welcome to the department! Orientation is Monday at 9am in Room 101.", 3)
    assert not c.withhold
    assert c.category is None


# --- sniff ------------------------------------------------------------------


def _zip(names: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in names.items():
            z.writestr(n, d)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("name", "data", "kind"),
    [
        ("x.pdf", b"%PDF-1.4 ...", "pdf"),
        (
            "re  welcome to the lab! - 'A' (a@x.edu) - 2023-12-17 1729",
            b"From: a@x\nTo: b@x\nSubject: hi\n\nbody",
            "eml",
        ),
        ("pic.dat", b"\x89PNG\r\n\x1a\n" + b"\0" * 20, "image"),
        ("a.jpg", b"\xff\xd8\xff\xe0" + b"\0" * 20, "image"),
        ("d.docx", _zip({"word/document.xml": b"<x/>"}), "docx"),
        ("s.xlsx", _zip({"xl/workbook.xml": b"<x/>"}), "xlsx"),
        ("a.zip", _zip({"a.txt": b"hi"}), "zip"),
        ("app.js", b"var x = 1;", "drop"),
        (".DS_Store", b"\0\0\0\1Bud1", "drop"),
        ("old.doc", b"\xd0\xcf\x11\xe0" + b"\0" * 20, "unsupported"),
        ("page.htm", b"<html><body>x</body></html>", "html"),
        ("notes.txt", b"plain words", "text"),
        ("blob.bin", b"\x00\x01\x02\xff", "unsupported"),
    ],
)
def test_sniff(name: str, data: bytes, kind: str) -> None:
    assert sniff(name, data) == kind


# --- pseudonym --------------------------------------------------------------


def test_travel_accommodation_is_not_special_education() -> None:
    # Regression: housing/travel emails were withheld as special_education in the pilot.
    text = "Your hotel accommodations are booked. Accommodation details: Nassau Inn, 2 nights."
    assert not classify(text, 3).withhold
    assert classify("Approved disability accommodations: extended exam time.", 3).withhold
