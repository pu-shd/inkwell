"""LaTeX math regions in text.

Names and places that NER or the registry matcher find inside math are variables and operators
("$L_i$", "\\mathcal{L}"), so they are not redaction targets there. Detection is deliberately
strict, because anything counted as math is not searched for names:

* ``$...$`` follows the TeX-in-text rule used by pandoc: no space just inside either delimiter
  and no digit right after the closing ``$``, so "paid $500 to Jane Roe for $20" is not math; it
  may wrap onto the next line but not cross a blank line;
* ``$$...$$``, ``\\(...\\)``, ``\\[...\\]`` and equation-like environments;
* a region must contain a TeX command (or be a tiny ``$x$``); one with two adjacent
  capitalized words ("\\text{Jane Roe}") or five ordinary words in a row is treated as prose.
"""

from __future__ import annotations

import re

_ENVS = r"equation|align|alignat|flalign|eqnarray|gather|multline|displaymath|math"
_BLOCKS = (
    re.compile(r"\\begin\{(" + _ENVS + r")(\*?)\}.*?\\end\{\1\2\}", re.S),
    re.compile(r"\$\$.+?\$\$", re.S),
    re.compile(r"\\\(.+?\\\)", re.S),
    re.compile(r"\\\[.+?\\\]", re.S),
)
# TeX allows a line break inside $...$ but not a blank line (a paragraph break).
_INLINE = re.compile(r"(?<![\\$])\$(?=[^\s$])(?:[^$\n]|\n(?![ \t]*\n)){1,500}?(?<=[^\s\\])\$(?![\d$])")
_PROSE = re.compile(r"(?<![\\A-Za-z])[A-Z][a-z]+\s+[A-Z][a-z]+\b")
_MAX_BLOCK = 20_000
_CMD = re.compile(r"\\[A-Za-z]+")
# Five ordinary words in a row is prose, not math (scripts and templates in saved web pages put
# stray dollar signs around whole sentences).
_WORD_RUN = re.compile(r"(?:[A-Za-z]{2,}[ \t\n,.;:'()]+){4}[A-Za-z]{2,}")


def _looks_like_math(region: str) -> bool:
    """A TeX command (or a tiny ``$x$``) and no prose. Operators alone are not enough: scripts and
    templates in saved web pages put dollar signs, braces and "=" around whole sentences."""
    if _PROSE.search(region):
        return False
    inner = region.strip("$").strip()
    if len(inner) <= 3 and not re.search(r"[A-Za-z]{2,}", inner):
        return True  # $x$, $n$, $x_i$
    if _WORD_RUN.search(_CMD.sub(" ", region)):
        return False
    return bool(_CMD.search(region))


def math_spans(text: str) -> list[tuple[int, int]]:
    """Sorted, non-overlapping (start, end) spans of LaTeX math in text."""
    if "$" not in text and "\\" not in text:
        return []
    spans: list[tuple[int, int]] = []
    masked = text
    for rx in _BLOCKS:
        for m in rx.finditer(masked):
            if m.end() - m.start() <= _MAX_BLOCK and _looks_like_math(m.group()):
                spans.append((m.start(), m.end()))
        for s, e in spans:  # later patterns must not match inside an earlier region
            masked = masked[:s] + " " * (e - s) + masked[e:]
    for m in _INLINE.finditer(masked):
        if _looks_like_math(m.group()):
            spans.append((m.start(), m.end()))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return merged


def inside(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def mask(text: str, spans: list[tuple[int, int]]) -> str:
    """The text with math regions blanked out (same length)."""
    for s, e in spans:
        text = text[:s] + " " * (e - s) + text[e:]
    return text


# --- typeset math in PDFs ---------------------------------------------------------------------
# LaTeX sets math in dedicated fonts (Computer Modern math italic/symbols/extension, AMS, Euler,
# txfonts/pxfonts, OpenType math fonts); names, even inside \text{}, are set in text fonts.
_MATH_FONT = re.compile(
    r"(?i)^(?:cm(?:mi|sy|ex|bsy|mib)\d*|msam\d*|msbm\d*|eu(?:fm|sm|rm|fb|sb|rb|ex)\d*|rsfs\d*|"
    r"lmmath|latinmodern-?math|cambria-?math|stix\w*math|xits\w*math|texgyre\w*math|newcm\w*math|"
    r"libertinus\w*math|fira\w*math|asana|(?:tx|px)(?:sy|mi|ex|mia)|rtxmi|mt-?extra)"
)


def is_math_font(name: str) -> bool:
    """True for a PDF font used only for math ("ABCDEF+CMMI10" -> CMMI10)."""
    return bool(_MATH_FONT.match(name.split("+", 1)[-1]))


def mostly_inside(
    box: tuple[float, float, float, float], rects: list[tuple[float, float, float, float]]
) -> bool:
    """More than half of the box's area lies inside one of the rects."""
    x0, y0, x1, y1 = box
    area = max((x1 - x0) * (y1 - y0), 1e-9)
    for a0, b0, a1, b1 in rects:
        w = min(x1, a1) - max(x0, a0)
        h = min(y1, b1) - max(y0, b0)
        if w > 0 and h > 0 and w * h > 0.5 * area:
            return True
    return False
