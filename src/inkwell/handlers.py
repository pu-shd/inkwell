"""Per-format scrubbing. Every handler fails closed: on any doubt, nothing is emitted."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import re
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import PurePosixPath

import fitz  # PyMuPDF
from bs4 import BeautifulSoup, Comment, Tag
from bs4.element import NavigableString
from docx import Document
from lxml import etree
from openpyxl import load_workbook
from PIL import Image, ImageOps

from . import imaging
from .classify import classify
from .config import Config
from .engine import Findings, Scrubber, Word, layer_text, redactable, resolve_overlaps
from .mathregions import is_math_font, mostly_inside
from .sniff import image_format, output_ext, sniff

MAX_DEPTH = 3


@dataclass
class Ctx:
    cfg: Config
    scrubber: Scrubber
    orig_key: str  # original relative path (only ever written to the key file)
    pseudo_key: str  # pseudonymous path used in reports/logs
    depth: int = 0
    extra_names: tuple[str, ...] = ()
    owner_ids: tuple[str, ...] = ()  # registry IDs of the person(s) this document belongs to
    redact: bool = True  # False: content passes through unchanged (status "copied")
    withhold: bool = True  # False: protected records are flagged in notes, not withheld
    flags: list[str] = field(default_factory=list)  # categories flagged while withhold=False

    def child(self, orig: str, pseudo: str, extra: Iterable[str] = ()) -> Ctx:
        return Ctx(
            self.cfg,
            self.scrubber,
            f"{self.orig_key}!{orig}",
            f"{self.pseudo_key}!{pseudo}",
            self.depth + 1,
            (*self.extra_names, *extra),
            self.owner_ids,
            self.redact,
            self.withhold,
        )


@dataclass
class Outcome:
    status: str  # redacted | copied | withheld | dropped | unsupported | error
    kind: str
    data: bytes | None = None
    ext: str = ""
    category: str | None = None
    findings: Findings = field(default_factory=Findings)
    notes: list[str] = field(default_factory=list)
    keys: list[tuple[str, str]] = field(default_factory=list)


class Withhold(Exception):  # noqa: N818 - control-flow signal
    def __init__(self, category: str, score: int) -> None:
        super().__init__(category)
        self.category = category
        self.score = score


def _check_withhold(text: str, ctx: Ctx) -> None:
    c = classify(text, ctx.cfg.withhold_threshold)
    if c.withhold and c.category:
        if not ctx.withhold:
            ctx.flags.append(f"{c.category}={c.score}")
            return
        raise Withhold(c.category, c.score)


def _copied(kind: str, data: bytes, ext: str = "") -> Outcome:
    """Redaction disabled: the original bytes, unchanged."""
    return Outcome("copied", kind, data, ext, notes=["unredacted"])


def placeholder(kind: str, category: str) -> bytes:
    return (
        "This document was withheld by inkwell because it was classified as a protected\n"
        "record that is withheld in full.\n"
        f"Category: {category}\n"
        f"Original type: {kind}\n"
    ).encode()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def process(name: str, data: bytes, ctx: Ctx) -> Outcome:
    kind = sniff(name, data)
    if kind == "drop":
        return Outcome("dropped", kind)
    if kind == "unsupported":
        return Outcome("unsupported", kind)
    if kind == "zip" and ctx.depth >= MAX_DEPTH:
        return Outcome("unsupported", kind, notes=["nested too deep"])
    handler: dict[str, Callable[[bytes, Ctx], Outcome]] = {
        "pdf": handle_pdf,
        "image": handle_image,
        "eml": handle_eml,
        "docx": handle_docx,
        "xlsx": handle_xlsx,
        "html": handle_html,
        "text": handle_text,
        "zip": handle_zip,
    }
    try:
        out = handler[kind](data, ctx)
        out.kind = kind
        if not out.ext:
            out.ext = output_ext(kind, name, out.data or b"")
        if ctx.flags:  # withholding disabled: say what would have been withheld
            out.notes += [f"flagged:{f}" for f in ctx.flags]
            out.category = out.category or ctx.flags[0].split("=")[0]
        return out
    except Withhold as w:
        return Outcome(
            "withheld",
            kind,
            placeholder(kind, w.category),
            ".WITHHELD.txt",
            w.category,
            notes=[f"withhold_score={w.score}"],
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
        # Only the exception type is recorded: messages can echo document content.
        return Outcome("error", kind, notes=[f"error:{type(exc).__name__}"])


# ---------------------------------------------------------------------------
# raster helpers
# ---------------------------------------------------------------------------


Rect = tuple[float, float, float, float]


def _math_font_rects(page: fitz.Page, mat: fitz.Matrix) -> list[Rect]:
    """Image-space boxes of text set in math fonts on a page with a text layer."""
    rects: list[Rect] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text", "").strip() and is_math_font(span.get("font", "")):
                    r = fitz.Rect(span["bbox"]) * mat
                    rects.append((r.x0, r.y0, r.x1, r.y1))
    return rects


def _redact_raster(
    img: Image.Image,
    native: Sequence[Word],
    ocr: Sequence[Word],
    ctx: Ctx,
    findings: Findings,
    math_rects: Sequence[Rect] = (),
) -> tuple[Image.Image, list[Word]]:
    """Black out PII; return the image and the words that remain visible (for a text layer).

    Words lying in math-font areas (typeset formulas) are not redacted as names or places, and are
    left out of the text layer: a variable spelling a record subject's name would otherwise read as
    that name there."""
    sc = ctx.scrubber
    boxes: list[imaging.Box] = []
    labeled: list[Word] = []  # registry-ID labels printed on boxes and put in the text layer
    rects = list(math_rects)

    def in_math(w: Word) -> bool:
        return bool(rects) and mostly_inside((w.x0, w.y0, w.x1, w.y1), rects)

    for source in (native, ocr):
        if not source:
            continue
        math = [in_math(w) for w in source] if rects else None
        hits, labels = sc.redact_words_labeled(source, findings, ctx.extra_names, ctx.owner_ids, math=math)
        boxes += imaging.word_boxes(source, hits)
        if not labeled:  # native words win; OCR labels only for scanned pages
            labeled = [
                Word(lab, source[i].x0, source[i].y0, source[i].x1, source[i].y1, source[i].line_key)
                for i, lab in labels.items()
            ]
    faces = imaging.detect_faces(img)
    if faces:
        findings.counts["FACE"] += len(faces)
        boxes += faces
    out = imaging.black_out(img, boxes)
    imaging.draw_labels(out, labeled)
    layer_src = native if native else ocr
    visible = [w for w in layer_src if not imaging.overlaps_any(w, boxes) and not in_math(w)] + labeled
    return out, visible


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB" if img.mode != "L" else "L").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _add_page(doc: fitz.Document, img: Image.Image, visible: Sequence[Word], ctx: Ctx) -> None:
    k = 72.0 / ctx.cfg.dpi
    page = doc.new_page(width=img.width * k, height=img.height * k)
    page.insert_image(page.rect, stream=_encode_jpeg(img, ctx.cfg.jpeg_quality))
    for w in visible:
        h = (w.y1 - w.y0) * k
        text = layer_text(w.text)
        if h <= 0 or not text:
            continue
        # Fit the box in both directions. Height alone overflows narrow (e.g. vertical) boxes;
        # text that runs off the page is truncated on extraction ("Ordinals" -> "Ord").
        width = (w.x1 - w.x0) * k
        natural = fitz.get_text_length(text, fontsize=1)
        fs = max(1.0, min(h * 0.85, width / natural if natural > 0 else h))
        try:
            page.insert_text(
                fitz.Point(w.x0 * k, w.y1 * k - h * 0.15), text + " ", fontsize=fs, render_mode=3
            )  # trailing space keeps extraction from fusing neighbours (X + y -> Xy)
        except (ValueError, RuntimeError):
            continue


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def handle_pdf(data: bytes, ctx: Ctx) -> Outcome:
    cfg = ctx.cfg
    findings = Findings()
    notes: list[str] = []
    src = fitz.open(stream=data, filetype="pdf")
    if src.needs_pass and not src.authenticate(""):
        return Outcome("unsupported", "pdf", notes=["encrypted"])
    scale = cfg.dpi / 72.0
    pages: list[tuple[int, list[Word], list[Word], list[Rect]]] = []
    texts: list[str] = []
    low_conf_pages = 0
    for page in src:
        img, rot = _render(page, cfg.dpi)
        native: list[Word] = []
        math_rects: list[Rect] = []
        if rot == 0:
            mat = page.rotation_matrix * fitz.Matrix(scale, scale)
            for x0, y0, x1, y1, txt, b, ln, _ in page.get_text("words"):
                r = fitz.Rect(x0, y0, x1, y1) * mat
                native.append(Word(txt, r.x0, r.y0, r.x1, r.y1, (int(b), int(ln))))
            math_rects = _math_font_rects(page, mat)
        ocr, conf = imaging.ocr_words(img, cfg.tesseract_lang)
        if not native and 0 <= conf < 60:
            low_conf_pages += 1
        pages.append((rot, native, ocr, math_rects))
        texts.append(" ".join(w.text for w in native) + "\n" + " ".join(w.text for w in ocr))
    # Withholding is decided per page: scanned case files mix protected records
    # letters with correspondence, and one flagged page must not empty the whole file.
    verdicts = [classify(t, cfg.withhold_threshold) for t in texts]
    flagged = [v for v in verdicts if v.withhold and v.category]
    if flagged and not ctx.withhold:
        cat = _dominant([v.category or "" for v in flagged])
        ctx.flags.append(f"{cat}={max(v.score for v in flagged)};pages={len(flagged)}/{len(verdicts)}")
        verdicts = [dataclasses.replace(v, withhold=False) for v in verdicts]
        flagged = []
    if flagged and len(flagged) == len(verdicts):
        raise Withhold(_dominant([v.category or "" for v in flagged]), max(v.score for v in flagged))
    if not ctx.redact and not flagged:
        return _copied("pdf", data, ".pdf")

    out = fitz.open()
    for i, (page, (rot, native, ocr, math_rects), verdict) in enumerate(
        zip(src, pages, verdicts, strict=True)
    ):
        if verdict.withhold and verdict.category:
            _add_withheld_page(out, page.rect, verdict.category)
            continue
        if not ctx.redact:
            out.insert_pdf(src, from_page=i, to_page=i)  # original page, untouched
            continue
        img, _ = _render(page, cfg.dpi, force_rot=rot)
        red, visible = _redact_raster(img, native, ocr, ctx, findings, math_rects)
        _add_page(out, red, visible, ctx)
    if ctx.redact:
        out.set_metadata({})
    if low_conf_pages:
        notes.append(f"low_ocr_confidence_pages={low_conf_pages}")
    category = None
    if flagged:
        notes.append(f"withheld_pages={len(flagged)}/{len(verdicts)}")
        category = _dominant([v.category or "" for v in flagged])
    blob = out.tobytes(garbage=4, deflate=True)
    status = "redacted" if ctx.redact else "copied"
    if not ctx.redact:
        notes.append("unredacted")
    return Outcome(status, "pdf", blob, ".pdf", category, findings=findings, notes=notes)


def _dominant(cats: list[str]) -> str:
    return max(sorted(set(cats)), key=cats.count)


def _add_withheld_page(doc: fitz.Document, rect: fitz.Rect, category: str) -> None:
    page = doc.new_page(width=rect.width, height=rect.height)
    page.insert_text(
        fitz.Point(54, 90),
        f"WITHHELD BY INKWELL\nprotected record: {category}",
        fontsize=16,
    )


def _render(page: fitz.Page, dpi: int, force_rot: int | None = None) -> tuple[Image.Image, int]:
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=True)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if force_rot is not None:
        return (img.rotate(-force_rot, expand=True) if force_rot else img), force_rot
    if page.get_text("text").strip():
        return img, 0  # born-digital page: trust its orientation
    return imaging.upright(img)


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


def handle_image(data: bytes, ctx: Ctx) -> Outcome:
    findings = Findings()
    fmt = image_format(data[:16]) or "png"
    with Image.open(io.BytesIO(data)) as im:
        im.seek(0)  # animated GIF/TIFF: first frame only
        img = ImageOps.exif_transpose(im).convert("RGB")  # also drops EXIF/GPS metadata
    img, _ = imaging.upright(img)
    words, conf = imaging.ocr_words(img, ctx.cfg.tesseract_lang)
    _check_withhold(" ".join(w.text for w in words), ctx)
    if not ctx.redact:
        return _copied("image", data)
    red, _ = _redact_raster(img, [], words, ctx, findings)
    buf = io.BytesIO()
    save_fmt = {"jpeg": "JPEG", "png": "PNG", "gif": "GIF", "tiff": "TIFF", "bmp": "BMP", "webp": "WEBP"}[fmt]
    kwargs: dict[str, object] = {"quality": 85} if save_fmt in ("JPEG", "WEBP") else {}
    red.save(buf, save_fmt, **kwargs)
    notes = [f"low_ocr_confidence={conf:.0f}"] if 0 <= conf < 60 else []
    return Outcome("redacted", "image", buf.getvalue(), findings=findings, notes=notes)


# ---------------------------------------------------------------------------
# segment-preserving text redaction (HTML text nodes, docx runs, spreadsheet cells)
# ---------------------------------------------------------------------------


def redact_segments(
    sc: Scrubber,
    segs: Sequence[str],
    seps: Sequence[str],
    findings: Findings,
    extra: Iterable[str] = (),
    context_ids: Iterable[str] = (),
) -> list[str]:
    """Analyze segments as one text (so labels and values in adjacent nodes connect), then
    rewrite each segment in place. ``seps[i]`` is the separator emitted after ``segs[i]``."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    for seg_text, sep in zip(segs, seps, strict=True):
        spans.append((pos, pos + len(seg_text)))
        parts.append(seg_text)
        parts.append(sep)
        pos += len(seg_text) + len(sep)
    text = "".join(parts)
    results = sc.analyze(text, extra)
    if not results:
        return list(segs)
    findings.add(results)
    merged = resolve_overlaps(redactable(results))
    if not merged:
        return list(segs)
    ctx_ids = tuple(context_ids)
    out: list[str] = []
    for seg, (s, e) in zip(segs, spans, strict=True):
        hits = [r for r in merged if r.start < e and r.end > s]
        if not hits:
            out.append(seg)
            continue
        buf: list[str] = []
        cur = 0
        for r in hits:
            hs, he = max(r.start, s) - s, min(r.end, e) - s
            buf.append(seg[cur:hs])
            if r.start >= s:  # label only where the entity begins
                buf.append(sc.label(r.entity_type, text[r.start : r.end], ctx_ids, findings))
            cur = he
        buf.append(seg[cur:])
        out.append("".join(buf))
    return out


