"""LaTeX math regions are not redaction targets: names and places detected inside math are false
positives (variables, operators). Strong identifiers (SSNs, emails) are still redacted there."""

from __future__ import annotations

import pytest

from inkwell.engine import Findings, Scrubber
from inkwell.leakcheck import find_leaks
from inkwell.mathregions import math_spans

from .conftest import SSN, STUDENT

SUR = STUDENT.split()[1]


def _regions(text: str) -> list[str]:
    return [text[s:e] for s, e in math_spans(text)]


@pytest.mark.parametrize(
    "text",
    [
        r"Let $\mathcal{L}(\theta) = \sum_{i=1}^{n} \log p(x_i \mid \theta)$ be the likelihood.",
        r"Display: $$\int_0^1 f(x)\,dx = F(1) - F(0)$$ done.",
        r"Inline \(\alpha + \beta\) and display \[\lim_{n \to \infty} a_n = L\].",
        "\\begin{equation}\n  E[X] = \\mu\n\\end{equation}",
        "\\begin{align*}\n  y &= Ax + b \\\\\n  z &= Ly\n\\end{align*}",
        "Tiny $x$ and $n$.",
    ],
)
def test_math_regions_found(text: str) -> None:
    found = _regions(text)
    assert found and all(("$" in f or "\\" in f) for f in found), found


@pytest.mark.parametrize(
    "text",
    [
        f"Paid $500 to {STUDENT} for $20 of books.",  # dollar amounts, not math
        f"Costs $5 and $10; {STUDENT} paid.",
        f"$ {STUDENT} $",  # space inside the delimiters
        rf"$\text{{{STUDENT}}}$",  # prose inside math
        f"No math here, {STUDENT}.",
        # prose between stray dollar signs (scripts and templates in saved pages)
        f"$(document).ready(function() {{ ask {SUR} at the office in Boston; }});$x",
        f"$$ Please send the forms to {SUR} at the main office before Friday $$",
        f"$.ajax({{ url: page, data: notes from {SUR} about the visit to Boston }});$",
    ],
)
def test_not_math(text: str) -> None:
    assert not any(STUDENT in r or SUR in r for r in _regions(text)), _regions(text)


def _red(sc: Scrubber, text: str) -> str:
    return sc.redact_text(text, Findings())


def test_math_left_alone_names_outside_still_redacted(scrubber: Scrubber) -> None:
    math = r"$\mathcal{L}(\theta) = \sum_{i} \log p(x_i \mid \theta) + \lambda \|\theta\|^2$"
    block = "\\begin{equation}\n  Var[X] = E[X^2] - E[X]^2\n\\end{equation}"
    text = f"{STUDENT} derived {math} and\n{block}\nfor the seminar."
    out = _red(scrubber, text)
    assert math in out and block in out, "math is untouched"
    assert STUDENT not in out and SUR not in out, "the name outside math is redacted"


def test_known_name_word_inside_math_is_a_variable(scrubber: Scrubber) -> None:
    text = f"Define $\\hat{{{SUR}}}_i = x_i^2$ as before. {SUR} agreed."
    out = _red(scrubber, text)
    assert f"$\\hat{{{SUR}}}_i = x_i^2$" in out and not out.rstrip().endswith(f"{SUR} agreed.")
    assert find_leaks("a.txt", out.encode(), scrubber.known) == [], "math is not a leak"


def test_operators_without_a_tex_command_are_not_math(scrubber: Scrubber) -> None:
    # "$a = {SUR}$" could be code in a saved page as easily as math: only TeX commands prove math.
    assert _regions(f"Set $a = {SUR} + b$ now.") == []
    assert SUR not in _red(scrubber, f"Set $a = {SUR} + b$ now.")


def test_strong_identifiers_still_redacted_in_math(scrubber: Scrubber) -> None:
    out = _red(scrubber, f"$s = {SSN}$ and $e = ada@example.org$")
    assert SSN not in out and "ada@example.org" not in out


def test_leak_check_still_sees_names_outside_math(scrubber: Scrubber) -> None:
    assert find_leaks("a.txt", f"$x_i$ {STUDENT}".encode(), scrubber.known) == ["known_student_name"]
    assert find_leaks("a.txt", f"${SSN}$".encode(), scrubber.known) == ["ssn_pattern"]


