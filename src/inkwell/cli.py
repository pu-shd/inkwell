"""inkwell command line: walk the input tree, scrub in parallel, write a PII-free report.

Exit codes: 0 ok, 1 config/startup error, 2 some files errored or failed the leak check,
3 nothing was processed / selected (silence is not success).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import logging
import multiprocessing as mp
import os
import re
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import Config, ConfigError, load_config
from .permitted import PermittedNames, PermittedPerson, load_permitted, name_key, write_people
from .pseudonym import PseudonymMap
from .registry import Registry

log = logging.getLogger("inkwell")

REPORT_FIELDS = ("pseudonym_path", "kind", "status", "category", "entities", "notes", "seconds")


@dataclass(frozen=True)
class Job:
    src: str  # absolute source path (never logged)
    rel: str  # original relative path (only written to key file)
    pseudo_stem: str  # pseudonymous relative path without extension
    mtime_ns: int
    atime_ns: int
    owner_ids: tuple[str, ...] = ()
    sha256: str = ""
    birth_ns: int | None = None  # creation time; None where the filesystem doesn't record it


@dataclass(frozen=True)
class Mode:
    redact: bool = True
    withhold: bool = True
    dry_run: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {"redact": self.redact, "withhold": self.withhold}


@dataclass
class JobResult:
    rel: str
    pseudo: str
    kind: str
    status: str
    category: str
    entities: dict[str, int]
    notes: list[str]
    keys: list[tuple[str, str]]
    seconds: float
    sha_out: str = ""
    mentions: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------

_W: dict[str, object] = {}


def _init_worker(
    cfg: Config,
    students: list[tuple[str, list[str]]],
    mode: Mode,
    permitted: list[PermittedPerson] | None = None,
) -> None:
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # tesseract: parallelism comes from processes
    logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)
    from .engine import Scrubber
    from .recognizers import configure_name_noise

    configure_name_noise(cfg.extra_name_noise)  # spawned workers start with a fresh module

    _W["cfg"] = cfg
    _W["mode"] = mode
    _W["scrubber"] = Scrubber(
        spacy_model=cfg.spacy_model,
        entities=cfg.entities,
        score_threshold=cfg.score_threshold,
        students=students,
        extra_id_labels=cfg.extra_id_labels,
    )
    if permitted:
        _W["scrubber"].set_permitted(PermittedNames(permitted, cfg.permitted_contacts))  # type: ignore[attr-defined]


def _run_job(job: Job) -> JobResult:
    from .engine import Scrubber
    from .handlers import Ctx, process
    from .leakcheck import find_leaks

    cfg: Config = _W["cfg"]  # type: ignore[assignment]
    sc: Scrubber = _W["scrubber"]  # type: ignore[assignment]
    mode: Mode = _W["mode"]  # type: ignore[assignment]
    t0 = time.monotonic()
    data = Path(job.src).read_bytes()
    ctx = Ctx(
        cfg, sc, job.rel, job.pseudo_stem, owner_ids=job.owner_ids, redact=mode.redact, withhold=mode.withhold
    )
    out = process(PurePosixPath(job.rel).name, data, ctx)
    pseudo = job.pseudo_stem + (out.ext or "")
    notes = list(out.notes)
    status = out.status
    sha_out = ""
    writable = ("redacted", "withheld", "copied")
    if status in writable and out.data is not None:
        # Leak check only where redaction ran; "copied" output is unredacted by request.
        leaks = find_leaks(pseudo, out.data, sc.known) if status == "redacted" else []
        if leaks:
            status = "leak_blocked"
            notes += [f"leak:{x}" for x in leaks]
        else:
            if not mode.dry_run:
                _write(cfg.output_dir / pseudo, out.data, job)
            sha_out = hashlib.sha256(out.data).hexdigest()
    if status not in writable and not mode.dry_run:
        _remove_outputs(cfg, job)  # fail closed: no stale copy from an earlier run survives
    return JobResult(
        job.rel,
        pseudo,
        out.kind,
        status,
        out.category or "",
        dict(out.findings.counts),
        notes,
        out.keys,
        round(time.monotonic() - t0, 2),
        sha_out,
        tuple(sorted(out.findings.mentions)),
    )


def _remove_outputs(cfg: Config, job: Job) -> None:
    d = cfg.output_dir / PurePosixPath(job.pseudo_stem).parent
    if d.is_dir():
        for old in d.glob(PurePosixPath(job.pseudo_stem).name + ".*"):
            old.unlink()


def _write(dest: Path, data: bytes, job: Job) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.utime(tmp, ns=(job.atime_ns, job.mtime_ns))
        os.replace(tmp, dest)
        stem = PurePosixPath(job.pseudo_stem).name
        for old in dest.parent.glob(stem + ".*"):
            if old != dest:
                old.unlink()  # stale output from a run that produced a different type
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def student_table(cfg: Config, registry: Registry) -> list[tuple[str, list[str]]]:
    """(student_id, names) for the recognizer. Loose top-level zips/folders named after a
    person that match no registry entry are still redacted, just without an ID."""
    import re

    table = registry.name_table()
    for p in cfg.input_dir.iterdir():
        if registry.is_group_dir(p.name) or (p.is_file() and p.suffix.lower() != ".zip"):
            continue
        stem = p.stem if p.is_file() else p.name
        if not registry.match_loose(stem):
            table.append(("", [re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)]))  # "JaneRoe" -> "Jane Roe"
    return table


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _birthtime_ns(st: os.stat_result) -> int | None:
    """File creation time (macOS/BSD st_birthtime). Linux stat() doesn't expose it."""
    bt = getattr(st, "st_birthtime", None)
    return None if bt is None else int(bt * 1_000_000_000)