# ---------------------------------------------------------------------------
# plain text
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def handle_text(data: bytes, ctx: Ctx) -> Outcome:
    text = _decode(data)
    _check_withhold(text, ctx)
    if not ctx.redact:
        return _copied("text", data, ".txt")
    f = Findings()
    red = ctx.scrubber.redact_text(text, f, ctx.extra_names, ctx.owner_ids)
    return Outcome("redacted", "text", red.encode("utf-8"), ".txt", findings=f)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_DROP_TAGS = ("script", "style", "noscript", "iframe", "object", "embed", "template", "meta", "base", "link")
# Presentational attributes are left alone; every other attribute value is scrubbed.
_SAFE_ATTRS = frozenset(
    "style class width height border align valign cellpadding cellspacing bgcolor color face size "
    "type dir lang colspan rowspan target rel span start shape coords nowrap hspace vspace "
    "frame rules scope charset".split()
)


def scrub_html_string(html: str, ctx: Ctx, findings: Findings, classify_too: bool) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(list(_DROP_TAGS)):
        t.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    nodes = [n for n in soup.find_all(string=True) if isinstance(n, NavigableString)]
    segs = [str(n) for n in nodes]
    if classify_too:
        _check_withhold(" ".join(segs), ctx)
    new = redact_segments(ctx.scrubber, segs, ["\n"] * len(segs), findings, ctx.extra_names, ctx.owner_ids)
    for n, s, old in zip(nodes, new, segs, strict=True):
        if s != old:
            n.replace_with(s)
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for attr in list(tag.attrs):
            val = tag.attrs[attr]
            if attr.startswith("on"):
                del tag.attrs[attr]
                continue
            if attr in _SAFE_ATTRS:
                continue
            if isinstance(val, list):
                val = " ".join(val)
            if not isinstance(val, str) or not val.strip():
                continue
            if attr == "src" and val.lower().startswith("cid:"):
                continue  # renamed by the email handler
            if attr == "href" and val.lower().startswith("mailto:"):
                tag.attrs[attr] = "mailto:redacted@invalid"
                findings.counts["EMAIL_ADDRESS"] += 1
                continue
            tag.attrs[attr] = ctx.scrubber.redact_text(val, findings, ctx.extra_names, ctx.owner_ids)
    if soup.title and soup.title.string:
        soup.title.string = ctx.scrubber.redact_text(
            str(soup.title.string), findings, ctx.extra_names, ctx.owner_ids
        )
    return str(soup)


