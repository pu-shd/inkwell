"""Institution-specific conventions live in configuration, not code."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from inkwell import recognizers
from inkwell.config import ConfigError, load_config
from inkwell.pseudonym import PseudonymMap
from inkwell.recognizers import LabeledFieldRecognizer, name_variants
from inkwell.registry import Registry


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    (tmp_path / "in").mkdir(exist_ok=True)
    env = {
        "INKWELL_INPUT_DIR": str(tmp_path / "in"),
        "INKWELL_OUTPUT_DIR": str(tmp_path / "out"),
        "INKWELL_KEY_FILE": str(tmp_path / "key.csv"),
    }
    env.update(over)
    return env


def test_defaults_are_generic(tmp_path: Path) -> None:
    cfg = load_config(_env(tmp_path))
    assert cfg.extra_id_labels == () and cfg.extra_name_noise == ()
    import re

    rx = re.compile(cfg.group_dir_regex)
    assert rx.match("Cohort 2019-20").group(1) == "2019-20"  # type: ignore[union-attr]
    assert rx.match("2019-20") and not rx.match("Jane Roe")


def test_config_parses_and_validates(tmp_path: Path) -> None:
    cfg = load_config(
        _env(
            tmp_path,
            INKWELL_EXTRA_ID_LABELS="XUID, Banner ID",
            INKWELL_EXTRA_NAME_NOISE="Cohortx, MSc",
            INKWELL_GROUP_DIR_REGEX=r"^Year (\d{4})$",
        )
    )
    assert cfg.extra_id_labels == ("XUID", "Banner ID")
    assert cfg.extra_name_noise == ("cohortx", "msc")
    for bad in (r"^Year \d{4}$", r"^(", r"^(a)(b)$"):
        with pytest.raises(ConfigError, match="INKWELL_GROUP_DIR_REGEX"):
            load_config(_env(tmp_path, INKWELL_GROUP_DIR_REGEX=bad))
    with pytest.raises(ConfigError, match="INKWELL_EXTRA_NAME_NOISE"):
        load_config(_env(tmp_path, INKWELL_EXTRA_NAME_NOISE="ok, not ok!"))


def test_extra_id_labels() -> None:
    default = LabeledFieldRecognizer()
    extra = LabeledFieldRecognizer(extra_id_labels=("XUID",))
    text = "XUID 4412345 assigned"
    assert not [r for r in default.analyze(text, ["STUDENT_ID"])]
    hits = extra.analyze(text, ["STUDENT_ID"])
    assert [text[h.start : h.end] for h in hits] == ["4412345"]


def test_extra_name_noise() -> None:
    try:
        assert "Cohortx" in name_variants("Roe Jane Cohortx")[1]
        recognizers.configure_name_noise(["cohortx"])
        assert name_variants("Roe Jane Cohortx")[1] == {"Roe", "Jane"}
    finally:
        recognizers.configure_name_noise([])
    assert "Cohortx" in name_variants("Roe Jane Cohortx")[1]


def test_custom_group_dir_pattern(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "registry.csv", group_dir_regex=r"^Year (\d{4})$")
    pm = PseudonymMap(tmp_path / "key.csv", reg)
    f = pm.file_path(PurePosixPath("Year 1990/Roe, Jane/letter.pdf"), "h")
    sid = next(iter(reg.students))
    assert str(f).startswith(f"Year 1990/{sid}/D-")
    assert reg.students[sid].class_years == {"1990"}
    reg.save()
    header = (tmp_path / "registry.csv").read_text().splitlines()[0]
    assert header.split(",") == [
        "student_id",
        "canonical_name",
        "aliases",
        "class_years",
        "folders",
        "institution_id",
        "needs_review",
    ]