# --- PDFs: math is recognized by its fonts (LaTeX math italic, symbols, AMS, OpenType math) -----


@pytest.mark.parametrize(
    ("font", "is_math"),
    [
        ("ABCDEF+CMMI10", True),
        ("CMSY7", True),
        ("CMEX10", True),
        ("MSBM10", True),
        ("LMMathItalic10-Regular", True),
        ("CambriaMath", True),
        ("STIXTwoMath-Regular", True),
        ("XITSMath", True),
        ("ABCDEF+CMR10", False),
        ("CMTI10", False),
        ("Helvetica", False),
        ("TimesNewRomanPSMT", False),
        ("LMRoman10-Regular", False),
    ],
)
def test_math_font_names(font: str, is_math: bool) -> None:
    from inkwell.mathregions import is_math_font

    assert is_math_font(font) is is_math


def test_words_in_math_fonts_are_not_redacted(scrubber: Scrubber) -> None:
    from inkwell.imaging import Word

    words = [
        Word(t, i * 50.0, 0, i * 50.0 + 40, 10, (0, 0)) for i, t in enumerate(STUDENT.split() + ["wrote"])
    ]
    words += [
        Word(t, i * 50.0, 20, i * 50.0 + 40, 30, (0, 1)) for i, t in enumerate([f"{SUR}_i", "=", SUR, SSN])
    ]
    math = [False] * 3 + [True] * 4
    hits, labels = scrubber.redact_words_labeled(words, Findings(), math=math)
    assert set(hits) == {0, 1, 6}, "the name in text and the SSN in math are redacted, math names are not"
    assert all(i < 3 or i == 6 for i in labels)


def test_pdf_math_font_preserved(
    scrubber: Scrubber, ctx: object, make_ocr: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stand-in for a LaTeX PDF: formulas set in Courier, which the test declares a math font.
    import re

    import fitz

    from inkwell import mathregions
    from inkwell.handlers import process

    monkeypatch.setattr(mathregions, "_MATH_FONT", re.compile(r"(?i)courier"))
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(fitz.Point(72, 100), f"Notes by {STUDENT} on the lecture", fontsize=14, fontname="helv")
    page.insert_text(fitz.Point(72, 160), f"{SUR} = Lovelace + Babbage", fontsize=18, fontname="cour")
    out = process("m.pdf", doc.tobytes(), ctx)  # type: ignore[arg-type]
    assert out.status == "redacted" and out.data
    with fitz.open(stream=out.data, filetype="pdf") as d:
        layer = d[0].get_text()
    assert "Quintavius" not in layer and SUR not in layer, "math words are kept out of the text layer"
    ocr = make_ocr(out.data)  # type: ignore[operator]
    assert "Lovelace" in ocr and "Babbage" in ocr, "the formula stays visible on the page"
    assert "Quintavius" not in ocr
    assert find_leaks("m.pdf", out.data, scrubber.known) == []


# --- math spread over lines or HTML elements ------------------------------------------------------


def test_prose_between_dollars_is_redacted(scrubber: Scrubber) -> None:
    out = _red(scrubber, f"$(document).ready(function() {{ ask {STUDENT} at the office; }});$x")
    assert SUR not in out and "Quintavius" not in out


def test_inline_math_may_wrap_but_not_cross_a_paragraph() -> None:
    assert _regions("Let $a +\n \\beta_i$ hold.") == ["$a +\n \\beta_i$"], "TeX allows a line break in $...$"
    assert _regions("Costs $\\alpha\n\n b$ x") == [], "a blank line ends inline math"


def test_html_math_split_over_elements_is_preserved(ctx: object) -> None:
    from inkwell.handlers import process
    from inkwell.leakcheck import extract_text

    html = (
        f"<html><body><p>Let $\\mathcal{{L}}(<i>{SUR}</i>, <b>Lovelace</b>) = \\sum_i x_i$ hold.</p>"
        f"<p>{STUDENT} wrote this.</p></body></html>"
    )
    out = process("a.html", html.encode(), ctx)  # type: ignore[arg-type]
    assert out.data
    text = extract_text("a.html", out.data)
    assert f"<i>{SUR}</i>" in out.data.decode() and "<b>Lovelace</b>" in out.data.decode(), "math kept"
    assert "Quintavius" not in text, "the name outside math is still redacted"
