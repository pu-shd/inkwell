"""Permitted places: institutional place names that stay visible (the place analogue of permitted
names), and their suggestion from places that recur across many record subjects' files."""

from __future__ import annotations

import csv
import stat
from collections.abc import Iterator
from email.message import EmailMessage
from pathlib import Path

import fitz
import pytest
from presidio_analyzer import RecognizerResult

from inkwell import cli
from inkwell.config import ConfigError, load_config
from inkwell.engine import Findings, Scrubber
from inkwell.handlers import Ctx, process
from inkwell.permitted import PermittedNames, candidate_place, load_places, place_key

from .conftest import STUDENT, text_pdf

HEADER = "place,aliases\n"


def _write(path: Path, body: str, header: str = HEADER) -> Path:
    path.write_text(header + body)
    return path


@pytest.fixture
def places_file(tmp_path: Path) -> Path:
    return _write(tmp_path / "places.csv", 'Springfield,"Springfield, Ohio;Springfield OH"\nWexford Hall,\n')


@pytest.fixture
def placed(scrubber: Scrubber, places_file: Path) -> Iterator[Scrubber]:
    scrubber.set_permitted(PermittedNames([], places=load_places(places_file)))
    yield scrubber
    scrubber.set_permitted(PermittedNames([]))


def _red(sc: Scrubber, text: str) -> tuple[str, Findings]:
    f = Findings()
    return sc.redact_text(text, f), f


# --- loading and configuration -------------------------------------------------------------


def test_load_valid(places_file: Path) -> None:
    places = load_places(places_file)
    assert [p.place for p in places] == ["Springfield", "Wexford Hall"]
    assert places[0].aliases == ("Springfield, Ohio", "Springfield OH")


def test_load_ignores_evidence_columns(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "p.csv", "Springfield,,4,9,0\n", "place,aliases,evidence_students,evidence_x,evidence_y\n"
    )
    assert [x.place for x in load_places(p)] == ["Springfield"]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("12 Elm Street,\n", "digits"),
        ("Springfield,08540\n", "digits"),
        (",\n", "place is required"),
        ("S,\n", "place is required"),
        ("x" * 81 + ",\n", "too long"),
    ],
)
def test_load_rejects_bad_rows(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_places(_write(tmp_path / "p.csv", body))


def test_load_rejects_bad_header(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="header"):
        load_places(_write(tmp_path / "p.csv", "Springfield\n", "name\n"))


def test_config(tmp_path: Path, places_file: Path) -> None:
    env = {
        "INKWELL_INPUT_DIR": str(tmp_path / "in"),
        "INKWELL_OUTPUT_DIR": str(tmp_path / "out"),
        "INKWELL_KEY_FILE": str(tmp_path / "k" / "key.csv"),
    }
    (tmp_path / "in").mkdir()
    (tmp_path / "k").mkdir()
    assert load_config(env).permitted_places_file is None
    assert load_config({**env, "INKWELL_PERMITTED_PLACES_FILE": str(places_file)}).permitted_places_file
    with pytest.raises(ConfigError, match="does not exist"):
        load_config({**env, "INKWELL_PERMITTED_PLACES_FILE": str(tmp_path / "nope.csv")})
    inside = _write(tmp_path / "in" / "p.csv", "Springfield,\n")
    with pytest.raises(ConfigError, match="outside INKWELL_INPUT_DIR"):
        load_config({**env, "INKWELL_PERMITTED_PLACES_FILE": str(inside)})


def test_place_key() -> None:
    assert (
        place_key("Springfield,  Ohio.")
        == place_key("springfield ohio")
        == place_key("The Springfield, Ohio")
    )
    assert place_key("St. Louis") == place_key("St Louis")


# --- matching ------------------------------------------------------------------------------


def test_permitted_place_stays_others_redacted(placed: Scrubber) -> None:
    out, f = _red(placed, "The meeting is in Springfield. We moved from Boston last year.")
    assert "Springfield" in out and "Boston" not in out
    assert f.counts["PERMITTED_PLACE"] == 1 and f.counts["LOCATION"] >= 1


def test_personal_fields_always_redacted(placed: Scrubber) -> None:
    out, _ = _red(placed, "Place of Birth: Springfield\nThe office is in Springfield.")
    first, second = out.splitlines()
    assert "Springfield" not in first, "a labeled personal field wins over a permitted place"
    assert "Springfield" in second


def test_only_locations_are_permitted(placed: Scrubber) -> None:
    # A permitted place never unmasks a person, ID or address that happens to share its text.
    text = "Wexford Hall"
    results = [RecognizerResult("PERSON", 0, len(text), 0.85)]
    assert [r.entity_type for r in placed._mark_permitted(text, results)] == ["PERSON"]


def test_record_subject_wins_over_place(placed: Scrubber, tmp_path: Path) -> None:
    sur = STUDENT.split()[1]
    sc = placed
    sc.set_permitted(PermittedNames([], places=load_places(_write(tmp_path / "p.csv", f"{sur},\n"))))
    text = f"Visit {sur} soon"
    s = text.index(sur)
    results = [
        RecognizerResult("LOCATION", s, s + len(sur), 0.85),
        RecognizerResult("KNOWN_STUDENT_NAME", s, s + len(sur), 1.0),
    ]
    kinds = sorted(r.entity_type for r in sc._mark_permitted(text, results))
    assert kinds == ["KNOWN_STUDENT_NAME", "LOCATION"]


def test_multi_part_span_keeps_only_permitted_parts(placed: Scrubber) -> None:
    text = "Springfield, Boston"
    results = [RecognizerResult("LOCATION", 0, len(text), 0.85)]
    got = {(r.entity_type, text[r.start : r.end]) for r in placed._mark_permitted(text, results)}
    assert got == {("PERMITTED_PLACE", "Springfield"), ("LOCATION", "Boston")}
    whole = "Springfield, Ohio"
    got = {
        (r.entity_type, whole[r.start : r.end])
        for r in placed._mark_permitted(whole, [RecognizerResult("LOCATION", 0, len(whole), 0.85)])
    }
    assert got == {("PERMITTED_PLACE", whole)}, "an alias matches the whole span"


def test_pdf(placed: Scrubber, ctx: Ctx) -> None:
    out = process("a.pdf", text_pdf(["The office is in Springfield.", "She was raised in Boston."]), ctx)
    assert out.status == "redacted" and out.data
    with fitz.open(stream=out.data, filetype="pdf") as d:
        layer = d[0].get_text()
    assert "Springfield" in layer and "Boston" not in layer
    assert out.findings.counts["PERMITTED_PLACE"] >= 1


# --- suggestion candidates -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "clean"),
    [
        ("Springfield", "Springfield"),
        (" Springfield, Ohio. ", "Springfield, Ohio"),
        ("the Wexford Hall", "Wexford Hall"),
        ("St. Louis", "St. Louis"),
        ("Isle of Man", "Isle of Man"),
        ("Wexford Hall", "Wexford Hall"),
        ("New South Wales", "New South Wales"),
        ("NJ", "NJ"),
    ],
)
def test_candidate_place_keeps(text: str, clean: str) -> None:
    assert candidate_place(text) == clean


