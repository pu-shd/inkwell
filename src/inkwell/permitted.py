"""Permitted names: people whose full names stay visible in the output.

The list is a CSV kept beside the key file (it contains real names):

    name,aliases,emails,phones
    Ada Lovelace,"Augusta Ada King;A. Lovelace",ada@example.org,(555) 555-0100

Rules:
* full names only (two or more name tokens), matched case-insensitively in either order
  ("Ada Lovelace" == "Lovelace, Ada"); a lone surname or first name stays redacted;
* a registry person (a record subject) is never permitted: conflicts are removed at startup and,
  at match time, any overlap with a known subject keeps the redaction;
* contact details stay redacted unless contacts are explicitly allowed, and then only the
  exact addresses / numbers listed for that person are kept.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError
from .recognizers import KnownNameRecognizer

COLUMNS = ("name", "aliases", "emails", "phones")
EVIDENCE_PREFIX = "evidence_"  # extra columns written by --suggest-permitted; ignored on load
_TITLES = {
    "dr",
    "prof",
    "professor",
    "mr",
    "mrs",
    "ms",
    "miss",
    "mx",
    "dean",
    "sir",
    "dame",
    "rev",
    "jr",
    "sr",
    "ii",
    "iii",
    "phd",
    "md",
}
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def name_key(text: str) -> frozenset[str]:
    """Order-insensitive identity of a name; titles, punctuation and possessives ignored."""
    text = re.sub(r"['’]s\b", "", text)
    tokens = [t.lower() for t in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", text)]
    return frozenset(t.strip("'’-") for t in tokens if t.strip("'’-") and t.strip("'’-") not in _TITLES)


def variant_key(text: str) -> frozenset[str]:
    """name_key without initials: groups suggestion variants ("Charles B. Babbage", "Charles
    Babbage", "Babbage, Charles") into one row. Not used for matching."""
    return frozenset(t for t in name_key(text) if len(t) > 1)


# --- --suggest-permitted precision -----------------------------------------------------------
# NER reports many PERSON spans that are not a person's full name: form labels, degrees, markup,
# mojibake, initials, salutations, citations, places and services. candidate_name() keeps only
# name-shaped text. Words below never occur in a name here; sites add more with
# INKWELL_EXTRA_NAME_NOISE. Some rare surnames that are also such words are lost (add by hand).
_NOISE = frozenset(
    """name names first middle last family given initial initials maiden birth date place address
    mailing email e-mail phone cell mobile fax box po suffix number student students account contact
    contacts please print sent from subject cc examiner graduate undergraduate school office registrar
    program programs administrator admin affairs university college department dept institute center
    centre apartment apartments tower building library gymnasium memorial chapel drive street road
    avenue ave blvd boulevard hotel hotels travel airlines management supplier service services
    ticket detail details menu copy academic year semester term dissertation thesis laude cum summa
    magna gpa fellowship scholarship degree degrees visitor leave absence incomplete notebook
    learning deep random walk walks curriculum vitae resume language english data web webpage app
    application user trip enroll save information info soon possible report reports form forms home
    zip city state country signature signed regards sincerely thanks team committee board staff
    studies eligible master ability st rd ln
    as of the and for with on in at by to et al or""".split()
)
_SALUTATIONS = frozenset({"dear", "hi", "hello", "hey"})
_PARTICLES = frozenset(
    {"van", "von", "de", "der", "den", "da", "di", "del", "della", "du", "la", "le", "d'", "d’", "l'", "l’"}
)
# Typical UTF-8 read as Mac Roman / cp1252: "√©", "Ô¨Å", "‚Ä", "Ã©"; and U+FFFD.
_MOJIBAKE = re.compile("[\u221a\u00d4\u00ac\u00c3\u00c2\u201a\u0152\u2022\u00c5\ufffd]")
_ALLOWED = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ .'’-]+")
_INITIAL = re.compile(r"(?:[A-Za-z]\.?|(?:[A-Za-z]\.){2,})")
_WORD = re.compile(r"(?:[dlDL]['’])?[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]+")


def _plain(token: str) -> str:
    return token.lower().replace(".", "")


def _shape(tokens: list[str], noise: frozenset[str]) -> str | None:
    """I(nitial) / p(article) / F(ull word) per token, or None if a token can't be part of a name."""
    shape = ""
    for t in tokens:
        if _plain(t) in _NOISE or _plain(t) in noise:
            return None
        if _INITIAL.fullmatch(t):
            shape += "I"
        elif t in _PARTICLES:
            shape += "p"
        elif _WORD.fullmatch(t) and not re.search(r"[A-Z]{2,}[a-z]", t):
            shape += "F"
        else:
            return None
    return shape


def _full_name_shape(shape: str | None, n: int) -> bool:
    # two full words; not ending in an initial; not two names run together ("A. Ada B. Byron")
    return (
        shape is not None
        and shape.count("F") >= 2
        and shape.endswith("F")
        and n <= 5
        and not re.search(r"I+F+I+F", shape)
    )


def candidate_name(text: str, noise: frozenset[str] = frozenset()) -> str | None:
    """The text as a full personal name (salutation, titles and possessive removed), or None."""
    s = re.sub(r"&[A-Za-z]+;?|&#\d+;?", " ", text)  # HTML entities cut into the span
    s = re.sub(r"['’]s\s*$", "", re.sub(r"\s+", " ", s)).strip(" ,;")
    if _MOJIBAKE.search(s) or not _ALLOWED.fullmatch(s):
        return None
    tokens = s.split()
    while tokens and _plain(tokens[0]) in _SALUTATIONS:
        tokens.pop(0)
    while tokens and _plain(tokens[-1]) in _TITLES:
        tokens.pop()
    titled = tokens
    while titled and _plain(titled[0]) in _TITLES:
        titled = titled[1:]
    for cand in (titled, tokens):  # "Dean J. Roe": Dean is the first name, not a title
        if _full_name_shape(_shape(cand, noise), len(cand)):
            return " ".join(cand)
    return None


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


@dataclass(frozen=True)
class PermittedPerson:
    name: str
    aliases: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()

    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def load_permitted(path: Path) -> list[PermittedPerson]:
    try:
        fh = path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"permitted names file cannot be read: {exc.strerror}") from exc
    with fh:
        reader = csv.DictReader(fh)
        header = tuple(reader.fieldnames or ())
        extra = [c for c in header[len(COLUMNS) :] if not c.startswith(EVIDENCE_PREFIX)]
        if header[: len(COLUMNS)] != COLUMNS or extra:
            raise ConfigError(f"permitted names file header must be {','.join(COLUMNS)}")
        people: list[PermittedPerson] = []
        for line, row in enumerate(reader, start=2):
            split = [
                tuple(x.strip() for x in (row.get(c) or "").split(";") if x.strip()) for c in COLUMNS[1:]
            ]
            person = PermittedPerson((row.get("name") or "").strip(), *split)
            for n in person.names():
                if len(name_key(n)) < 2:
                    raise ConfigError(f"permitted names file line {line}: a full name is required")
            for e in person.emails:
                if not _EMAIL.fullmatch(e):
                    raise ConfigError(f"permitted names file line {line}: invalid email address")
            for p in person.phones:
                if len(_digits(p)) < 7:
                    raise ConfigError(f"permitted names file line {line}: invalid phone number")
            people.append(person)
    return people


