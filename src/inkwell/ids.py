"""Random, non-sequential identifiers with a Crockford base32 check character.

Sequential numbers assigned in sorted order would leak each person's alphabetical rank
within a class; these IDs carry no information about the person or the ordering.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Container

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford: no I, L, O, U
# Check character: position-weighted sum mod 31 (prime), drawn from the same alphabet so IDs are
# safe in filenames and shells. Catches single-character typos except 0<->Z swaps.

PREFIXES = {"student": "S", "doc": "D", "dir": "F", "record": "R"}
BODY_LEN = {"S": 8, "D": 10, "F": 6, "R": 8}


def _check_char(body: str) -> str:
    total = sum((i + 1) * ALPHABET.index(ch) for i, ch in enumerate(body))
    return ALPHABET[total % 31]


def new_id(kind: str, taken: Container[str] = ()) -> str:
    prefix = PREFIXES[kind]
    while True:
        body = "".join(secrets.choice(ALPHABET) for _ in range(BODY_LEN[prefix]))
        ident = f"{prefix}-{body}{_check_char(body)}"
        if ident not in taken:
            return ident


def is_valid(ident: str) -> bool:
    m = re.fullmatch(r"([SDFR])-([0-9A-HJKMNP-TV-Z]+)([0-9A-HJKMNP-TV-Z])", ident)
    if not m or len(m.group(2)) != BODY_LEN[m.group(1)]:
        return False
    return _check_char(m.group(2)) == m.group(3)
