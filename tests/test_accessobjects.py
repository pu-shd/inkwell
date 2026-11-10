"""Sanitizing and auditing Access objects exported with SaveAsText. Synthetic data only."""

from __future__ import annotations

from inkwell.accessobjects import Audit, decode, sanitize
from inkwell.recognizers import KnownNameRecognizer

from .conftest import SID

FORM = """Version =20
VersionRequired =20
Checksum =-123456789
Begin Form
    RecordSelectors = NotDefault
    Filter ="([SSN]='123456789')"
    ServerFilter ="[LastName]='Fernhollow'"
        " AND [FirstName]='Quintavius'"
    OrderBy ="[LastName]"
    Caption ="Return this form to Ada by September 26"
    PrtDevNamesW = Begin
        0x0800110000000000 ,
    End
    PrtDevModeW = Begin
        0x48005000 ,0x6e006f00 ,0x65006c00 ,0x69006e00
        0x61000000 ,0x00000000 ,0x00000000 ,0x00000000
        0x00000000 ,0x00000000 ,0x00000000 ,0x00000000
        0x00000000 ,0x00000000 ,0x00000000 ,0x00000000
        0x01040006 ,0x02000000
    End
    PrtMip = Begin
        0x6801000068010000
    End
    Begin TextBox
        Name ="SSN"
        ControlSource ="SSN"
        InputMask ="000\\-00\\-0000;;_"
    End
End
"""


def test_sanitize_removes_filters_with_their_continuation_lines() -> None:
    out, changes = sanitize(FORM)
    assert "Filter =" not in out and "ServerFilter" not in out and "AND [FirstName]" not in out
    assert 'OrderBy ="[LastName]"' in out  # sort orders name fields only
    assert changes["filter"] == 2


def test_sanitize_drops_printer_names_and_blanks_the_device_name() -> None:
    out, changes = sanitize(FORM)
    assert "PrtDevNamesW" not in out
    assert "PrtMip = Begin" in out  # margins and layout stay
    devmode = out[out.index("PrtDevModeW = Begin") : out.index("PrtMip")]
    first_line = devmode.splitlines()[1]
    assert first_line.strip() == "0x00000000 ,0x00000000 ,0x00000000 ,0x00000000"  # "HPnoelina" gone
    assert "0x01040006 ,0x02000000" in devmode  # the rest (orientation, paper) is kept
    assert changes["printer_names"] == 1 and changes["device_name"] == 1


def test_sanitize_removes_checksum_and_ssn_input_masks() -> None:
    out, changes = sanitize(FORM)
    assert "Checksum" not in out and "InputMask" not in out
    assert 'ControlSource ="SSN"' in out
    assert changes["checksum"] == 1 and changes["ssn_input_mask"] == 1


def test_sanitize_is_idempotent() -> None:
    once, _ = sanitize(FORM)
    twice, changes = sanitize(once)
    assert twice == once and sum(changes.values()) == 0


def _audit(allow: dict[str, int] | None = None) -> Audit:
    known = KnownNameRecognizer([(SID, ["Fernhollow, Quintavius"])])
    return Audit(known, tokens={"QxTkvPmRa"}, allowed_words=allow or {})


def test_audit_finds_identifiers_in_unsanitized_text() -> None:
    findings = _audit().scan("forms/F", FORM)
    kinds = {f.kind for f in findings}
    assert {"nine_digits", "name_word"} <= kinds  # first name and surname are on separate lines


def test_audit_of_sanitized_text_is_clean() -> None:
    out, _ = sanitize(FORM)
    assert _audit().scan("forms/F", out) == []


def test_audit_ignores_hex_and_checksums_but_not_bare_numbers() -> None:
    text = 'Checksum =123456789\n        0x123456789abcdef0 ,\n    Caption ="id 987654321"\n'
    findings = _audit().scan("reports/R", text)
    assert [(f.kind, f.line) for f in findings] == [("nine_digits", 3)]


def test_audit_reports_record_tokens_emails_phones_and_ssns() -> None:
    text = 'A ="QxTkvPmRa"\nB ="x@example.org"\nC ="(609) 555-0142"\nD ="123-45-6789"\n'
    kinds = [f.kind for f in _audit().scan("queries/Q", text)]
    assert kinds == ["record_token", "email", "phone", "ssn"]


def test_audit_masks_what_it_found() -> None:
    text = 'Caption ="Quintavius Fernhollow 123456789 QxTkvPmRa"'
    shown = " ".join(f.context for f in _audit().scan("forms/F", text))
    assert "Quintavius" not in shown and "Fernhollow" not in shown
    assert "123456789" not in shown and "QxTkvPmRa" not in shown


def test_allowed_words_are_counted_and_capped() -> None:
    text = 'Caption ="Fernhollow Room"\nCaption ="Fernhollow Hall"\n'
    allow = {"Fernhollow": 1}
    findings = _audit(allow).scan("forms/F", text)
    assert [(f.kind, f.line) for f in findings] == [("name_word", 2)]  # the second use exceeds the cap


def test_decode_handles_utf16_and_utf8() -> None:
    assert decode('Caption ="é"'.encode("utf-16")) == 'Caption ="é"'
    assert decode(b"\xef\xbb\xbfCaption") == "Caption"
    assert decode(b"Caption") == "Caption"


def test_files_round_trip_in_their_own_encoding() -> None:
    # Regression: the export is ANSI (cp1252, no BOM); writing it back as UTF-8 would garble
    # non-ASCII characters when Access's LoadFromText reads the file as ANSI.
    from inkwell.accessobjects import read_object, write_object

    for raw in (
        'Caption ="Caf\xe9 r\xe9sum\xe9"\r\n'.encode("cp1252"),
        '﻿Caption ="é"\r\n'.encode(),
        'Caption ="é"\r\n'.encode("utf-16"),
        b'Caption ="plain"\r\n',
    ):
        text, encoding = read_object(raw)
        assert "Caption" in text
        assert write_object(text, encoding) == raw


def test_sanitize_drops_a_property_accdb_rejects() -> None:
    # Regression: LoadFromText into an .accdb failed every form and report exported from the .mdb
    # with "This property does not apply to this control" at NoSaveCTIWhenDisabled.
    text = 'Begin Form\n    Caption ="F"\n    NoSaveCTIWhenDisabled =1\n    Begin\n    End\nEnd\n'
    out, changes = sanitize(text)
    assert "NoSaveCTIWhenDisabled" not in out and 'Caption ="F"' in out
    assert changes["accdb_rejects"] == 1
