"""Custom Presidio recognizers for identifiers in sensitive records.

Design bias: over-redaction is acceptable, under-redaction is not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from presidio_analyzer import EntityRecognizer, Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

# ---------------------------------------------------------------------------
# Plain pattern recognizers
# ---------------------------------------------------------------------------


def ssn_like_recognizer() -> PatternRecognizer:
    """Any ###-##-#### shaped number, valid or not (old records used invalid/test SSNs)."""
    return PatternRecognizer(
        supported_entity="SSN_LIKE",
        name="SsnLikeRecognizer",
        patterns=[
            Pattern("ssn_sep", r"(?<!\d)\d{3}[\s.\-–]\d{2}[\s.\-–]\d{4}(?!\d)", 0.85),
        ],
        global_regex_flags=re.MULTILINE,
    )


def student_id_recognizer(extra_context: Iterable[str] = ()) -> PatternRecognizer:
    """Bare 9-digit numbers (common institutional ID length; also unformatted SSNs), boosted by
    context words, including any institution-specific ID labels."""
    return PatternRecognizer(
        supported_entity="STUDENT_ID",
        name="StudentIdRecognizer",
        patterns=[
            Pattern("nine_digits", r"(?<![\d\-])\d{9}(?![\d\-])", 0.5),
            Pattern("id_with_hash", r"(?i)(?<=\bID\s?#\s?)\d{5,10}\b", 0.8),
        ],
        context=["student", "id", "emplid", "identification", "number", "ssn", *extra_context],
        global_regex_flags=re.MULTILINE,
    )


_STREET_SUFFIX = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|Court|Ct|Place|Pl|"
    r"Way|Terrace|Ter|Circle|Cir|Parkway|Pkwy|Highway|Hwy|Square|Sq|Trail|Trl|Pike|Hall|"
    r"Apartments|Apts)"
)


def street_address_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="STREET_ADDRESS",
        name="StreetAddressRecognizer",
        patterns=[
            Pattern(
                "street",
                r"\b\d{1,6}[A-Za-z]?\s+(?:[NSEW]\.?\s+)?(?:[A-Z0-9][\w'.\-]*\s+){0,4}"
                + _STREET_SUFFIX
                + r"\b\.?(?:,?\s+(?:Apt|Apartment|Unit|Suite|Ste|Rm|Room|#)\.?\s*[\w\-]+)?",
                0.7,
            ),
            Pattern("po_box", r"(?i)\bP\.?\s?O\.?\s+Box\s+\d+\b", 0.8),
            Pattern("state_zip", r"\b[A-Z]{2}\.?,?\s+\d{5}(?:-\d{4})?\b", 0.7),
            Pattern("zip_plus4", r"\b\d{5}-\d{4}\b", 0.6),
        ],
        global_regex_flags=re.MULTILINE,
    )


def gov_id_recognizer() -> PatternRecognizer:
    """SEVIS IDs (N + 10 digits) and USCIS A-numbers."""
    return PatternRecognizer(
        supported_entity="GOV_ID",
        name="GovIdRecognizer",
        patterns=[
            Pattern("sevis", r"\bN\d{10}\b", 0.85),
            Pattern("a_number", r"\bA[\- ]?\d{8,9}\b", 0.6),
        ],
        global_regex_flags=re.MULTILINE,
    )


# ---------------------------------------------------------------------------
# Labeled form fields: "Date of Birth: 01/02/1970", "Mother's maiden name: X"
# ---------------------------------------------------------------------------

