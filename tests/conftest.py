from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

import fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

from inkwell.config import Config
from inkwell.engine import Scrubber
from inkwell.handlers import Ctx

# Synthetic identities only. Never put real records in tests.
STUDENT = "Quintavius Fernhollow"
STUDENT_FOLDER = "Fernhollow, Quintavius"
SSN = "123-45-6789"
SID = "S-0000000AJ"  # registry ID of the synthetic person (format-valid, see ids.py)
OTHER_SID = "S-1111111BQ"


@pytest.fixture(scope="session")
def scrubber() -> Scrubber:
    from inkwell.config import DEFAULT_ENTITIES

    return Scrubber(
        spacy_model="en_core_web_lg",
        entities=DEFAULT_ENTITIES,
        score_threshold=0.35,
        students=[(SID, [STUDENT_FOLDER]), (OTHER_SID, ["Fernhollow, Ottoline"])],
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    inp = tmp_path / "in"
    inp.mkdir()
    return Config(input_dir=inp, output_dir=tmp_path / "out", key_file=tmp_path / "key.csv", dpi=200)


@pytest.fixture
def ctx(cfg: Config, scrubber: Scrubber) -> Ctx:
    return Ctx(cfg, scrubber, "orig/file", "Cohort 1990-91/STUDENT_0001/DOC_000001", owner_ids=(SID,))


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def text_image(lines: list[str], size: tuple[int, int] = (1700, 1100), font_px: int = 40) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    y = 60
    for line in lines:
        d.text((60, y), line, fill="black", font=_font(font_px))
        y += int(font_px * 1.8)
    return img


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def scanned_pdf(lines: list[str]) -> bytes:
    """A PDF whose only content is a raster image (like a decades-old scan)."""
    img = text_image(lines, size=(1700, 2200))
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=png_bytes(img))
    return doc.tobytes()


def text_pdf(lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 72.0
    for line in lines:
        page.insert_text(fitz.Point(72, y), line, fontsize=12)
        y += 20
    doc.set_metadata({"author": STUDENT, "title": f"File of {STUDENT}"})
    return doc.tobytes()


def ocr_text(pdf_or_png: bytes) -> str:
    import pytesseract

    if pdf_or_png.startswith(b"%PDF"):
        out = []
        with fitz.open(stream=pdf_or_png, filetype="pdf") as d:
            for p in d:
                pix = p.get_pixmap(dpi=200)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                out.append(pytesseract.image_to_string(img))
        return "\n".join(out)
    return pytesseract.image_to_string(Image.open(io.BytesIO(pdf_or_png)))


@pytest.fixture
def make_ocr() -> Callable[[bytes], str]:
    return ocr_text