def handle_html(data: bytes, ctx: Ctx) -> Outcome:
    if not ctx.redact:
        soup = BeautifulSoup(_decode(data), "lxml")
        _check_withhold(soup.get_text(" "), ctx)
        return _copied("html", data, ".html")
    f = Findings()
    out = scrub_html_string(_decode(data), ctx, f, classify_too=True)
    return Outcome("redacted", "html", out.encode("utf-8"), ".html", findings=f)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _cid_map(msg: Message) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in msg.walk():
        raw = part.get("Content-ID")
        if raw:
            old = str(raw).strip().strip("<>")
            out[old] = hashlib.sha256(old.encode()).hexdigest()[:20] + "@inkwell.invalid"
    return out


def _rewrite_cids(html: str, cids: dict[str, str]) -> str:
    for old, new in cids.items():
        html = html.replace(f"cid:{old}", f"cid:{new}")
    return html


_KEEP_HEADERS = {
    "date",
    "subject",
    "mime-version",
    "content-type",
    "content-transfer-encoding",
    "content-disposition",
    "content-id",
}
_ADDR_HEADERS = ("from", "to", "cc", "bcc", "reply-to", "sender")


def _part_text(part: Message) -> str:
    try:
        return str(part.get_content())  # type: ignore[attr-defined]
    except (LookupError, KeyError, UnicodeError, AssertionError):
        payload = part.get_payload(decode=True)
        return _decode(payload) if isinstance(payload, bytes) else ""


