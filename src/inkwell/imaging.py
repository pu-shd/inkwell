"""OCR, orientation, face detection and box drawing for raster content."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageDraw

from .engine import Word

Box = tuple[float, float, float, float]

MIN_OCR_DIM = 32
OCR_UPSCALE_BELOW = 1400  # px; small screenshots OCR far better when upscaled
PAD = 3


def ocr_words(img: Image.Image, lang: str) -> tuple[list[Word], float]:
    """Return words in *img* pixel coordinates and the mean confidence (0-100, -1 if none)."""
    if min(img.size) < MIN_OCR_DIM:
        return [], -1.0
    scale = 1.0
    src = img
    if max(img.size) < OCR_UPSCALE_BELOW:
        scale = 2.0
        src = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
    data = pytesseract.image_to_data(
        src.convert("L"), lang=lang, config="--psm 3", output_type=pytesseract.Output.DICT
    )
    words: list[Word] = []
    confs: list[float] = []
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        if not txt:
            continue
        conf = float(data["conf"][i])
        if conf >= 0:
            confs.append(conf)
        x, y, w, h = (data[k][i] / scale for k in ("left", "top", "width", "height"))
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        words.append(Word(txt, x, y, x + w, y + h, key))
    mean = sum(confs) / len(confs) if confs else -1.0
    return words, mean


_ROTATE = re.compile(r"Rotate:\s*(\d+)")
_CONF = re.compile(r"Orientation confidence:\s*([\d.]+)")


def upright(img: Image.Image) -> tuple[Image.Image, int]:
    """Detect page orientation with Tesseract OSD and rotate to upright."""
    if min(img.size) < 200:
        return img, 0
    try:
        osd = pytesseract.image_to_osd(img.convert("L"), config="--psm 0")
    except pytesseract.TesseractError:
        return img, 0  # too little text to decide
    rot = _ROTATE.search(osd)
    conf = _CONF.search(osd)
    if not rot or not conf:
        return img, 0
    angle = int(rot.group(1))
    if angle in (90, 180, 270) and float(conf.group(1)) >= 2.0:
        # Tesseract's "Rotate" is clockwise degrees to correct; PIL rotates counter-clockwise.
        return img.rotate(-angle, expand=True), angle
    return img, 0


@lru_cache(maxsize=1)
def _face_cascades() -> tuple[cv2.CascadeClassifier, cv2.CascadeClassifier]:
    base = cv2.data.haarcascades  # type: ignore[attr-defined]
    return (
        cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml"),
        cv2.CascadeClassifier(base + "haarcascade_profileface.xml"),
    )


def detect_faces(img: Image.Image) -> list[Box]:
    """Faces are biometric identifiers; black them out."""
    if min(img.size) < 48:
        return []
    gray = np.asarray(img.convert("L"))
    scale = 1.0
    if max(gray.shape) > 2000:
        scale = 2000 / max(gray.shape)
        gray = cv2.resize(gray, None, fx=scale, fy=scale)
    boxes: list[Box] = []
    for casc in _face_cascades():
        found = casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(24, 24))
        for x, y, w, h in found if len(found) else []:
            pad_w, pad_h = 0.25 * w, 0.35 * h
            boxes.append(
                (
                    (x - pad_w) / scale,
                    (y - pad_h) / scale,
                    (x + w + pad_w) / scale,
                    (y + h + pad_h) / scale,
                )
            )
    return boxes


def word_boxes(words: Sequence[Word], idxs: Iterable[int]) -> list[Box]:
    return [(words[i].x0, words[i].y0, words[i].x1, words[i].y1) for i in idxs]


def black_out(img: Image.Image, boxes: Iterable[Box]) -> Image.Image:
    out = img.convert("RGB") if img.mode not in ("RGB", "L") else img.copy()
    draw = ImageDraw.Draw(out)
    fill: int | tuple[int, int, int] = 0 if out.mode == "L" else (0, 0, 0)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle(
            (max(0, x0 - PAD), max(0, y0 - PAD), min(out.width, x1 + PAD), min(out.height, y1 + PAD)),
            fill=fill,
        )
    return out


def overlaps_any(w: Word, boxes: Sequence[Box]) -> bool:
    return any(w.x0 < b[2] and w.x1 > b[0] and w.y0 < b[3] and w.y1 > b[1] for b in boxes)


def draw_labels(img: Image.Image, labels: Sequence[Word]) -> None:
    """Print registry-ID pseudonyms in white on their (already black) boxes."""
    if not labels:
        return
    from PIL import ImageFont

    draw = ImageDraw.Draw(img)
    white: int | tuple[int, int, int] = 255 if img.mode == "L" else (255, 255, 255)
    black: int | tuple[int, int, int] = 0 if img.mode == "L" else (0, 0, 0)
    for w in labels:
        h = max(8, int((w.y1 - w.y0) * 0.8))
        font = ImageFont.load_default(size=h)
        x0, y0, x1, y1 = draw.textbbox((w.x0, w.y0), w.text, font=font)
        draw.rectangle((x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD), fill=black)  # label may outgrow box
        draw.text((w.x0, w.y0), w.text, fill=white, font=font)