_REL = re.compile(r"^(\d+)([dhw])$")


def parse_when(value: str, now: dt.datetime | None = None) -> dt.datetime:
    """'2019-09-01', '2019-09-01T08:30' (local time unless an offset is given), or a relative
    age: '30d', '12h', '2w' before now."""
    now = now or dt.datetime.now(dt.UTC)
    m = _REL.match(value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        unit_name = {"d": "days", "h": "hours", "w": "weeks"}[unit]
        return now - dt.timedelta(**{unit_name: n})
    try:
        when = dt.datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"not a date, date-time or age like 30d: {value!r}") from exc
    return when if when.tzinfo else when.astimezone()


@dataclass(frozen=True)
class Window:
    """Inclusive 'after', exclusive 'before' bounds on modification/creation time."""

    modified_after: dt.datetime | None = None
    modified_before: dt.datetime | None = None
    created_after: dt.datetime | None = None
    created_before: dt.datetime | None = None

    @property
    def active(self) -> bool:
        return any((self.modified_after, self.modified_before, self.created_after, self.created_before))

    @property
    def uses_created(self) -> bool:
        return self.created_after is not None or self.created_before is not None

    def validate(self) -> None:
        for lo, hi, what in (
            (self.modified_after, self.modified_before, "modified"),
            (self.created_after, self.created_before, "created"),
        ):
            if lo and hi and lo >= hi:
                raise ConfigError(f"--{what}-after must be earlier than --{what}-before")

    def matches(self, job: Job) -> bool:
        mtime = dt.datetime.fromtimestamp(job.mtime_ns / 1e9, dt.UTC)
        if self.modified_after and mtime < self.modified_after:
            return False
        if self.modified_before and mtime >= self.modified_before:
            return False
        if self.uses_created:
            if job.birth_ns is None:
                raise ConfigError(
                    "--created-after/--created-before need file creation time, which this "
                    "filesystem does not record (e.g. Linux/Docker); use --modified-* instead"
                )
            ctime = dt.datetime.fromtimestamp(job.birth_ns / 1e9, dt.UTC)
            if self.created_after and ctime < self.created_after:
                return False
            if self.created_before and ctime >= self.created_before:
                return False
        return True