@pytest.mark.parametrize(
    "text",
    [
        "Home City",
        "Birth City",
        "Native Country",
        "Street, City",
        "Citizenship Ruritania",
        "U.S.A",
        "D.C",
        "Univ Springfield",
        "Springfield College",
        "Ph.D. Students",
        "Trip Overview",
        "Lower Level",
        "12 Elm Street",
        "08540",
        "springfield",
        'dir="ltr',
        "Spring√©field",
        "A",
        "x&nbsp",
        "One Two Three Four Five Six Seven",
    ],
)
def test_candidate_place_rejects(text: str) -> None:
    assert candidate_place(text) is None


def test_candidate_place_extra_noise() -> None:
    assert candidate_place("Widget Hall") == "Widget Hall"
    assert candidate_place("Widget Hall", noise=frozenset({"widget"})) is None


# --- CLI suggestion pass -------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {"in": tmp_path / "in", "out": tmp_path / "out", "key": tmp_path / "secret" / "key.csv"}
    paths["in"].mkdir()
    paths["key"].parent.mkdir()
    for k, var in (("in", "INPUT_DIR"), ("out", "OUTPUT_DIR"), ("key", "KEY_FILE")):
        monkeypatch.setenv(f"INKWELL_{var}", str(paths[k]))
    monkeypatch.setenv("INKWELL_WORKERS", "1")
    return paths


def _subjects(root: Path) -> None:
    for i, (folder, home) in enumerate(
        [("Roe, Jane", "Boston"), ("Poe, Jasper", "Boston"), ("Doe, Kip", "Denver")]
    ):
        d = root / "Cohort 1990-91" / folder
        d.mkdir(parents=True)
        m = EmailMessage()
        m["From"] = "Registrar <office@example.org>"
        m["To"] = f"{folder} <s{i}@example.edu>"
        m["Subject"] = "Welcome"
        m.set_content(f"Welcome to Springfield.\nPlace of Birth: {home}\nWe hope Boston is far behind you.\n")
        (d / "welcome.eml").write_bytes(m.as_bytes())


def test_suggest_places(env: dict[str, Path]) -> None:
    _subjects(env["in"])
    assert cli.main(["--suggest-permitted", "--min-students", "3"]) == 0
    assert not env["out"].exists() and not env["key"].exists()
    files = sorted(env["key"].parent.glob("permitted-places-suggestions-*.csv"))
    assert len(files) == 1 and stat.S_IMODE(files[0].stat().st_mode) == 0o600
    with files[0].open() as fh:
        rows = {r["place"]: r for r in csv.DictReader(fh)}
    # Springfield recurs for everyone outside personal fields. Boston recurs too, but is mostly
    # seen as someone's place of birth; Denver appears once (and only as a personal field).
    assert set(rows) == {"Springfield"}, rows.keys()
    assert rows["Springfield"]["evidence_students"] == "3"
    assert rows["Springfield"]["evidence_personal_students"] == "0"
    assert [p.place for p in load_places(files[0])] == ["Springfield"], "directly usable"


def test_suggest_places_share_of_everyone(env: dict[str, Path]) -> None:
    # Institutional places reach a large share of all record subjects; hometowns don't.
    _subjects(env["in"])
    d = env["in"] / "Cohort 1990-91" / "Loe, Ivy"
    d.mkdir()
    (d / "note.txt").write_text("Nothing about places here.")
    assert cli.main(["--suggest-permitted", "--min-students", "3", "--min-place-share", "100"]) == 0
    (f,) = env["key"].parent.glob("permitted-places-suggestions-*.csv")
    assert list(csv.DictReader(f.open())) == [], "3 of 4 people is below a 100% share"
    assert cli.main(["--suggest-permitted", "--min-place-share", "0"]) == 1, "share must be 1-100"
