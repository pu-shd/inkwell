from __future__ import annotations

import pytest

from inkwell.engine import Findings, Scrubber
from inkwell.recognizers import KnownNameRecognizer, name_variants

from .conftest import SSN


def _red(sc: Scrubber, text: str) -> tuple[str, Findings]:
    f = Findings()
    return sc.redact_text(text, f), f


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"SSN: {SSN}", "6789"),
        ("Social Security No. 123456789", "123456789"),
        ("Soc. Sec. # 987 65 4321", "4321"),
        ("Student ID: 961234567", "961234567"),
        ("Student number 960001234", "960001234"),
        ("Date of Birth: 04/17/1966", "1966"),
        ("DOB 17-Apr-66", "17-Apr-66"),
        ("born on March 3, 1965 in Ohio", "1965"),
        ("Place of Birth: Kalamazoo, Michigan", "Kalamazoo"),
        ("Birthplace - Ulaanbaatar", "Ulaanbaatar"),
        ("Mother's Maiden Name: Thistlewaite", "Thistlewaite"),
        ("Father's name: Ozymandias Quell", "Ozymandias"),
        ("Parent/Guardian Name: Brunhilde Okonkwo-Vasquez", "Brunhilde"),
        ("Home Address: 42 Wallaby Way, Apt 3", "Wallaby"),
        ("Permanent address:\n  1600 Pennsylvania Avenue\n  Washington, DC 20500", "Pennsylvania"),
        ("PO Box 1234, Smalltown", "1234"),
        ("SEVIS ID N0012345678", "N0012345678"),
        ("Contact me at jdoe@example.com", "jdoe@example.com"),
        ("Phone (609) 555-0142", "555-0142"),
    ],
)
def test_identifiers_removed(scrubber: Scrubber, text: str, secret: str) -> None:
    out, f = _red(scrubber, text)
    assert secret not in out, out
    assert sum(f.counts.values()) > 0


def test_ocr_label_value_on_next_line(scrubber: Scrubber) -> None:
    out, _ = _red(scrubber, "Social Security Number\n555 12 3456\nOther stuff")
    assert "3456" not in out
    assert "Other stuff" in out


def test_known_student_name_variants(scrubber: Scrubber) -> None:
    text = "QUINTAVIUS FERNHOLLOW, Fernhollow, Quintavius, and Q. Fernhollow attended."
    out, f = _red(scrubber, text)
    assert "Fernhollow" not in out and "FERNHOLLOW" not in out and "Quintavius" not in out
    assert f.counts["KNOWN_STUDENT_NAME"] >= 1


def test_adhoc_names(scrubber: Scrubber) -> None:
    f = Findings()
    out = scrubber.redact_text("Thanks, Zebulon", f, extra_names=["Zebulon Pryce-Okafor"])
    assert "Zebulon" not in out


def test_benign_text_survives(scrubber: Scrubber) -> None:
    text = "The committee meeting is scheduled for the spring term."
    out, f = _red(scrubber, text)
    assert out == text
    assert not f.counts


def test_name_variants() -> None:
    phrases, tokens = name_variants("Roe, Jane")
    assert "Jane Roe" in phrases and "Roe, Jane" in phrases
    assert tokens == {"Roe", "Jane"}
    _, tokens2 = name_variants("Okafor Records  xtra info")
    assert tokens2 == {"Okafor"}


def test_known_name_token_case_sensitive() -> None:
    rec = KnownNameRecognizer.from_names(["Qo Zennor"])
    assert rec.find_any("Dr. Qo said")
    assert not rec.find_any("the qoppa sign and aqorn")  # lowercase or inside a word


def test_status_words_in_folder_names_are_not_names() -> None:
    # Regression: folders like "<Name> Transfer" made "Transfer" a name token, which matched
    # every "Content-Transfer-Encoding" header and blocked 38 pilot emails.
    _, tokens = name_variants("Roe Jane Transfer Declined Voluntary Withdraw In You")
    assert tokens == {"Roe", "Jane"}
    rec = KnownNameRecognizer.from_names(["Jane Roe - Presidential Scholar Visiting Student transferred"])
    assert not rec.find_any("Content-Transfer-Encoding: base64")
    assert not rec.find_any("In the Visiting Student handbook, You will find")
    assert rec.find_any("Jane")


def test_name_particles_not_standalone_tokens() -> None:
    # Regression: "De" from "Maria De Souza" matched "De" in "Deﬁnition" once the text layer
    # dropped the ligature. Particles stay in full-name phrases only.
    phrases, tokens = name_variants("Maria De Souza")
    assert "De" not in tokens and {"Maria", "Souza"} <= tokens
    assert "Maria De Souza" in phrases