def plan(cfg: Config, pmap: PseudonymMap) -> list[Job]:
    jobs: list[Job] = []
    for root, dirs, files in os.walk(cfg.input_dir):
        dirs.sort()
        rroot = Path(root)
        for f in sorted(files):
            p = rroot / f
            if p.is_symlink() or not p.is_file():
                continue
            rel = PurePosixPath(p.relative_to(cfg.input_dir).as_posix())
            sha = _sha256(p)
            pseudo = pmap.file_path(rel, sha, still_exists=lambda o: (cfg.input_dir / o).exists())
            st = p.stat()
            owners = tuple(sorted(pmap.owners(str(rel))))
            jobs.append(
                Job(
                    str(p),
                    str(rel),
                    str(pseudo),
                    st.st_mtime_ns,
                    st.st_atime_ns,
                    owners,
                    sha,
                    _birthtime_ns(st),
                )
            )
    return jobs


def _already_done(cfg: Config, job: Job) -> bool:
    d = cfg.output_dir / PurePosixPath(job.pseudo_stem).parent
    stem = PurePosixPath(job.pseudo_stem).name
    if not d.is_dir():
        return False
    for f in d.glob(stem + ".*"):
        if f.name.startswith(stem + ".") and f.stat().st_mtime_ns == job.mtime_ns:
            return True
    return False