class PermittedNames:
    def __init__(self, people: Iterable[PermittedPerson], allow_contacts: bool = False) -> None:
        self.people = list(people)
        self.allow_contacts = allow_contacts
        self._keys = {name_key(n) for p in self.people for n in p.names()}
        self._emails = {e.lower() for p in self.people for e in p.emails}
        self._phones = {_digits(x) for p in self.people for x in p.phones}

    def __bool__(self) -> bool:
        return bool(self._keys)

    def is_name(self, text: str) -> bool:
        key = name_key(text)
        return len(key) >= 2 and key in self._keys

    def is_contact(self, entity: str, text: str) -> bool:
        if not self.allow_contacts:
            return False
        if entity == "EMAIL_ADDRESS":
            return text.strip().lower() in self._emails
        if entity == "PHONE_NUMBER":
            return _digits(text) in self._phones
        return False

    @staticmethod
    def without_students(
        people: Sequence[PermittedPerson], known: KnownNameRecognizer
    ) -> tuple[list[PermittedPerson], list[PermittedPerson]]:
        """Split into (kept, conflicts): anyone whose name or alias is a registry person."""
        kept: list[PermittedPerson] = []
        conflicts: list[PermittedPerson] = []
        for p in people:
            (conflicts if any(known.is_registry_full_name(n) for n in p.names()) else kept).append(p)
        return kept, conflicts


def write_people(path: Path, rows: Iterable[Sequence[str]], extra_header: Sequence[str] = ()) -> None:
    """Owner-only CSV in the permitted-names format (used for conflicts and suggestions)."""
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([*COLUMNS, *extra_header])
        for r in rows:
            w.writerow(r)
