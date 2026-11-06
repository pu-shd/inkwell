"""Post-scrub verification: re-extract text from the produced output and look for residual
high-risk identifiers. A hit means the output is discarded (fail closed)."""

from __future__ import annotations

import io
import re
import zipfile
from email import policy
from email.parser import BytesParser

import fitz
from bs4 import BeautifulSoup, Tag
from openpyxl import load_workbook

from .handlers import _SAFE_ATTRS as SAFE_ATTRS
from .recognizers import KnownNameRecognizer
from .sniff import sniff

# Retained headers that cannot carry names; a Date header must not match people whose
# names are also day or month abbreviations.
_NON_PII_HEADERS = {"date", "mime-version", "content-transfer-encoding"}

SSN_RX = re.compile(r"(?<!\d)\d{3}[\s.\-–]\d{2}[\s.\-–]\d{4}(?!\d)")
LABELED_SSN_RX = re.compile(r"(?i)\b(?:SSN|social\s+security)\b[^\n]{0,15}?\d{3}\D?\d{2}\D?\d{4}")


def extract_text(name: str, data: bytes, depth: int = 0) -> str:
    kind = sniff(name, data)
    if kind == "pdf":
        with fitz.open(stream=data, filetype="pdf") as d:
            meta = " ".join(str(v) for v in (d.metadata or {}).values() if v)
            return meta + "\n" + "\n".join(p.get_text("text") for p in d)
    if kind == "eml":
        msg = BytesParser(policy=policy.compat32).parsebytes(data)
        parts = [f"{k}: {v}" for k, v in msg.items() if k.lower() not in _NON_PII_HEADERS]
        for p in msg.walk():
            parts += [f"{k}: {v}" for k, v in p.items() if k.lower() not in _NON_PII_HEADERS]
            if p.is_multipart():
                continue
            payload = p.get_payload(decode=True)
            if isinstance(payload, bytes):
                parts.append(
                    extract_text(p.get_filename() or "part", payload, depth + 1) if depth < 3 else ""
                )
        return "\n".join(parts)
    if kind == "html":
        soup = BeautifulSoup(data, "lxml")
        attrs = [
            str(v)
            for t in soup.find_all(True)
            if isinstance(t, Tag)
            for k, v in t.attrs.items()
            if k not in SAFE_ATTRS  # presentational (style, class...) are deliberately kept
        ]
        return soup.get_text("\n") + "\n" + "\n".join(attrs)
    if kind == "docx":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "\n".join(
                re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "replace"))
                for n in z.namelist()
                if n.endswith(".xml")
            )
    if kind == "xlsx":
        wb = load_workbook(io.BytesIO(data), read_only=True)
        out = [str(wb.properties.creator or "")]
        for ws in wb.worksheets:
            out.append(ws.title)
            for row in ws.iter_rows(values_only=True):
                out.append("\t".join("" if v is None else str(v) for v in row))
        return "\n".join(out)
    if kind == "zip":
        if depth >= 3:
            return ""
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "\n".join(
                i.filename + "\n" + extract_text(i.filename, z.read(i), depth + 1)
                for i in z.infolist()
                if not i.is_dir()
            )
    if kind == "text":
        return data.decode("utf-8", "replace")
    return ""  # images: text is verified at redaction time; re-OCR is not repeatable


def find_leaks(name: str, data: bytes, known: KnownNameRecognizer | None) -> list[str]:
    text = extract_text(name, data)
    issues: list[str] = []
    if SSN_RX.search(text) or LABELED_SSN_RX.search(text):
        issues.append("ssn_pattern")
    if known is not None and not known.is_empty and known.find_any(text):
        issues.append("known_student_name")
    return issues