def _is_attachment(part: Message) -> bool:
    disp = (part.get("Content-Disposition") or "").lower()
    return (
        part.get_filename() is not None
        or disp.startswith("attachment")
        or (part.get_content_maintype() not in ("text", "multipart", "message"))
    )


def handle_eml(data: bytes, ctx: Ctx) -> Outcome:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(data)  # type: ignore[arg-type,assignment]
    findings = Findings()
    notes: list[str] = []
    keys: list[tuple[str, str]] = []

    # Names in address headers seed an ad-hoc deny list for this message.
    names: list[str] = []
    all_texts: list[str] = []
    for part in msg.walk():
        for h in _ADDR_HEADERS:
            for v in part.get_all(h, []) or []:
                for disp, _addr in getaddresses([str(v)]):
                    if disp:
                        names.append(disp)
        if part.get("Subject"):
            all_texts.append(str(part.get("Subject")))
        if part.get_content_maintype() == "text" and not _is_attachment(part):
            all_texts.append(_part_text(part))
    ctx = dataclasses.replace(ctx, extra_names=(*ctx.extra_names, *names))
    _check_withhold("\n".join(all_texts), ctx)
    if not ctx.redact:
        return _eml_copy(data, msg, ctx)

    att_n = 0
    stem = PurePosixPath(ctx.pseudo_key).stem
    cids = _cid_map(msg)
    for part in list(msg.walk()):
        is_att = _is_attachment(part)
        filename = part.get_filename()
        _scrub_headers(part, ctx, findings)
        if part.get("Content-ID"):
            old_cid = str(part["Content-ID"]).strip().strip("<>")
            del part["Content-ID"]
            part["Content-ID"] = f"<{cids.get(old_cid, 'x@inkwell.invalid')}>"
        if part.is_multipart() or part.get_content_maintype() == "message":
            continue
        if is_att:
            att_n += 1
            orig_name = filename or f"part{att_n}"
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                payload = b""
            sub = process(orig_name, payload, ctx.child(orig_name, f"att{att_n}"))
            findings.merge(sub.findings)
            keys += sub.keys
            notes += [f"att{att_n}:{n}" for n in sub.notes]
            new_name = f"{stem}_att{att_n:02d}{sub.ext or '.bin'}"
            keys.append((f"{ctx.orig_key}!{orig_name}", f"{ctx.pseudo_key}!{new_name}"))
            _replace_attachment(part, sub, new_name, notes, att_n)
        elif part.get_content_maintype() == "text":
            sub_t = part.get_content_subtype()
            txt = _part_text(part)
            if sub_t == "html":
                new = _rewrite_cids(scrub_html_string(txt, ctx, findings, classify_too=False), cids)
            else:
                new = ctx.scrubber.redact_text(txt, findings, ctx.extra_names, ctx.owner_ids)
            _set_text(part, new, sub_t if sub_t in ("html", "plain") else "plain")
    blob = msg.as_bytes(policy=policy.default.clone(linesep="\n"))
    return Outcome("redacted", "eml", blob, ".eml", findings=findings, notes=notes, keys=keys)


