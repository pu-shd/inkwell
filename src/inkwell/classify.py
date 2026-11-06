"""Document-level classifier for protected record types that are withheld.

The categories below are the built-in example profile (education records).

Presidio works at the entity level; "this is a transcript" is a document-level judgement.
Each category has weighted indicator phrases. A document whose best category reaches the
threshold is withheld (replaced by a placeholder). Over-withholding is the safe direction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (regex, weight). Strong, near-unambiguous indicators weigh 3; supporting terms weigh 1.
CATEGORIES: dict[str, list[tuple[str, int]]] = {
    "transcript_or_grades": [
        (r"\b(?:official|unofficial|academic)\s+transcript", 3),
        (r"\btranscripts?\b", 2),
        (r"\bgrade\s+report\b", 3),
        (r"\bgrade\s+point\s+average\b|\b(?:cum(?:ulative)?\.?|term|overall|major)\s+G\.?P\.?A\b", 3),
        (r"\bG\.?P\.?A\.?\b", 2),
        (r"\bquality\s+points\b|\bcredits?\s+(?:attempted|earned)\b|\bacademic\s+record\b", 3),
        (r"\b(?:final|letter|course|semester)\s+grades?\b|\bgrades?\s+(?:of|for|in)\b", 2),
        (r"\bgrading\s+(?:option|basis)\b|\bpass\s*/\s*fail\b|\bincomplete\s+grade\b", 2),
        (r"\bacademic\s+(?:standing|progress|probation)\b|\bgeneral\s+exam(?:ination)?\s+result", 2),
    ],
    "class_schedule": [
        (r"\b(?:class|course|student|weekly)\s+schedule\b", 3),
        (r"\bcourse\s+(?:enrollment|registration|selection)\b|\benrolled\s+courses\b", 2),
        (r"\b(?:M|T|W|Th|F|MW|TTh|MWF|TR)\s+\d{1,2}:\d{2}\s*(?:am|pm)?\s*[-–]\s*\d{1,2}:\d{2}", 2),
    ],
    "disciplinary": [
        (r"\bdisciplin(?:ary|e)\b", 3),
        (r"\bacademic\s+(?:integrity|dishonesty|misconduct)\b|\bhonor\s+(?:code|committee)\b", 3),
        (r"\bmisconduct\b|\bplagiari[sz]|\bsanction(?:s|ed)?\b|\bsuspen(?:sion|ded)\b", 2),
        (r"\b(?:expel|expulsion|dismissal|dismissed)\b|\bconduct\s+(?:board|hearing|violation)", 2),
        (r"\bprobation\b", 2),
    ],
    "financial_aid": [
        (r"\bfinancial\s+(?:aid|support|assistance)\b|\bFAFSA\b|\bstudent\s+loans?\b", 3),
        (r"\bfellowship\s+(?:award|offer|support|letter)\b|\baward\s+letter\b", 3),
        (r"\bstipend\b|\btuition\b|\bfellowship\b|\bassistantship\b|\bscholarship\b", 2),
        (r"\bbursar\b|\bloan\b|\bgrant\s+(?:award|support)\b|\bfunding\s+(?:offer|package)\b", 2),
        (r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?\b", 1),
        (r"\b(?:I-?9|W-?2|1098-?T|1099)\b|\bpayroll\b|\bdirect\s+deposit\b", 2),
    ],
    "special_education": [
        (r"\bIEP\b|\bindividuali[sz]ed\s+education(?:al)?\s+(?:program|plan)\b", 3),
        (r"\b504\s+plan\b|\bspecial\s+education\b|\bdisability\s+services\b", 3),
        (r"\b(?:learning|physical|psychiatric)\s+disabilit(?:y|ies)\b", 3),
        (r"\b(?:disability|academic|testing|exam(?:ination)?|classroom|reasonable)\s+accommodations?\b", 3),
        (r"\bODS\b|\baccessibility\s+services\b", 2),
        (r"\bmedical\s+leave\b|\bcounseling\s+(?:center|services)\b|\bdiagnos(?:is|ed)\b", 2),
    ],
    "biometric": [
        (r"\bfingerprints?\b|\bbiometric\b|\bretina(?:l)?\s+scan\b|\bvoice\s*print\b", 3),
    ],
}

_COMPILED = {cat: [(re.compile(rx, re.IGNORECASE), w) for rx, w in pats] for cat, pats in CATEGORIES.items()}

# Count is capped per indicator so one repeated word can't dominate.
_PER_INDICATOR_CAP = 3


@dataclass(frozen=True)
class Classification:
    withhold: bool
    category: str | None
    score: int
    scores: dict[str, int]


def classify(text: str, threshold: int) -> Classification:
    scores: dict[str, int] = {}
    for cat, pats in _COMPILED.items():
        s = 0
        for rx, w in pats:
            n = min(len(rx.findall(text)), _PER_INDICATOR_CAP)
            s += n * w
        scores[cat] = s
    best = max(scores, key=lambda c: scores[c])
    best_score = scores[best]
    if best_score >= threshold:
        return Classification(True, best, best_score, scores)
    return Classification(False, None, best_score, scores)
