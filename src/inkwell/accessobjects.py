"""Sanitize and audit Access objects exported with SaveAsText, before a rebuild imports them.

Exported forms, reports, queries and modules can carry personal data outside the tables: saved
filters (a student's surname, an SSN), printer names (a staff member's printer, a dorm printer)
and free text. `sanitize` removes what never belongs in a rebuilt database; `Audit` then checks
the result and reports anything identifying, masked, so a person can review it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from .recognizers import KnownNameRecognizer

_FILTER = re.compile(r"^\s*(?:Filter|ServerFilter)\s*=")
_CONTINUATION = re.compile(r'^\s+"')
_CHECKSUM = re.compile(r"^\s*Checksum\s*=")
# Exported from an .mdb, rejected by LoadFromText into an .accdb ("does not apply to this control").
_ACCDB_REJECTS = re.compile(r"^\s*NoSaveCTIWhenDisabled\s*=")
_SSN_MASK = re.compile(r'^\s*InputMask\s*="000\\+-00\\+-0000')
_BLOCK = re.compile(r"^(\s*)(PrtDevNamesW?|PrtDevModeW?)\s*=\s*Begin\s*$")
_HEX = re.compile(r"0x([0-9a-fA-F]+)")
_HEX_LINE = re.compile(r"^\s*(?:0x[0-9a-fA-F]+\s*,?\s*)+$")
# DEVMODE starts with the device name: CHAR[32] (ANSI) or WCHAR[32] (the W variant).
_DEVICE_NAME_BYTES = {"PrtDevMode": 32, "PrtDevModeW": 64}


def read_object(raw: bytes) -> tuple[str, str]:
    """Text and encoding of a SaveAsText file: UTF-16 or UTF-8 with a BOM, or, without one,
    what Access reads as ANSI (cp1252; plain UTF-8 only when it decodes as such)."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "cp1252"


def write_object(text: str, encoding: str) -> bytes:
    """Bytes in the encoding the file was read with, so LoadFromText reads it as the original."""
    return text.encode(encoding)


def decode(raw: bytes) -> str:
    return read_object(raw)[0]


def _blank_device_name(lines: list[str], nbytes: int) -> tuple[list[str], bool]:
    """Zero the first `nbytes` bytes of a hex block, keeping every chunk's length and place."""
    left, changed, out = nbytes, False, []
    for line in lines:

        def repl(m: re.Match[str]) -> str:
            nonlocal left, changed
            digits = m.group(1)
            if left <= 0:
                return m.group(0)
            take = min(left, len(digits) // 2)
            left -= take
            new = "0" * (take * 2) + digits[take * 2 :]
            changed = changed or new.lower() != digits.lower()
            return "0x" + new

        out.append(_HEX.sub(repl, line))
    return out, changed


def sanitize(text: str) -> tuple[str, Counter[str]]:
    """Remove saved filters, checksums, SSN input masks and printer names; blank the device name
    in page setup. Returns the text and what was changed. Idempotent."""
    lines = text.splitlines()
    out: list[str] = []
    changes: Counter[str] = Counter()
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FILTER.match(line):
            changes["filter"] += 1
            i += 1
            while i < len(lines) and _CONTINUATION.match(lines[i]):
                i += 1
            continue
        if _CHECKSUM.match(line):
            changes["checksum"] += 1
            i += 1
            continue
        if _ACCDB_REJECTS.match(line):
            changes["accdb_rejects"] += 1
            i += 1
            continue
        if _SSN_MASK.match(line):
            changes["ssn_input_mask"] += 1
            i += 1
            continue
        block = _BLOCK.match(line)
        if block:
            indent, kind = block.groups()
            end = i + 1
            while end < len(lines) and lines[end] != f"{indent}End":
                end += 1
            body = lines[i + 1 : end]
            if kind.startswith("PrtDevNames"):
                changes["printer_names"] += 1
            else:
                body, changed = _blank_device_name(body, _DEVICE_NAME_BYTES[kind])
                changes["device_name"] += int(changed)
                out += [line, *body, f"{indent}End"]
            i = end + 1
            continue
        out.append(line)
        i += 1
    ending = "\r\n" if "\r\n" in text else "\n"
    return ending.join(out) + (ending if text.endswith(("\n", "\r\n")) else ""), changes


@dataclass(frozen=True)
class Finding:
    object: str
    line: int
    kind: str
    context: str  # the line with every finding masked


_PATTERNS = (
    ("email", re.compile(r"[^@\s\"'<>()]+@[^@\s\"'<>()]+\.[A-Za-z]{2,}"), "«EMAIL»"),
    ("phone", re.compile(r"\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"), "«PHONE»"),
    ("ssn", re.compile(r"(?<!\d)\d{3}[\s.-]\d{2}[\s.-]\d{4}(?!\d)"), "«SSN»"),
    ("nine_digits", re.compile(r"(?<![\w.])\d{9}(?![\w.])"), "«#########»"),
)


@dataclass
class Audit:
    """Checks sanitized objects. `allowed_words` are reviewed name words (ordinary words or staff
    names) with the number of uses reviewed; any further use is a finding."""

    known: KnownNameRecognizer
    tokens: Iterable[str]
    allowed_words: dict[str, int] = field(default_factory=dict)
    used: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        toks = sorted({t for t in self.tokens if len(t) >= 4}, key=len, reverse=True)
        self._tok = re.compile("|".join(map(re.escape, toks))) if toks else None

    def scan(self, label: str, text: str) -> list[Finding]:
        findings: list[Finding] = []
        for n, line in enumerate(text.splitlines(), 1):
            kinds: list[str] = []
            masked = line
            if self._tok is not None and self._tok.search(line):
                kinds.append("record_token")
                masked = self._tok.sub("«TOKEN»", masked)
            numeric = not (_HEX_LINE.match(line) or _CHECKSUM.match(line))
            for kind, rx, mask in _PATTERNS:
                if kind == "nine_digits" and not numeric:
                    continue
                if rx.search(masked):
                    kinds.append(kind)
                    masked = rx.sub(mask, masked)
            for s, e, full in sorted(self.known.matches(line), key=lambda m: -m[0]):
                word = line[s:e]
                if not full and self.used[word] < self.allowed_words.get(word, 0):
                    self.used[word] += 1
                    continue
                kinds.append("full_name" if full else "name_word")
            for s, e, _ in sorted(self.known.matches(masked), key=lambda m: -m[0]):
                masked = masked[:s] + "«NAME»" + masked[e:]
            for kind in dict.fromkeys(kinds):
                findings.append(Finding(label, n, kind, masked.strip()[:160]))
        return findings