def _scrub_headers(part: Message, ctx: Ctx, findings: Findings) -> None:
    subject = str(part["Subject"]) if part.get("Subject") is not None else None
    addr_values = {h: [str(v) for v in part.get_all(h, []) or []] for h in _ADDR_HEADERS}
    for k in set(part.keys()):
        if k.lower() not in _KEEP_HEADERS:
            del part[k]
    if subject is not None:
        del part["Subject"]
        part["Subject"] = ctx.scrubber.redact_text(subject, findings, ctx.extra_names, ctx.owner_ids)
    for h, values in addr_values.items():
        if values:
            part[h.title()] = _scrub_addresses(values, ctx, findings)
    # filename= / name= parameters frequently contain people's names
    if part.get_param("name") is not None:
        part.set_param("name", "redacted")
    if part.get_param("filename", header="Content-Disposition") is not None:
        part.set_param("filename", "redacted", header="Content-Disposition")


def _scrub_addresses(values: list[str], ctx: Ctx, findings: Findings) -> str:
    """Every address becomes REDACTED <redacted@invalid>, except that a permitted person keeps
    their display name (and, if contacts are allowed, their listed address)."""
    from email.utils import formataddr

    permitted, known = ctx.scrubber.permitted, ctx.scrubber.known
    out = []
    for disp, addr in getaddresses(values) or [("", "")]:
        keep_name = bool(disp) and permitted.is_name(disp) and not known.is_registry_full_name(disp)
        keep_addr = keep_name and permitted.is_contact("EMAIL_ADDRESS", addr)
        findings.counts["PERMITTED_NAME" if keep_name else "EMAIL_ADDRESS"] += 1
        out.append(formataddr((disp if keep_name else "REDACTED", addr if keep_addr else "redacted@invalid")))
    return ", ".join(out)


