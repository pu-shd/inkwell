"""Detect file kind from content (extensions in this corpus are unreliable)."""

from __future__ import annotations

import re
import zipfile
from io import BytesIO

KINDS = ("pdf", "image", "eml", "docx", "xlsx", "html", "text", "zip", "drop", "unsupported")

# Saved-web-page assets and OS cruft: dropped, never copied (could hide PII in JSON/JS).
_DROP_EXT = {
    ".js",
    ".css",
    ".ds_store",
    ".db",
    ".download",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".map",
    ".ico",
}
_DROP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

_EML_HEADER = re.compile(
    rb"^(?:Return-Path|Received|From|To|Subject|Date|Message-ID|MIME-Version|Delivered-To|"
    rb"X-[\w-]+|Content-Type):",
    re.IGNORECASE | re.MULTILINE,
)

IMAGE_EXT = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "tiff": ".tif", "bmp": ".bmp", "webp": ".webp"}


def image_format(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def sniff(name: str, data: bytes) -> str:
    lname = name.lower()
    base = lname.rsplit("/", 1)[-1]
    head = data[:4096]
    if base in _DROP_NAMES or base.startswith("._"):
        return "drop"
    if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
        return "pdf"
    if image_format(head):
        return "image"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(BytesIO(data)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            return "unsupported"
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if any(n.startswith("ppt/") for n in names):
            return "unsupported"
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # legacy OLE (.doc/.xls) - not supported
        return "unsupported"
    for ext in _DROP_EXT:
        if lname.endswith(ext):
            return "drop"
    text_head = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    low = text_head[:512].lower()
    if low.startswith((b"<!doctype html", b"<html", b"<?xml")) and (
        b"<html" in head.lower() or lname.endswith((".htm", ".html", ".xhtml"))
    ):
        return "html"
    if lname.endswith((".htm", ".html", ".xhtml", ".mht")):
        return "html"
    if lname.endswith(".eml") or len(_EML_HEADER.findall(head)) >= 2:
        return "eml"
    if lname.endswith((".txt", ".csv", ".tsv", ".md", ".rtf")) or _looks_textual(head):
        return "text"
    return "unsupported"


def _looks_textual(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def output_ext(kind: str, name: str, data: bytes) -> str:
    if kind == "pdf":
        return ".pdf"
    if kind == "image":
        fmt = image_format(data[:16]) or "png"
        return IMAGE_EXT.get(fmt, ".png")
    return {
        "eml": ".eml",
        "docx": ".docx",
        "xlsx": ".xlsx",
        "html": ".html",
        "text": ".txt",
        "zip": ".zip",
    }.get(kind, ".bin")