# (entity, label regex, also_take_next_line)
_LABELS: list[tuple[str, str, bool]] = [
    (
        "US_SSN",
        r"social\s+security(?:\s+(?:number|no\.?|#))?|soc\.?\s*sec\.?(?:\s*no\.?)?|"
        r"\bS\.?S\.?N\.?\b|\bS\.?S\.?#",
        False,
    ),
    (
        "STUDENT_ID",
        r"student\s+(?:id(?:entification)?|number|no\.?|#)|\bEMPLID\b|"
        r"univ(?:ersity)?\.?\s+id|\bID\s+(?:number|no\.?)",
        False,
    ),
    (
        "DATE_OF_BIRTH",
        r"date\s+of\s+birth|birth\s*date|\bD\.?O\.?B\.?\b|\bborn\s+(?:on\s+)?(?=\w+\s+\d|\d)",
        False,
    ),
    (
        "PLACE_OF_BIRTH",
        r"place\s+of\s+birth|birth\s*place|(?:city|country|state)\s+of\s+birth|"
        r"\bborn\s+in\b",
        False,
    ),
    ("MAIDEN_NAME", r"mother'?s?\s+maiden\s+name|maiden\s+name|\bn[ée]e\b", False),
    (
        "PERSON",
        r"(?:student|applicant|candidate|full|legal|first|last|middle|given|family|sur|"
        r"preferred|nick)\s*name|father'?s?\s+name|mother'?s?\s+name|"
        r"parent(?:s|'s|/guardian|\s+or\s+guardian)?(?:'s)?\s+name|guardian(?:'s)?(?:\s+name)?|"
        r"spouse(?:'s)?(?:\s+name)?|emergency\s+contact|next\s+of\s+kin|"
        r"(?:^|(?<=\n))\s*name(?=\s*:)",
        False,
    ),
    (
        "STREET_ADDRESS",
        r"(?:home|permanent|mailing|current|local|street|campus|parents?'?s?|"
        r"forwarding|residential|billing)\s+address|(?:^|(?<=\n))\s*address(?=\s*:)|"
        r"(?:^|(?<=\n))\s*residence(?=\s*:)",
        True,
    ),
]

_SEP = re.compile(r"[\s:#.\-–=_|]*")
_MAX_VALUE = 80


class LabeledFieldRecognizer(EntityRecognizer):
    """Redacts the value that follows a form label on the same line (and optionally next line).

    If the label ends its line (common in OCR'd forms where the value sits below), the next
    non-empty line is taken instead.
    """

    def __init__(self, extra_id_labels: Iterable[str] = ()) -> None:
        labels = list(_LABELS)
        extra = [r"\b" + re.escape(lab).replace(r"\ ", r"\s*") + r"\b" for lab in extra_id_labels]
        if extra:
            labels.append(("STUDENT_ID", "|".join(extra), False))
        self._labels = [(ent, re.compile(rx, re.IGNORECASE | re.MULTILINE), nxt) for ent, rx, nxt in labels]
        super().__init__(
            supported_entities=sorted({e for e, _, _ in _LABELS}),
            name="LabeledFieldRecognizer",
        )

    def load(self) -> None:  # pragma: no cover - nothing to load
        return None

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None
    ) -> list[RecognizerResult]:
        results: list[RecognizerResult] = []
        for ent, rx, take_next in self._labels:
            if ent not in entities:
                continue
            shape = _VALUE_SHAPE.get(ent)
            for m in rx.finditer(text):
                spans = (
                    _shaped_spans(text, m.end(), shape) if shape else _value_spans(text, m.end(), take_next)
                )
                for start, end in spans:
                    results.append(RecognizerResult(ent, start, end, 0.9))
        return results


# Number-like identifiers: only the number after the label is redacted, not the rest of the line.
_VALUE_SHAPE = {
    "US_SSN": re.compile(r"[\dXx*][\dXx*\s.\-–]{5,13}[\dXx*]"),
    "STUDENT_ID": re.compile(r"[A-Za-z]{0,2}\d[\d\s\-]{2,14}\d"),
}
_SHAPE_WINDOW = 40


def _shaped_spans(text: str, pos: int, shape: re.Pattern[str]) -> Iterable[tuple[int, int]]:
    """Find an ID-shaped value just after the label (same line, else the next line)."""
    window = text[pos : pos + _SHAPE_WINDOW + 40]
    m = shape.search(window)
    if m and m.start() <= _SHAPE_WINDOW:
        yield pos + m.start(), pos + m.end()


def _value_spans(text: str, pos: int, take_next: bool) -> Iterable[tuple[int, int]]:
    sep = _SEP.match(text, pos)
    vstart = sep.end() if sep else pos
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    # If the separator consumed a newline, the value is on a later line.
    if vstart > line_end:
        line_end = text.find("\n", vstart)
        if line_end == -1:
            line_end = len(text)
    vend = min(line_end, vstart + _MAX_VALUE)
    took_same_line = text[vstart:vend].strip() != ""
    if took_same_line:
        yield vstart, vend
    if take_next or not took_same_line:
        nstart = line_end + 1
        while nstart < len(text) and text[nstart] in " \t\r\n":
            nstart += 1
        if nstart < len(text):
            nend = text.find("\n", nstart)
            if nend == -1:
                nend = len(text)
            nend = min(nend, nstart + _MAX_VALUE)
            if nend > nstart:
                yield nstart, nend