def _set_text(part: Message, text: str, subtype: str) -> None:
    for h in ("Content-Transfer-Encoding",):
        if h in part:
            del part[h]
    part.set_payload(text.encode("utf-8"))
    part.set_type(f"text/{subtype}")
    part.set_param("charset", "utf-8")
    from email import encoders

    encoders.encode_base64(part)


def _replace_attachment(part: Message, sub: Outcome, new_name: str, notes: list[str], n: int) -> None:
    from email import encoders

    if "Content-Transfer-Encoding" in part:
        del part["Content-Transfer-Encoding"]
    if sub.status in ("redacted", "withheld") and sub.data is not None:
        if sub.status == "withheld":
            part.set_type("text/plain")
        part.set_payload(sub.data)
    else:
        notes.append(f"att{n}:removed_{sub.status}")
        part.set_type("text/plain")
        part.set_payload(f"[attachment removed by inkwell: {sub.status}]\n".encode())
    if "Content-Disposition" in part:
        del part["Content-Disposition"]
    part["Content-Disposition"] = "attachment"
    part.set_param("filename", new_name, header="Content-Disposition")
    if part.get_param("name") is not None:
        part.set_param("name", new_name)
    encoders.encode_base64(part)


# ---------------------------------------------------------------------------
# DOCX (operates on the raw WordprocessingML so tables, text boxes, headers, footers,
# footnotes, comments and tracked deletions are all covered)
# ---------------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_TEXT_PARTS = re.compile(r"^/word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$")


def handle_docx(data: bytes, ctx: Ctx) -> Outcome:
    doc = Document(io.BytesIO(data))
    findings = Findings()
    notes: list[str] = []
    xml_parts = [p for p in doc.part.package.iter_parts() if _DOCX_TEXT_PARTS.match(str(p.partname))]

    paras: list[tuple[object, list[etree._Element]]] = []
    for p in xml_parts:
        root = p.element if hasattr(p, "element") else etree.fromstring(p.blob)  # noqa: S320
        for para in root.iter(f"{W}p"):
            ts = [t for t in para.iter(f"{W}t", f"{W}delText", f"{W}instrText")]
            if ts:
                paras.append((p, ts))
        for el in root.iter():
            for a in (f"{W}author", f"{W}initials"):
                if a in el.attrib:
                    el.attrib[a] = "REDACTED"
    _check_withhold("\n".join("".join(t.text or "" for t in ts) for _, ts in paras), ctx)
    if not ctx.redact:
        return _copied("docx", data, ".docx")

    segs: list[str] = []
    seps: list[str] = []
    elems: list[etree._Element] = []
    for _, ts in paras:
        for i, t in enumerate(ts):
            segs.append(t.text or "")
            seps.append("\n" if i == len(ts) - 1 else "")
            elems.append(t)
    new = redact_segments(ctx.scrubber, segs, seps, findings, ctx.extra_names, ctx.owner_ids)
    for el, s in zip(elems, new, strict=True):
        el.text = s
        el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    for part in doc.part.package.iter_parts():
        if str(part.partname) in ("/docProps/app.xml", "/docProps/custom.xml"):
            part._blob = _scrub_props_xml(part.blob)  # noqa: SLF001 - no public setter
            findings.counts["DOC_PROPERTIES"] += 1

    cp = doc.core_properties
    for attr in (
        "author",
        "last_modified_by",
        "title",
        "subject",
        "keywords",
        "comments",
        "category",
        "identifier",
        "content_status",
    ):
        setattr(cp, attr, "")

    for part in doc.part.package.iter_parts():
        ct = getattr(part, "content_type", "")
        if ct.startswith("image/") and image_format(part.blob[:16]):
            sub = handle_image(part.blob, ctx)
            if sub.data:
                part._blob = sub.data  # noqa: SLF001 - python-docx has no public setter
                findings.merge(sub.findings)
        elif ct.startswith("image/"):
            notes.append("unscanned_image_format")
    buf = io.BytesIO()
    doc.save(buf)
    return Outcome("redacted", "docx", buf.getvalue(), ".docx", findings=findings, notes=notes)


