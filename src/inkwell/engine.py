"""Presidio analyzer/anonymizer wrapper shared by all handlers."""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from presidio_analyzer import AnalyzerEngine, EntityRecognizer, RecognizerRegistry, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from .recognizers import KnownNameRecognizer, custom_recognizers

CHUNK = 50_000
OVERLAP = 500


@dataclass
class Word:
    """A positioned word (pixel coordinates in the page/image it came from)."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line_key: tuple[int, ...]


@dataclass
class Findings:
    counts: Counter[str] = field(default_factory=Counter)
    mentions: set[str] = field(default_factory=set)  # registry IDs of people named in the document

    def add(self, results: Iterable[RecognizerResult]) -> None:
        for r in results:
            self.counts[r.entity_type] += 1

    def merge(self, other: Findings) -> None:
        self.counts.update(other.counts)
        self.mentions |= other.mentions


StudentTable = Iterable[tuple[str, Iterable[str]]]


class Scrubber:
    def __init__(
        self,
        *,
        spacy_model: str,
        entities: Sequence[str],
        score_threshold: float,
        students: StudentTable = (),
        extra_id_labels: Sequence[str] = (),
    ) -> None:
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": spacy_model}],
            }
        )
        nlp_engine = provider.create_engine()
        registry = RecognizerRegistry(supported_languages=["en"])
        registry.load_predefined_recognizers(nlp_engine=nlp_engine, languages=["en"])
        for rec in custom_recognizers(extra_id_labels):
            registry.add_recognizer(rec)
        self.known = KnownNameRecognizer(students)
        if not self.known.is_empty:
            registry.add_recognizer(self.known)
        self.analyzer = AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=["en"])
        self.anonymizer = AnonymizerEngine()
        self.entities = list(entities)
        self.threshold = score_threshold

    # -- analysis -----------------------------------------------------------

    def analyze(self, text: str, extra_names: Iterable[str] = ()) -> list[RecognizerResult]:
        if not text.strip():
            return []
        adhoc: list[EntityRecognizer] = []
        extra = [n for n in extra_names if n and n.strip()]
        if extra:
            # Names from e-mail headers: people, but not necessarily students.
            rec = KnownNameRecognizer.from_names(extra, name="AdHocNames", entity="PERSON")
            if not rec.is_empty:
                adhoc.append(rec)
        results: list[RecognizerResult] = []
        for off, chunk in _chunks(text):
            for r in self.analyzer.analyze(
                text=chunk,
                language="en",
                entities=self.entities,
                score_threshold=self.threshold,
                ad_hoc_recognizers=adhoc or None,
            ):
                r.start += off
                r.end += off
                results.append(r)
        return _dedupe(results)

    def label(self, entity: str, matched: str, context_ids: Iterable[str], findings: Findings) -> str:
        """Replacement text. Known record subjects become their registry ID, so mentions of the same
        person link across documents; everything else becomes its entity type."""
        if entity == "KNOWN_STUDENT_NAME":
            sid = self.known.resolve(matched, context_ids)
            if sid:
                findings.mentions.add(sid)
                return f"<{sid}>"
        return f"<{entity}>"

    # -- text ---------------------------------------------------------------

    def redact_text(
        self,
        text: str,
        findings: Findings,
        extra_names: Iterable[str] = (),
        context_ids: Iterable[str] = (),
    ) -> str:
        results = self.analyze(text, extra_names)
        if not results:
            return text
        findings.add(results)
        ctx = tuple(context_ids)
        resolved = resolve_overlaps(results)
        out = self.anonymizer.anonymize(
            text=text,
            analyzer_results=resolved,  # type: ignore[arg-type]
            operators={
                "DEFAULT": OperatorConfig("replace"),
                "KNOWN_STUDENT_NAME": OperatorConfig(
                    "custom", {"lambda": lambda m: self.label("KNOWN_STUDENT_NAME", m, ctx, findings)}
                ),
            },
        )
        return out.text

    # -- positioned words (PDF pages, images) --------------------------------

    def redact_words(
        self,
        words: Sequence[Word],
        findings: Findings,
        extra_names: Iterable[str] = (),
        context_ids: Iterable[str] = (),
    ) -> list[int]:
        return self.redact_words_labeled(words, findings, extra_names, context_ids)[0]

    def redact_words_labeled(
        self,
        words: Sequence[Word],
        findings: Findings,
        extra_names: Iterable[str] = (),
        context_ids: Iterable[str] = (),
    ) -> tuple[list[int], dict[int, str]]:
        """Indexes of words to black out, plus registry-ID labels keyed by the first word of
        each resolved known-name match (printed on the box)."""
        text, spans = build_text(words)
        results = self.analyze(text, extra_names)
        findings.add(results)
        hit: set[int] = set()
        labels: dict[int, str] = {}
        ctx = tuple(context_ids)
        for r in resolve_overlaps(results):
            covered = [i for i, (s, e) in enumerate(spans) if s < r.end and e > r.start]
            hit.update(covered)
            if covered and r.entity_type == "KNOWN_STUDENT_NAME":
                lab = self.label(r.entity_type, text[r.start : r.end], ctx, findings)
                if lab != "<KNOWN_STUDENT_NAME>":
                    labels[covered[0]] = lab.strip("<>")
        return sorted(hit), labels


def layer_text(text: str) -> str:
    """The exact form a word takes in a PDF text layer (NFKC, Latin-1 only).

    Analysis runs on this form so that what the leak check extracts from the layer is what
    the analyzer saw; otherwise e.g. a ligature turns into a separator and exposes a token."""
    return unicodedata.normalize("NFKC", text).encode("latin-1", "ignore").decode("latin-1")


def build_text(words: Sequence[Word]) -> tuple[str, list[tuple[int, int]]]:
    """Join words into lines; return text and per-word char spans."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    prev_line: tuple[int, ...] | None = None
    for w in words:
        if prev_line is not None:
            sep = " " if w.line_key == prev_line else "\n"
            parts.append(sep)
            pos += 1
        t = layer_text(w.text)
        parts.append(t)
        spans.append((pos, pos + len(t)))
        pos += len(t)
        prev_line = w.line_key
    return "".join(parts), spans


def _chunks(text: str) -> Iterable[tuple[int, str]]:
    if len(text) <= CHUNK:
        yield 0, text
        return
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK)
        if end < len(text):
            nl = text.rfind("\n", start + CHUNK // 2, end)
            if nl != -1:
                end = nl
        yield start, text[start : end + OVERLAP if end < len(text) else end]
        start = end


def _dedupe(results: list[RecognizerResult]) -> list[RecognizerResult]:
    seen: set[tuple[str, int, int]] = set()
    out = []
    for r in results:
        key = (r.entity_type, r.start, r.end)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _pick_label(a: RecognizerResult, b: RecognizerResult) -> str:
    # A known subject wins so the union can still be resolved to a registry ID.
    for r in (a, b):
        if r.entity_type == "KNOWN_STUDENT_NAME":
            return r.entity_type
    return a.entity_type if a.score >= b.score else b.entity_type


def resolve_overlaps(results: list[RecognizerResult]) -> list[RecognizerResult]:
    """Merge overlapping spans into their union so nothing is left partially visible."""
    rs = sorted(results, key=lambda r: (r.start, -r.end))
    merged: list[RecognizerResult] = []
    for r in rs:
        if merged and r.start <= merged[-1].end:
            m = merged[-1]
            label = _pick_label(m, r)
            merged[-1] = RecognizerResult(label, m.start, max(m.end, r.end), max(m.score, r.score))
        else:
            merged.append(RecognizerResult(r.entity_type, r.start, r.end, r.score))
    return merged