# ---------------------------------------------------------------------------
# Known names harvested from the folder structure / email headers
# ---------------------------------------------------------------------------

_NAME_NOISE = {
    # file/folder words
    "files",
    "file",
    "records",
    "record",
    "xtra",
    "extra",
    "info",
    "copy",
    "old",
    "new",
    "misc",
    "zip",
    "print",
    "travel",
    "exchange",
    "docs",
    "documents",
    "folder",
    "scan",
    "scans",
    # function words that would match sentence starts once capitalized
    "the",
    "and",
    "of",
    "for",
    "from",
    "to",
    "in",
    "on",
    "at",
    "by",
    "with",
    "you",
    "he",
    "she",
    "all",
    "re",
    "fw",
    "fwd",
    "yr",
    "till",
    # surname particles are handled via _PARTICLES (phrases only)
    # titles / suffixes
    "dr",
    "mr",
    "mrs",
    "ms",
    "prof",
    "jr",
    "sr",
    "ii",
    "iii",
    "iv",
    # status annotations commonly seen on per-person folders
    "student",
    "students",
    "transfer",
    "transferred",
    "declined",
    "decline",
    "voluntary",
    "withdraw",
    "withdrawn",
    "withdrew",
    "withdrawal",
    "presidential",
    "scholar",
    "fellow",
    "fellowship",
    "deferred",
    "defer",
    "deferral",
    "admit",
    "admitted",
    "accepted",
    "rejected",
    "leave",
    "visiting",
    "special",
    "joint",
    "dual",
    "fall",
    "spring",
    "summer",
    "year",
    "program",
    "completed",
    "terminated",
    "deceased",
}


_PARTICLES = {
    "de",
    "da",
    "del",
    "della",
    "der",
    "den",
    "di",
    "du",
    "la",
    "le",
    "van",
    "von",
    "el",
    "al",
    "bin",
    "ibn",
    "dos",
    "das",
    "st",
}


_EXTRA_NOISE: set[str] = set()


def configure_name_noise(words: Iterable[str]) -> None:
    """Add institution-specific non-name words (degree codes, departments, status notes) that
    appear in folder names. Call before building the registry; replaces any earlier set."""
    _EXTRA_NOISE.clear()
    _EXTRA_NOISE.update(w.lower() for w in words)


def name_variants(raw: str) -> tuple[set[str], set[str]]:
    """Split a folder/display name into (full-name phrases, single name tokens)."""
    raw = re.sub(r"\(.*?\)|<.*?>|\S+@\S+", " ", raw)
    if "," in raw:
        last, _, first = raw.partition(",")
        raw = f"{first} {last}"
    words = [t for t in re.split(r"[^A-Za-zÀ-ÖØ-öø-ÿ'’]+", raw) if t]
    noise = _NAME_NOISE | _EXTRA_NOISE
    words = [t for t in words if (t.lower() not in noise or t.lower() in _PARTICLES) and len(t) >= 2]
    tokens = [t for t in words if t.lower() not in _PARTICLES]
    phrases: set[str] = set()
    if len(words) > len(tokens) and len(tokens) >= 2:
        phrases.add(" ".join(words))  # "Maria De Souza"
    if len(tokens) >= 2:
        phrases.add(" ".join(tokens))
        phrases.add(" ".join([tokens[-1], *tokens[:-1]]))
        phrases.add(f"{tokens[-1]}, {' '.join(tokens[:-1])}")
        phrases.add(f"{tokens[0]} {tokens[-1]}")
        phrases.add(f"{tokens[-1]}, {tokens[0]}")
    return phrases, set(tokens)