_EXT_PROPS_DROP = {"TitlesOfParts", "HeadingPairs", "Company", "Manager", "HyperlinkBase", "HLinks"}


def _scrub_props_xml(blob: bytes) -> bytes:
    """docProps/app.xml keeps the document title (TitlesOfParts), company and manager;
    docProps/custom.xml keeps arbitrary user-defined fields. Drop all of them."""
    root = etree.fromstring(blob)  # noqa: S320 - OOXML part from a zip we already parsed
    for el in list(root):
        local = etree.QName(el).localname
        if root.tag.endswith("}Properties") and "custom-properties" in (root.tag or ""):
            root.remove(el)  # custom.xml: every user-defined property
        elif local in _EXT_PROPS_DROP:
            root.remove(el)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

_COLUMN_LABEL = re.compile(
    r"(?i)\b(?:ssn|social\s+security|student\s*(?:id|number|no)|emplid|id\s*(?:no|number|#)|"
    r"birth|dob|maiden|name|address|street|city|zip|postal|phone|e-?mail|parent|guardian|spouse|"
    r"citizenship|country|gpa|grade|gender|sex|ethnicity|race|visa|passport|sevis)\b"
)


def _is_pii_header(value: str, ctx: Ctx) -> bool:
    if _COLUMN_LABEL.search(value):
        return True
    low = value.lower()
    return any(re.search(r"\b" + re.escape(lab.lower()) + r"\b", low) for lab in ctx.cfg.extra_id_labels)


def handle_xlsx(data: bytes, ctx: Ctx) -> Outcome:
    wb = load_workbook(io.BytesIO(data), data_only=False)
    findings = Findings()
    texts: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            texts.append("\t".join("" if c.value is None else str(c.value) for c in row))
    _check_withhold("\n".join(texts), ctx)
    if not ctx.redact:
        return _copied("xlsx", data, ".xlsx")

    for i, ws in enumerate(wb.worksheets, 1):
        # columns whose header labels identify personal data are blanked wholesale
        pii_cols: set[int] = set()
        header_row: int | None = None
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 10)):
            hits = {c.column for c in row if isinstance(c.value, str) and _is_pii_header(c.value, ctx)}
            if hits:
                pii_cols, header_row = hits, row[0].row
                break
        cells = [c for row in ws.iter_rows() for c in row]
        segs, seps, targets = [], [], []
        last_row = None
        for c in cells:
            if c.value is None:
                continue
            if header_row is not None and c.row > header_row and c.column in pii_cols:
                c.value = "<REDACTED>"
                findings.counts["SPREADSHEET_COLUMN"] += 1
                continue
            if last_row is not None and seps:
                seps[-1] = "\t" if c.row == last_row else "\n"
            segs.append(str(c.value))
            seps.append("\n")
            targets.append(c)
            last_row = c.row
        new = redact_segments(ctx.scrubber, segs, seps, findings, ctx.extra_names, ctx.owner_ids)
        for c, old, s in zip(targets, segs, new, strict=True):
            if s != old:
                c.value = s
        for c in cells:
            if c.comment is not None:
                c.comment = None
                findings.counts["COMMENT_REMOVED"] += 1
        title_new = ctx.scrubber.redact_text(ws.title, findings, ctx.extra_names, ctx.owner_ids)
        if title_new != ws.title:
            ws.title = f"Sheet{i}"
        ws.oddHeader.center.text = None
        ws.oddFooter.center.text = None
    props = wb.properties
    props.creator = None
    props.lastModifiedBy = None
    props.title = None
    props.subject = None
    props.keywords = None
    props.description = None
    buf = io.BytesIO()
    wb.save(buf)
    return Outcome("redacted", "xlsx", buf.getvalue(), ".xlsx", findings=findings)


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------