def _restore_dir_times(cfg: Config, pmap: PseudonymMap) -> None:
    pairs = []
    for root, _dirs, _ in os.walk(cfg.input_dir):
        rel = PurePosixPath(Path(root).relative_to(cfg.input_dir).as_posix())
        if str(rel) == ".":
            continue
        dest = cfg.output_dir / pmap.dir_path(rel)
        if dest.is_dir():
            st = Path(root).stat()
            pairs.append((len(rel.parts), dest, st))
    for _, dest, st in sorted(pairs, key=lambda x: -x[0]):
        os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _contains_image(name: str, data: bytes, depth: int = 0) -> bool:
    """Containers (email, zip, docx, xlsx) holding an image: OCR'd content can't be re-checked."""
    import io
    import zipfile
    from email import policy
    from email.parser import BytesParser

    from .sniff import image_format, sniff

    if depth > 3:
        return False
    kind = sniff(name, data)
    if kind == "eml":
        msg = BytesParser(policy=policy.compat32).parsebytes(data)
        for part in msg.walk():
            if part.get_content_maintype() == "image":
                return True
            payload = part.get_payload(decode=True) if not part.is_multipart() else None
            if isinstance(payload, bytes) and (
                image_format(payload[:16])
                or (part.get_content_maintype() != "text" and _contains_image("p", payload, depth + 1))
            ):
                return True
        return False
    if kind in ("zip", "docx", "xlsx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                head = z.open(info).read(16)
                if image_format(head):
                    return True
                if head.startswith(b"PK") and _contains_image(info.filename, z.read(info), depth + 1):
                    return True
    return False


def _load_permitted(
    cfg: Config, students: list[tuple[str, list[str]]]
) -> tuple[list[PermittedPerson], list[PermittedPerson]]:
    """Permitted people minus anyone who is a registry person (the registry always wins)."""
    if cfg.permitted_names_file is None:
        return [], []
    from .recognizers import KnownNameRecognizer

    people = load_permitted(cfg.permitted_names_file)
    return PermittedNames.without_students(people, KnownNameRecognizer(students))


def _suggest_job(job: Job) -> tuple[tuple[str, ...], list[tuple[str, tuple[str, ...]]], bool]:
    """(owners, names found, ok). A file that can't be read is skipped and counted, never fatal."""
    try:
        owners, names = _suggest_names(job)
        return owners, names, True
    except Exception:  # noqa: BLE001 - one bad file must not end the pass
        return job.owner_ids, [], False


def _suggest_names(job: Job) -> tuple[tuple[str, ...], list[tuple[str, tuple[str, ...]]]]:
    """Full names in one document: email display names (with their addresses) and PERSON
    entities in its existing text. No OCR, so scanned pages are not considered."""
    from email import policy
    from email.parser import BytesParser
    from email.utils import getaddresses

    from .engine import Scrubber
    from .leakcheck import extract_text
    from .sniff import sniff

    sc: Scrubber = _W["scrubber"]  # type: ignore[assignment]
    name = PurePosixPath(job.rel).name
    data = Path(job.src).read_bytes()
    kind = sniff(name, data)
    found: dict[frozenset[str], tuple[str, set[str]]] = {}

    def add(surface: str, email: str = "") -> None:
        surface = re.sub(r"<[^>]*>?|\S+@\S+", " ", surface)  # header text: drop addresses
        surface = re.sub(r"\s+", " ", surface).strip(" ,;:<>\"'")
        key = name_key(surface)
        if not 2 <= len(key) <= 4 or re.search(r"\d", surface) or sc.known.is_registry_full_name(surface):
            return
        entry = found.setdefault(key, (surface, set()))
        if email:
            entry[1].add(email.lower())

    if kind in ("drop", "unsupported", "image"):
        return job.owner_ids, []
    if kind == "eml":
        msg = BytesParser(policy=policy.compat32).parsebytes(data)
        for part in msg.walk():
            for h in ("from", "to", "cc", "reply-to", "sender"):
                for disp, addr in getaddresses([str(v) for v in part.get_all(h, []) or []]):
                    if disp:
                        add(disp, addr)
    try:
        text = extract_text(name, data)[:300_000]
    except Exception:  # noqa: BLE001 - a suggestion pass must not stop on one bad file
        text = ""
    results = sc.analyze(text)
    known = [(r.start, r.end) for r in results if r.entity_type == "KNOWN_STUDENT_NAME"]
    for r in results:
        if r.entity_type == "PERSON" and not any(s < r.end and e > r.start for s, e in known):
            add(text[r.start : r.end])
    return job.owner_ids, [(surface, tuple(sorted(emails))) for surface, emails in found.values()]


def suggest_permitted(
    cfg: Config, students: list[tuple[str, list[str]]], jobs: list[Job], min_people: int
) -> int:
    """Write permitted-suggestions-<time>.csv (permitted-names format plus evidence columns)
    beside the key file. Nothing is applied automatically; nothing touches the output."""
    agg: dict[frozenset[str], dict[str, object]] = {}
    skipped = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(
        cfg.workers, initializer=_init_worker, initargs=(cfg, students, Mode(dry_run=True))
    ) as pool:
        for owners, names, ok in pool.imap_unordered(_suggest_job, jobs, chunksize=4):
            skipped += not ok
            for surface, addrs in names:
                e = agg.setdefault(
                    name_key(surface),
                    {"surfaces": Counter(), "people": set(), "docs": 0, "emails": Counter()},
                )
                e["surfaces"][surface] += 1  # type: ignore[index]
                e["people"] |= set(owners)  # type: ignore[operator]
                e["docs"] += 1  # type: ignore[operator]
                e["emails"].update(addrs)  # type: ignore[attr-defined]
    rows = []
    for e in agg.values():
        people: set[str] = e["people"]  # type: ignore[assignment]
        if len(people) < min_people:
            continue
        surfaces: Counter[str] = e["surfaces"]  # type: ignore[assignment]
        plain = [s for s, _ in surfaces.most_common() if "," not in s] or [surfaces.most_common(1)[0][0]]
        seen: Counter[str] = e["emails"]  # type: ignore[assignment]
        rows.append(
            [
                plain[0],
                "",
                ";".join(m for m, _ in seen.most_common(3)),
                "",
                str(len(people)),
                str(e["docs"]),
            ]
        )
    rows.sort(key=lambda r: (-int(r[4]), -int(r[5]), r[0]))
    out = cfg.key_file.parent / f"permitted-suggestions-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    write_people(out, rows, extra_header=("evidence_students", "evidence_documents"))
    log.info(
        "suggested %d permitted name(s) seen in the files of >= %d people; review, then point "
        "INKWELL_PERMITTED_NAMES_FILE at it: %s",
        len(rows),
        min_people,
        out,
    )
    if skipped:
        log.warning("%d file(s) could not be read and were skipped; suggestions may be incomplete", skipped)
        return 2
    return 0


def audit(cfg: Config, registry: Registry) -> int:
    """Fail closed on outputs produced by older code: anything that no longer passes the
    current leak check, and every image (no text layer to check), is deleted."""
    from .leakcheck import find_leaks
    from .recognizers import KnownNameRecognizer
    from .sniff import sniff

    if not cfg.output_dir.is_dir():
        log.error("audit: output directory does not exist")
        return 1
    known = KnownNameRecognizer(student_table(cfg, registry))
    checked = 0
    removed: Counter[str] = Counter()
    for p in sorted(cfg.output_dir.rglob("*")):
        if not p.is_file() or p.name.startswith(("_inkwell", ".")) or p.name.endswith(".WITHHELD.txt"):
            continue
        checked += 1
        data = p.read_bytes()
        if sniff(p.name, data) == "image":
            reason = "image"
        elif _contains_image(p.name, data):
            reason = "contains_image"
        else:
            leaks = find_leaks(p.name, data, known)
            reason = leaks[0] if leaks else ""
        if reason:
            p.unlink()
            removed[reason] += 1
    log.info("audit: %d outputs checked; removed for reprocessing: %s", checked, dict(removed))
    if checked == 0:
        log.error("audit: no outputs found")
        return 3
    return 0


def _preflight(cfg: Config) -> None:
    import pytesseract
    import spacy

    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError as exc:
        raise ConfigError("tesseract binary not found on PATH") from exc
    if cfg.tesseract_lang not in pytesseract.get_languages(config=""):
        raise ConfigError(f"tesseract language data missing: {cfg.tesseract_lang}")
    if not spacy.util.is_package(cfg.spacy_model):
        raise ConfigError(f"spaCy model not installed: {cfg.spacy_model}")


WARNINGS = {
    "redact": (
        "REDACTION DISABLED (--no-redact).\n"
        "Documents that were not withheld were copied WITHOUT redaction. This output contains\n"
        "personally identifiable information: names of record subjects and their families, SSNs,\n"
        "ID numbers, addresses, dates and places of birth, faces. Handle it exactly like the original\n"
        "records. Only path names are pseudonymized; no leak check was run on copied files.\n"
    ),
    "withhold": (
        "WITHHOLDING DISABLED (--no-withhold).\n"
        "Records in the withholding profile's protected categories (in the example profile: grades,\n"
        "transcripts, schedules, disciplinary, financial aid, special education) were redacted for\n"
        "identifiers but NOT withheld: their substance is readable and may still identify a person\n"
        "in context. Affected files are marked 'flagged:<category>' in _inkwell_report.csv.\n"
    ),
}
DRY_FIELDS = (
    "original_path",
    "pseudonym_path",
    "pseudonym_is_new",
    "kind",
    "would_status",
    "category",
    "entities",
    "notes",
    "modified",
    "created",
)


def _mode_warnings(mode: Mode) -> list[str]:
    out = []
    if not mode.redact:
        out.append(WARNINGS["redact"])
    if not mode.withhold:
        out.append(WARNINGS["withhold"])
    return out


def _check_mode(cfg: Config, mode: Mode, args: argparse.Namespace, window: Window) -> None:
    """An output folder holds one mode. Switching requires --force over the whole input."""
    f = cfg.output_dir / "_inkwell_mode.json"
    previous = (
        json.loads(f.read_text())
        if f.exists()
        else (
            {"redact": True, "withhold": True}
            if cfg.output_dir.exists() and any(cfg.output_dir.iterdir())
            else None
        )
    )
    if previous is None or previous == mode.as_dict():
        return
    if not args.force or args.limit or window.active:
        raise ConfigError(
            f"output was produced with {previous}, this run asks for {mode.as_dict()}; mixing modes "
            "in one folder is refused. Use --force without --limit/date filters to redo everything, "
            "or choose a different INKWELL_OUTPUT_DIR"
        )


def _private_writer(path: Path) -> tuple[object, csv.Writer]:  # type: ignore[name-defined]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    fh = os.fdopen(fd, "w", newline="", encoding="utf-8")
    return fh, csv.writer(fh)


def _iso(ns: int | None) -> str:
    return (
        "" if ns is None else dt.datetime.fromtimestamp(ns / 1e9).astimezone().isoformat(timespec="seconds")
    )


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="inkwell", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    run = ap.add_argument_group("run modes")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="full simulation (OCR, classification, redaction, leak check) that writes "
        "nothing to the output, key or registry; reports what each file would get",
    )
    run.add_argument(
        "--report",
        metavar="PATH",
        help="dry-run report location (default: dryrun-<time>.csv beside the key file). "
        "It lists ORIGINAL paths, so it must be outside the input and output folders",
    )
    run.add_argument(
        "--plan-only",
        action="store_true",
        help="fast: count selected files per detected type; no processing, nothing written",
    )
    run.add_argument(
        "--audit",
        action="store_true",
        help="re-run the leak check over existing outputs; delete failures and images so "
        "the next run reprocesses them (use after a recognizer fix)",
    )
    run.add_argument(
        "--suggest-permitted",
        action="store_true",
        help="draft a permitted-names list from full names that recur across many people's files "
        "(email display names and existing text, no OCR); written beside the key for review",
    )
    run.add_argument(
        "--min-students",
        type=int,
        default=3,
        metavar="N",
        help="--suggest-permitted: a name must appear in the files of at least N different "
        "registry people (default 3)",
    )
    sel = ap.add_argument_group("selection (WHEN: 2019-09-01, 2019-09-01T08:30, or an age: 30d, 12h, 2w)")
    sel.add_argument("--modified-after", metavar="WHEN", help="only files last modified at/after WHEN")
    sel.add_argument("--modified-before", metavar="WHEN", help="only files last modified before WHEN")
    sel.add_argument("--created-after", metavar="WHEN", help="only files created at/after WHEN (macOS)")
    sel.add_argument("--created-before", metavar="WHEN", help="only files created before WHEN (macOS)")
    sel.add_argument("--limit", type=int, default=0, help="process at most N selected files")
    sel.add_argument("--force", action="store_true", help="reprocess files already scrubbed")
    beh = ap.add_argument_group("behavior (each one alone is weaker; the run warns about its output)")
    beh.add_argument(
        "--no-redact",
        action="store_true",
        help="don't redact: non-withheld documents are copied unchanged (output keeps PII)",
    )
    beh.add_argument(
        "--no-withhold",
        action="store_true",
        help="don't withhold protected records: redact them and flag them in the report",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    mode = Mode(redact=not args.no_redact, withhold=not args.no_withhold, dry_run=args.dry_run)
    try:
        if not mode.redact and not mode.withhold:
            raise ConfigError("--no-redact with --no-withhold would copy every record unprotected; refused")
        if sum(map(bool, (args.dry_run, args.plan_only, args.audit, args.suggest_permitted))) > 1:
            raise ConfigError("choose one of --dry-run, --plan-only, --audit, --suggest-permitted")
        if args.min_students < 2:
            raise ConfigError("--min-students must be at least 2")
        if args.report and not args.dry_run:
            raise ConfigError("--report applies to --dry-run")
        window = Window(
            *(
                parse_when(v) if v else None
                for v in (args.modified_after, args.modified_before, args.created_after, args.created_before)
            )
        )
        window.validate()
        cfg = load_config()
        report_file = None
        if args.dry_run:
            report_file = (
                Path(args.report).expanduser().resolve()
                if args.report
                else (cfg.key_file.parent / f"dryrun-{dt.datetime.now():%Y%m%d-%H%M%S}.csv")
            )
            for d, name in ((cfg.output_dir, "output"), (cfg.input_dir, "input")):
                if report_file == d or d in report_file.parents:
                    raise ConfigError(
                        f"--report must not be inside the {name} folder (it lists original paths)"
                    )
            if report_file.exists():
                raise ConfigError("--report file already exists")
        if not args.plan_only:
            _preflight(cfg)
        from .recognizers import configure_name_noise

        configure_name_noise(cfg.extra_name_noise)
        registry = Registry(cfg.key_file.parent / "registry.csv", cfg.group_dir_regex)
        registry.build(cfg.input_dir)
        pmap = PseudonymMap(cfg.key_file, registry)
        known_before = set(pmap.rows)
        if args.audit:
            return audit(cfg, registry)
        students = student_table(cfg, registry)
        permitted, conflicts = _load_permitted(cfg, students)
        all_jobs = plan(cfg, pmap)
        jobs = [j for j in all_jobs if window.matches(j)]
        if not (args.dry_run or args.plan_only or args.suggest_permitted):
            _check_mode(cfg, mode, args, window)
    except (ConfigError, ValueError) as exc:
        log.error("configuration error: %s", exc)
        return 1

    excluded = len(all_jobs) - len(jobs)
    if window.active:
        log.info("date window selects %d of %d files (%d excluded)", len(jobs), len(all_jobs), excluded)
    if args.limit:
        jobs = jobs[: args.limit]
    if not jobs:
        log.error("no input files selected" if all_jobs else "no input files found")
        return 3
    warnings = _mode_warnings(mode)
    for wtext in warnings:
        log.warning("%s", wtext.replace("\n", " "))

    if conflicts:
        log.warning(
            "%d permitted name(s) are also registry people and will stay redacted%s",
            len(conflicts),
            "" if args.dry_run or args.plan_only else "; listed in permitted-conflicts.csv beside the key",
        )
    if not (args.dry_run or args.plan_only or args.suggest_permitted):
        cfile = cfg.key_file.parent / "permitted-conflicts.csv"
        if conflicts:
            write_people(cfile, ([p.name, ";".join(p.aliases), "", ""] for p in conflicts))
        else:
            cfile.unlink(missing_ok=True)

    if args.suggest_permitted:
        return suggest_permitted(cfg, students, jobs, args.min_students)

    if args.plan_only:
        from .sniff import sniff

        kinds: Counter[str] = Counter()
        for j in jobs:
            with open(j.src, "rb") as fh:
                head = fh.read(8192)
                if head.startswith(b"PK"):  # zip containers need the central directory
                    head += fh.read()
            kinds[sniff(j.rel, head)] += 1
        log.info("plan: %d files selected; by detected kind: %s", len(jobs), dict(kinds))
        log.info("plan: nothing written (output, report, key and registry untouched)")
        return 0

    if args.dry_run:
        todo = jobs
        log.info("DRY RUN: %d files; nothing will be written except the report", len(todo))
    else:
        registry.save()
        pmap.save()  # persist pseudonyms before any output exists
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        (cfg.output_dir / "_inkwell_mode.json").write_text(json.dumps(mode.as_dict()))
        warn_file = cfg.output_dir / "_inkwell_WARNING.txt"
        if warnings:
            warn_file.write_text("\n".join(warnings))
        else:
            warn_file.unlink(missing_ok=True)
        todo = jobs if args.force else [j for j in jobs if not _already_done(cfg, j)]
    log.info("%d files selected, %d to process, %d workers", len(jobs), len(todo), cfg.workers)

    statuses: Counter[str] = Counter()
    entities: Counter[str] = Counter()
    by_rel = {j.rel: j for j in todo}
    if args.dry_run:
        assert report_file is not None
        report_file.parent.mkdir(parents=True, exist_ok=True)
        rf, w = _private_writer(report_file)
        w.writerow(DRY_FIELDS)
    else:
        report_path = cfg.output_dir / "_inkwell_report.csv"
        new_report = not report_path.exists()
        rf = report_path.open("a", newline="", encoding="utf-8")
        w = csv.writer(rf)
        if new_report:
            w.writerow(REPORT_FIELDS)
    ctx = mp.get_context("spawn")
    try:
        with ctx.Pool(
            cfg.workers,
            initializer=_init_worker,
            initargs=(cfg, students, mode, permitted),
            maxtasksperchild=200,
        ) as pool:
            for i, r in enumerate(pool.imap_unordered(_run_job, todo, chunksize=1), 1):
                statuses[r.status] += 1
                entities.update(r.entities)
                notes = ";".join(r.notes)
                if args.dry_run:
                    j = by_rel[r.rel]
                    w.writerow(
                        [
                            r.rel,
                            r.pseudo,
                            "no" if r.rel in known_before else "yes",
                            r.kind,
                            r.status,
                            r.category,
                            json.dumps(r.entities),
                            notes,
                            _iso(j.mtime_ns),
                            _iso(j.birth_ns),
                        ]
                    )
                else:
                    for k, v in r.keys:
                        pmap.record(k, v, pmap.owners(r.rel))
                    pmap.set_result(r.rel, r.sha_out, set(r.mentions))
                    w.writerow(
                        [r.pseudo, r.kind, r.status, r.category, json.dumps(r.entities), notes, r.seconds]
                    )
                rf.flush()  # type: ignore[attr-defined]
                lvl = logging.WARNING if r.status in ("error", "leak_blocked") else logging.INFO
                log.log(
                    lvl,
                    "[%d/%d] %s%s %s %s",
                    i,
                    len(todo),
                    "would be " if args.dry_run else "",
                    r.status,
                    r.kind,
                    r.pseudo,
                )
                if not args.dry_run and i % 50 == 0:
                    pmap.save()
    finally:
        rf.close()  # type: ignore[attr-defined]

    summary = {
        "mode": {**mode.as_dict(), "dry_run": mode.dry_run},
        "planned": len(all_jobs),
        "excluded_by_window": excluded,
        "selected": len(jobs),
        "processed": len(todo),
        "statuses": dict(statuses),
        "entities": dict(entities),
        "permitted_names": len(permitted),
        "permitted_conflicts": len(conflicts),
    }
    if args.dry_run:
        log.info("DRY RUN summary: %s", json.dumps(summary))
        log.info("DRY RUN report (contains original paths; keep it private): %s", report_file)
    else:
        pmap.save()
        _restore_dir_times(cfg, pmap)
        (cfg.output_dir / "_inkwell_summary.json").write_text(json.dumps(summary, indent=2))
        log.info("summary: %s", json.dumps(summary))
    for wtext in warnings:
        log.warning("%s", wtext.replace("\n", " "))
    if todo and sum(statuses.values()) != len(todo):
        log.error("worker results missing: expected %d got %d", len(todo), sum(statuses.values()))
        return 2
    if statuses["error"] or statuses["leak_blocked"]:
        log.warning(
            "%d errors, %d leak-blocked files %s NOT written; see report",
            statuses["error"],
            statuses["leak_blocked"],
            "would be" if args.dry_run else "were",
        )
        return 2
    if not todo:
        log.info("nothing to do: all selected files already scrubbed (use --force to redo)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