class KnownNameRecognizer(EntityRecognizer):
    """Exact-match recognizer for names known in advance, optionally tied to registry IDs.

    Full names: case-insensitive, whitespace-flexible. Single tokens: case-sensitive in
    Capitalized or UPPER form only, so 'Will' or 'Rose' as words in prose are less likely hit.

    ``students`` is an iterable of (registry_id, names); an empty ID means "known name, no
    registry entry". ``resolve`` maps a matched string back to a single registry ID.
    """

    def __init__(
        self,
        students: Iterable[tuple[str, Iterable[str]]],
        name: str = "KnownNameRecognizer",
        entity: str = "KNOWN_STUDENT_NAME",
    ) -> None:
        phrases: set[str] = set()
        tokens: set[str] = set()
        self._key_ids: dict[frozenset[str], set[str]] = {}
        self._token_ids: dict[str, set[str]] = {}
        for sid, names in students:
            for n in names:
                ph, tk = name_variants(n)
                phrases |= ph
                tokens |= tk
                if sid:
                    lower = frozenset(t.lower() for t in tk)
                    if len(lower) >= 2:
                        self._key_ids.setdefault(lower, set()).add(sid)
                    for t in lower:
                        self._token_ids.setdefault(t, set()).add(sid)
        self._phrase_rx = _alternation(phrases, flags=re.IGNORECASE, flexible_space=True)
        forms: set[str] = set()
        for t in tokens:
            forms.add(t[:1].upper() + t[1:].lower())
            forms.add(t[:1].upper() + t[1:])  # keep inner capitals (McDonald)
            forms.add(t.upper())
        self._token_rx = _alternation(forms, flags=0, flexible_space=False)
        self._entity = entity
        super().__init__(supported_entities=[entity], name=name)

    @classmethod
    def from_names(
        cls, names: Iterable[str], name: str = "KnownNameRecognizer", entity: str = "KNOWN_STUDENT_NAME"
    ) -> KnownNameRecognizer:
        return cls([("", list(names))], name=name, entity=entity)

    def is_registry_full_name(self, name: str) -> bool:
        """True if *name* (any order, 2+ tokens) is the full name or alias of a registry person."""
        key = frozenset(t.lower() for t in name_variants(name)[1])
        return len(key) >= 2 and key in self._key_ids

    def resolve(self, matched: str, context_ids: Iterable[str] = ()) -> str | None:
        """Registry ID for a matched name, or None when unknown or ambiguous.

        A full name resolves directly. Partial names ('Roe', 'Ms. Roe') resolve only if a
        single registry person carries all the tokens, or if exactly one candidate owns the document."""
        _, toks = name_variants(matched)
        key = frozenset(t.lower() for t in toks)
        if not key:
            return None
        cands = set(self._key_ids.get(key, set()))
        if not cands:
            known = [self._token_ids[t] for t in key if t in self._token_ids]
            if not known:
                return None
            cands = set.intersection(*known)
        if len(cands) == 1:
            return next(iter(cands))
        ctx = cands & set(context_ids)
        return next(iter(ctx)) if len(ctx) == 1 else None

    def load(self) -> None:  # pragma: no cover
        return None

    @property
    def is_empty(self) -> bool:
        return self._phrase_rx is None and self._token_rx is None

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None
    ) -> list[RecognizerResult]:
        if self._entity not in entities:
            return []
        out: list[RecognizerResult] = []
        for rx, score in ((self._phrase_rx, 0.95), (self._token_rx, 0.85)):
            if rx is None:
                continue
            for m in rx.finditer(text):
                out.append(RecognizerResult(self._entity, m.start(), m.end(), score))
        return out

    def find_any(self, text: str) -> bool:
        """Used by the leak check."""
        return any(rx is not None and rx.search(text) for rx in (self._phrase_rx, self._token_rx))


def _alternation(items: Iterable[str], *, flags: int, flexible_space: bool) -> re.Pattern[str] | None:
    items = sorted({i for i in items if i}, key=len, reverse=True)
    if not items:
        return None
    parts = []
    for i in items:
        esc = re.escape(i)
        if flexible_space:
            esc = esc.replace(r"\ ", r"[\s,]+")
        parts.append(esc)
    # Boundaries are letters/digits only: "_" counts as a separator so names on form blanks
    # ("___________Jane Roe") still match.
    return re.compile(r"(?<![^\W_])(?:" + "|".join(parts) + r")(?![^\W_])", flags)


def custom_recognizers(extra_id_labels: Iterable[str] = ()) -> list[EntityRecognizer]:
    labels = list(extra_id_labels)
    return [
        ssn_like_recognizer(),
        student_id_recognizer([lab.lower() for lab in labels]),
        street_address_recognizer(),
        gov_id_recognizer(),
        LabeledFieldRecognizer(labels),
    ]