def handle_zip(data: bytes, ctx: Ctx) -> Outcome:
    findings = Findings()
    notes: list[str] = []
    keys: list[tuple[str, str]] = []
    zin = zipfile.ZipFile(io.BytesIO(data))
    infos = [i for i in zin.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
    total = sum(i.file_size for i in infos)
    if total > ctx.cfg.max_zip_bytes:
        return Outcome("unsupported", "zip", notes=["zip_too_large"])
    if not ctx.redact:
        return _zip_copy(zin, infos, ctx)
    stem = PurePosixPath(ctx.pseudo_key).stem
    dirs: dict[str, str] = {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, info in enumerate(infos, 1):
            parent = str(PurePosixPath(info.filename).parent)
            pdir = ""
            if parent not in (".", ""):
                pdir = dirs.setdefault(parent, f"DIR_{len(dirs) + 1:03d}") + "/"
            if info.flag_bits & 0x1:
                notes.append(f"m{n}:encrypted_skipped")
                continue
            raw = zin.read(info)
            sub = process(info.filename, raw, ctx.child(info.filename, f"m{n}"))
            findings.merge(sub.findings)
            keys += sub.keys
            notes += [f"m{n}:{x}" for x in sub.notes]
            if sub.status not in ("redacted", "withheld") or sub.data is None:
                notes.append(f"m{n}:{sub.status}")
                continue
            new_name = f"{pdir}{stem}_m{n:03d}{sub.ext}"
            keys.append((f"{ctx.orig_key}!{info.filename}", f"{ctx.pseudo_key}!{new_name}"))
            zi = zipfile.ZipInfo(new_name, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zi, sub.data)
    return Outcome("redacted", "zip", buf.getvalue(), ".zip", findings=findings, notes=notes, keys=keys)


# ---------------------------------------------------------------------------
# redaction disabled: containers keep everything except withheld members
# ---------------------------------------------------------------------------


def _eml_copy(data: bytes, msg: EmailMessage, ctx: Ctx) -> Outcome:
    """Headers and bodies untouched; attachments that are withheld (or that can't be
    classified, so withholding can't be ruled out) are replaced by a placeholder."""
    notes = ["unredacted"]
    changed = False
    n = 0
    for part in list(msg.walk()):
        if part.is_multipart() or part.get_content_maintype() == "message" or not _is_attachment(part):
            continue
        n += 1
        name = part.get_filename() or f"part{n}"
        payload = part.get_payload(decode=True)
        sub = process(name, payload if isinstance(payload, bytes) else b"", ctx.child(name, f"att{n}"))
        notes += [f"att{n}:{x}" for x in sub.notes if x != "unredacted"]
        if sub.status == "copied" and sub.data == payload:
            continue
        changed = True
        from email import encoders

        if "Content-Transfer-Encoding" in part:
            del part["Content-Transfer-Encoding"]
        if sub.status in ("withheld", "copied") and sub.data is not None:
            if sub.status == "withheld":
                part.set_type("text/plain")
                notes.append(f"att{n}:withheld")
            part.set_payload(sub.data)
        else:
            part.set_type("text/plain")
            part.set_payload(f"[attachment removed by inkwell: {sub.status}]\n".encode())
            notes.append(f"att{n}:removed_{sub.status}")
        encoders.encode_base64(part)
    if not changed:
        return Outcome("copied", "eml", data, ".eml", notes=notes)
    return Outcome(
        "copied", "eml", msg.as_bytes(policy=policy.default.clone(linesep="\n")), ".eml", notes=notes
    )


def _zip_copy(zin: zipfile.ZipFile, infos: list[zipfile.ZipInfo], ctx: Ctx) -> Outcome:
    notes = ["unredacted"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, info in enumerate(infos, 1):
            if info.flag_bits & 0x1:
                notes.append(f"m{n}:encrypted_skipped")
                continue
            sub = process(info.filename, zin.read(info), ctx.child(info.filename, f"m{n}"))
            notes += [f"m{n}:{x}" for x in sub.notes if x != "unredacted"]
            if sub.status not in ("copied", "withheld") or sub.data is None:
                notes.append(f"m{n}:{sub.status}")  # unclassifiable: left out (fail closed)
                continue
            name = info.filename + (".WITHHELD.txt" if sub.status == "withheld" else "")
            zi = zipfile.ZipInfo(name, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zi, sub.data)
    return Outcome("copied", "zip", buf.getvalue(), ".zip", notes=notes)
