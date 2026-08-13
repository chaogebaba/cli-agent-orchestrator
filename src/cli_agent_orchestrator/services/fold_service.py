"""Transactional, post-condition-checked Markdown edits for ``cao fold``."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import tokenize
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from markdown_it import MarkdownIt
from markdown_it.token import Token

Operation = Literal["replace", "after", "strike"]
FootprintKind = Literal["span", "point"]
ValidatorKind = Literal["P5", "P6"]
P10Classification = Literal["DEFECT", "HYGIENE"]
P9Classification = Literal["DEFECT", "HYGIENE"]


class FoldUsageError(ValueError):
    """The requested edit is malformed or has conflicting footprints."""


class FoldPostconditionError(RuntimeError):
    """The candidate buffer failed one or more post-conditions."""

    def __init__(self, messages: Sequence[str]) -> None:
        self.messages = tuple(messages)
        super().__init__("\n".join(self.messages))


@dataclass(frozen=True)
class FoldHunk:
    anchor: str
    operation: Operation
    value: str | None = None
    expect_count: int = 1


@dataclass(frozen=True)
class FoldResult:
    old_sha: str
    new_sha: str
    violations: tuple[str, ...] = ()
    check_only: bool = False
    p9: P9Report | None = None
    p10: P10Report | None = None


@dataclass(frozen=True)
class RepoMapping:
    name: str
    path: Path


@dataclass(frozen=True)
class P9Finding:
    classification: P9Classification
    kind: str
    path: str
    line: int
    detail: str
    offset: int

    def render(self) -> str:
        return f"P9 {self.classification} {self.path}:{self.line} {self.kind} {self.detail}"


@dataclass(frozen=True)
class P9Status:
    kind: str
    detail: str


@dataclass(frozen=True)
class P9Report:
    path: str
    population_eligible: bool
    findings: tuple[P9Finding, ...]
    statuses: tuple[P9Status, ...]
    used_mappings: frozenset[str]
    basename_resolved: int = 0
    ambiguous_basename: int = 0

    @property
    def defect_count(self) -> int:
        return sum(finding.classification == "DEFECT" for finding in self.findings)

    @property
    def ambiguous_adjacency(self) -> int:
        return sum(finding.kind == "AMBIGUOUS-ADJACENCY" for finding in self.findings)

    def render_lines(self) -> tuple[str, ...]:
        findings = tuple(finding.render() for finding in self.findings)
        statuses = tuple(
            f"P9 STATUS {status.kind} {self.path} {status.detail}" for status in self.statuses
        )
        return findings + statuses


@dataclass(frozen=True)
class P10Finding:
    classification: P10Classification
    kind: str
    path: str
    line: int
    branch_id: str | None = None
    detail: str = ""

    def render(self) -> str:
        if self.classification == "DEFECT":
            assert self.branch_id is not None
            return f"P10 DEFECT {self.path}:{self.line} branch:{self.branch_id} " f"{self.kind}"
        return f"P10 HYGIENE {self.path} {self.kind} {self.detail}"


@dataclass(frozen=True)
class P10StatusCounts:
    skipped: int = 0
    undeclared: int = 0
    no_parser: int = 0
    unparseable: int = 0
    ineligible: int = 0


@dataclass(frozen=True)
class P10Report:
    path: str
    population_eligible: bool
    covered: bool
    findings: tuple[P10Finding, ...]
    statuses: tuple[str, ...]
    status_counts: P10StatusCounts

    @property
    def defect_count(self) -> int:
        return sum(finding.classification == "DEFECT" for finding in self.findings)

    def render_lines(self) -> tuple[str, ...]:
        findings = tuple(finding.render() for finding in self.findings)
        statuses = tuple(f"P10 {status} {self.path}" for status in self.statuses)
        return findings + statuses


@dataclass(frozen=True)
class FoldCorpusResult:
    violations: tuple[str, ...]
    p9_reports: tuple[P9Report, ...]
    p9_unused_mappings: tuple[RepoMapping, ...]
    p10_reports: tuple[P10Report, ...]

    @property
    def p9_summary_lines(self) -> tuple[str, ...]:
        population = sum(report.population_eligible for report in self.p9_reports)
        defects = sum(report.defect_count for report in self.p9_reports)
        ambiguous = sum(report.ambiguous_basename for report in self.p9_reports)
        resolved = sum(report.basename_resolved for report in self.p9_reports)
        adjacency = sum(report.ambiguous_adjacency for report in self.p9_reports)
        denominator = ambiguous + resolved
        rate = (100.0 * ambiguous / denominator) if denominator else 0.0
        return (
            f"P9 POPULATION: {population}",
            f"P9 COVERAGE: {population}/{population} graded",
            f"P9 DENOMINATOR: {defects} path-missing defect firings",
            f"P9 HYGIENE: ambiguous-basename={ambiguous}/{denominator} ({rate:.4f}%) "
            f"ambiguous-adjacency={adjacency}",
        )

    @property
    def p9_unused_mapping_lines(self) -> tuple[str, ...]:
        return tuple(
            f"P9 STATUS MAPPING-UNUSED - {mapping.name}={mapping.path}"
            for mapping in self.p9_unused_mappings
        )

    @property
    def p10_summary_lines(self) -> tuple[str, str, str, str]:
        population = sum(report.population_eligible for report in self.p10_reports)
        coverage = sum(report.covered for report in self.p10_reports if report.population_eligible)
        denominator = sum(report.defect_count for report in self.p10_reports)
        counts = P10StatusCounts(
            skipped=sum(report.status_counts.skipped for report in self.p10_reports),
            undeclared=sum(report.status_counts.undeclared for report in self.p10_reports),
            no_parser=sum(report.status_counts.no_parser for report in self.p10_reports),
            unparseable=sum(report.status_counts.unparseable for report in self.p10_reports),
            ineligible=sum(report.status_counts.ineligible for report in self.p10_reports),
        )
        return (
            f"P10 POPULATION: {population}",
            f"P10 COVERAGE: {coverage}/{population} annotated",
            f"P10 DENOMINATOR: {denominator} defect firings",
            "P10 STATUS: "
            f"skipped={counts.skipped} undeclared={counts.undeclared} "
            f"no-parser={counts.no_parser} unparseable={counts.unparseable} "
            f"ineligible={counts.ineligible}",
        )


@dataclass
class _Edit:
    edit_id: int
    hunk_index: int
    operation: Operation
    start: int
    end: int
    emitted: bytes
    footprint_kind: FootprintKind
    footprint_start: int
    footprint_end: int
    post_start: int = 0
    post_end: int = 0


@dataclass(frozen=True)
class _Segment:
    post_start: int
    post_end: int
    pre_start: int | None
    pre_end: int | None
    edit_id: int | None


@dataclass(frozen=True)
class _Item:
    start: int
    end: int
    ordinal: int
    identity: tuple[str, ...]
    marker: int | None = None


@dataclass(frozen=True)
class _Container:
    container_id: int
    kind: ValidatorKind
    depth: int | None
    start: int
    end: int
    items: tuple[_Item, ...]

    @property
    def partition(self) -> tuple[ValidatorKind, int | None]:
        return (self.kind, self.depth if self.kind == "P6" else None)


@dataclass(frozen=True)
class _StructuralViolation:
    condition: ValidatorKind
    container_id: int
    item_ordinal: int
    item_start: int
    item_end: int
    identity: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class _Structure:
    containers: tuple[_Container, ...]
    violations: tuple[_StructuralViolation, ...]


@dataclass(frozen=True)
class _P10Fence:
    opener: str
    body: str
    opener_line: int
    body_line: int


@dataclass(frozen=True)
class _P10Token:
    value: str
    line: int


@dataclass(frozen=True)
class _P9Citation:
    raw: str
    cited_path: str
    line: int
    start: int
    end: int


@dataclass(frozen=True)
class _P9Resolution:
    hits: tuple[Path, ...]
    used_mappings: frozenset[str]
    unreadable: bool = False


@dataclass(frozen=True)
class _AcceptanceUnit:
    text: str
    start_line: int


_TOKEN_RE = re.compile(r"§\d+|[\w]+|[^\w\s]", re.UNICODE)
_LIST_MARKER_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<number>\d+)[.)][ \t]+")
_P9_CITATION_RE = re.compile(
    r"`(?P<path>[^`\s]+\.(?:py|md|sh|toml|json|ini|cfg))" r"(?P<location>:\d+(?:-\d+)?)`"
)
_P9_NAME_RE = re.compile(r"`(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\(\))?`")
_P9_SEARCH_EXCLUDES = frozenset(
    {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache", "tmp", "archive"}
)
_P10_ID = r"[A-Za-z0-9_-]+"
_P10_BRANCH_TOKEN_RE = re.compile(rf"(?<![A-Za-z0-9_@])@branch\s*:\s*(?P<id>{_P10_ID})")
_P10_AC_TOKEN_RE = re.compile(rf"(?<![A-Za-z0-9_@])branch\s*:\s*(?P<id>{_P10_ID})")
_P10_BRANCH_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_])@branch\s*:")
_P10_AC_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_@])branch\s*:")
_P10_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.*)$")
_P10_AC_HEADING_RE = re.compile(r"^###\s+AC\d+\b", re.IGNORECASE)
_P10_AC_BOLD_RE = re.compile(r"^\s*\*\*AC\d+\b", re.IGNORECASE)
_P10_AC_ROW_RE = re.compile(r"^\s*\|?\s*AC\d+\s*\|", re.IGNORECASE)
_P10_FENCE_OPEN_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<mark>`{3,}|~{3,})(?P<info>.*)$")
_P10_STATUS_ORDER = (
    "SKIPPED",
    "SKIPPED-UNDECLARED",
    "SKIPPED-NO-PARSER",
    "SKIPPED-UNPARSEABLE",
    "INELIGIBLE",
)


def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "schemas" / "fold_hunks.schema.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FoldUsageError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def parse_hunks_document(data: bytes) -> list[FoldHunk]:
    """Decode and validate the closed stdin document contract."""
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise FoldUsageError(f"stdin is not valid UTF-8: {exc}") from exc
    try:
        document = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except FoldUsageError:
        raise
    except json.JSONDecodeError as exc:
        raise FoldUsageError(f"malformed JSON: {exc.msg} at byte {exc.pos}") from exc

    errors = sorted(Draft202012Validator(_schema()).iter_errors(document), key=_schema_error_key)
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "(root)"
        raise FoldUsageError(f"stdin schema error at {location}: {error.message}")

    assert isinstance(document, dict)
    raw_hunks = document["hunks"]
    assert isinstance(raw_hunks, list)
    hunks: list[FoldHunk] = []
    for raw in raw_hunks:
        assert isinstance(raw, dict)
        operation = cast(
            Operation,
            next(key for key in ("replace", "after", "strike") if key in raw),
        )
        value = raw.get(operation)
        if isinstance(value, str):
            try:
                value.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                raise FoldUsageError(f"hunk operand is not UTF-8 encodable: {exc}") from exc
        hunks.append(
            FoldHunk(
                anchor=raw["anchor"],
                operation=operation,
                value=value if isinstance(value, str) else None,
                expect_count=raw.get("expect_count", 1),
            )
        )
    return hunks


def _schema_error_key(error: Any) -> tuple[list[str], str]:
    return ([str(part) for part in error.absolute_path], error.message)


def fold_file(path: Path, hunks: Sequence[FoldHunk]) -> FoldResult:
    """Apply all hunks to one immutable pre-state and atomically replace the file."""
    _validate_target(path)
    for hunk_index, hunk in enumerate(hunks, start=1):
        if not hunk.anchor:
            raise FoldUsageError(f"hunk {hunk_index}: anchor must not be empty")
        if hunk.expect_count < 1:
            raise FoldUsageError(f"hunk {hunk_index}: expect_count must be at least 1")
    pre = _read_markdown(path)
    old_sha = _sha(pre)
    edits, failures = _resolve_edits(path, pre, hunks)
    if failures:
        raise FoldPostconditionError(failures)
    conflict = _footprint_conflict(edits)
    if conflict is not None:
        raise FoldUsageError(conflict)

    post, segments = _assemble(pre, edits)
    failures.extend(_edit_postconditions(path, pre, edits))
    failures.extend(_structural_delta_failures(path, pre, post, segments, edits))
    if failures:
        raise FoldPostconditionError(failures)

    _atomic_write(path, post)
    return FoldResult(old_sha=old_sha, new_sha=_sha(post))


def check_file(path: Path, repos: Sequence[RepoMapping] = ()) -> FoldResult:
    """Report absolute P5/P6, P9, and P10 findings without editing or failing."""
    _validate_target(path)
    data = _read_markdown(path)
    structure = _parse_structure(data)
    return FoldResult(
        old_sha=_sha(data),
        new_sha=_sha(data),
        violations=tuple(violation.message for violation in structure.violations),
        check_only=True,
        p9=_analyze_p9(data, str(path), repos),
        p10=_analyze_p10(data, str(path)),
    )


def check_corpus(root: Path, repos: Sequence[RepoMapping] = ()) -> FoldCorpusResult:
    """Run file-global checks over the pinned bridge-document corpus."""
    paths = _p10_corpus_paths(root)
    if not paths:
        raise FoldUsageError(
            f"{root}: corpus is empty; expected orchestrator/blueprints/ (or blueprints/), "
            "doctrine/, and orchestrator/GOLDEN-TIPS.md (or GOLDEN-TIPS.md)"
        )
    violations: list[str] = []
    p9_reports: list[P9Report] = []
    reports: list[P10Report] = []
    for path in paths:
        data = _read_markdown(path)
        structure = _parse_structure(data)
        violations.extend(violation.message for violation in structure.violations)
        p9_reports.append(_analyze_p9(data, path.relative_to(root.resolve()).as_posix(), repos))
        reports.append(_analyze_p10(data, path.relative_to(root.resolve()).as_posix()))
    used_mappings = set().union(*(report.used_mappings for report in p9_reports))
    unused = tuple(mapping for mapping in repos if mapping.name not in used_mappings)
    return FoldCorpusResult(tuple(violations), tuple(p9_reports), unused, tuple(reports))


def _p10_corpus_paths(root: Path) -> tuple[Path, ...]:
    # Prefer orchestrator/ sub-layout, fall back to legacy root locations.
    bp_dir = root / "orchestrator" / "blueprints"
    if not bp_dir.is_dir():
        bp_dir = root / "blueprints"
    candidates = list(bp_dir.glob("*.md")) if bp_dir.is_dir() else []
    candidates.extend((root / "doctrine").rglob("*.md"))
    tips = root / "orchestrator" / "GOLDEN-TIPS.md"
    if not tips.is_file():
        tips = root / "GOLDEN-TIPS.md"
    if tips.is_file():
        candidates.append(tips)
    return tuple(sorted({path.resolve() for path in candidates}, key=lambda path: str(path)))


def _analyze_p9(data: bytes, display_path: str, repos: Sequence[RepoMapping]) -> P9Report:
    text = data.decode("utf-8")
    citations = _p9_citations(text)
    if not citations:
        return P9Report(display_path, False, (), (), frozenset())

    mappings = tuple(RepoMapping(mapping.name, mapping.path.resolve()) for mapping in repos)
    if not mappings:
        return P9Report(
            display_path,
            True,
            (),
            (P9Status("SKIPPED-NO-REPO", "no --repo mapping supplied"),),
            frozenset(),
        )

    mapping_by_name = {mapping.name: mapping for mapping in mappings}
    usable = {mapping.name: _p9_mapping_usable(mapping) for mapping in mappings}
    excluded_roots = _p9_excluded_worktree_roots(mappings)
    findings: list[P9Finding] = []
    statuses: dict[str, str] = {}
    used_mappings: set[str] = set()
    basename_resolved = 0
    ambiguous_basename = 0

    for citation in citations:
        bound = _p9_bound_mappings(citation.cited_path, mappings, mapping_by_name)
        used_mappings.update(mapping.name for mapping in bound)
        unavailable = [mapping for mapping in bound if not usable[mapping.name]]
        if unavailable:
            names = ",".join(
                f"{mapping.name}={mapping.path}"
                for mapping in sorted(unavailable, key=lambda item: item.name)
            )
            statuses.setdefault("SKIPPED-NO-REPO", f"unreadable-or-missing mapping {names}")
        available = tuple(mapping for mapping in bound if usable[mapping.name])
        if not available:
            continue

        resolution = _p9_resolve(citation.cited_path, available, excluded_roots, mapping_by_name)
        used_mappings.update(resolution.used_mappings)
        is_basename = "/" not in _p9_relative_citation_path(
            citation.cited_path, available, mapping_by_name
        )
        if not resolution.hits:
            reason = "unreadable-or-missing" if resolution.unreadable else "missing"
            findings.append(
                P9Finding(
                    "DEFECT",
                    "path-missing",
                    display_path,
                    citation.line,
                    f"citation={citation.raw} reason={reason}",
                    citation.start,
                )
            )
            continue
        if len(resolution.hits) == 1:
            if is_basename:
                basename_resolved += 1
            continue

        if is_basename:
            ambiguous_basename += 1
        paths = ",".join(str(path) for path in resolution.hits)
        findings.append(
            P9Finding(
                "HYGIENE",
                "AMBIGUOUS-BASENAME",
                display_path,
                citation.line,
                f"citation={citation.raw} hits={paths}",
                citation.start,
            )
        )

    findings.extend(_p9_adjacency_findings(text, display_path, citations))
    findings.sort(
        key=lambda finding: (
            finding.path,
            finding.line,
            finding.kind,
            finding.offset,
            finding.detail,
        )
    )
    return P9Report(
        display_path,
        True,
        tuple(findings),
        tuple(P9Status(kind, detail) for kind, detail in sorted(statuses.items())),
        frozenset(used_mappings),
        basename_resolved,
        ambiguous_basename,
    )


def _p9_citations(text: str) -> tuple[_P9Citation, ...]:
    return tuple(
        _P9Citation(
            raw=match.group(0)[1:-1],
            cited_path=match.group("path"),
            line=text.count("\n", 0, match.start()) + 1,
            start=match.start(),
            end=match.end(),
        )
        for match in _P9_CITATION_RE.finditer(text)
    )


def _p9_bound_mappings(
    cited_path: str, mappings: Sequence[RepoMapping], mapping_by_name: dict[str, RepoMapping]
) -> tuple[RepoMapping, ...]:
    first, _separator, _rest = cited_path.partition("/")
    explicit = mapping_by_name.get(first)
    if explicit is not None:
        return (explicit,)
    if len(mappings) == 1:
        return tuple(mappings)
    return tuple(mappings)


def _p9_relative_citation_path(
    cited_path: str, mappings: Sequence[RepoMapping], mapping_by_name: dict[str, RepoMapping]
) -> str:
    first, separator, rest = cited_path.partition("/")
    if separator and first in mapping_by_name:
        return rest
    return cited_path


def _p9_mapping_usable(mapping: RepoMapping) -> bool:
    return mapping.path.is_dir() and os.access(mapping.path, os.R_OK | os.X_OK)


def _p9_resolve(
    cited_path: str,
    mappings: Sequence[RepoMapping],
    excluded_roots: frozenset[Path],
    mapping_by_name: dict[str, RepoMapping],
) -> _P9Resolution:
    relative = _p9_relative_citation_path(cited_path, mappings, mapping_by_name)
    is_basename = "/" not in relative
    hits: dict[Path, None] = {}
    unreadable = False
    for mapping in mappings:
        if is_basename:
            candidates, candidate_unreadable = _p9_search_basename(
                mapping.path, relative, excluded_roots
            )
        else:
            candidates, candidate_unreadable = _p9_resolve_qualified(mapping.path, relative)
        hits.update((candidate, None) for candidate in candidates)
        unreadable = unreadable or candidate_unreadable
    return _P9Resolution(
        tuple(sorted(hits, key=str)), frozenset(mapping.name for mapping in mappings), unreadable
    )


def _p9_resolve_qualified(root: Path, relative: str) -> tuple[tuple[Path, ...], bool]:
    direct, unreadable = _p9_readable_file(root / relative)
    if direct is not None:
        return (direct,), unreadable
    package, package_unreadable = _p9_readable_file(
        root / "src" / "cli_agent_orchestrator" / relative
    )
    return ((package,) if package is not None else (), unreadable or package_unreadable)


def _p9_search_basename(
    root: Path, basename: str, excluded_roots: frozenset[Path]
) -> tuple[tuple[Path, ...], bool]:
    hits: list[Path] = []
    unreadable = False
    root = root.resolve()

    def onerror(_error: OSError) -> None:
        nonlocal unreadable
        unreadable = True

    for directory, directories, files in os.walk(
        root, topdown=True, followlinks=False, onerror=onerror
    ):
        current = Path(directory)
        directories[:] = [
            name
            for name in directories
            if name not in _P9_SEARCH_EXCLUDES
            and not _p9_excluded_directory(current / name, excluded_roots)
        ]
        if basename not in files:
            continue
        candidate, candidate_unreadable = _p9_readable_file(current / basename)
        unreadable = unreadable or candidate_unreadable
        if candidate is not None:
            hits.append(candidate)
    return tuple(hits), unreadable


def _p9_readable_file(candidate: Path) -> tuple[Path | None, bool]:
    try:
        if candidate.is_symlink() or not candidate.exists() or not candidate.is_file():
            return None, False
        if not os.access(candidate, os.R_OK):
            return None, True
        return candidate.resolve(), False
    except OSError:
        return None, True


def _p9_excluded_directory(candidate: Path, excluded_roots: frozenset[Path]) -> bool:
    try:
        return candidate.resolve() in excluded_roots
    except OSError:
        return False


def _p9_excluded_worktree_roots(mappings: Sequence[RepoMapping]) -> frozenset[Path]:
    mapping_roots = frozenset(mapping.path.resolve() for mapping in mappings)
    roots: set[Path] = set()
    for mapping in mappings:
        roots.update(_p9_git_worktree_roots(mapping.path))
    return frozenset(root for root in roots if root not in mapping_roots)


def _p9_git_worktree_roots(root: Path) -> tuple[Path, ...]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return ()
    if result.returncode != 0:
        return ()
    return tuple(
        Path(line.removeprefix("worktree ")).resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    )


def _p9_adjacency_findings(
    text: str, display_path: str, citations: Sequence[_P9Citation]
) -> list[P9Finding]:
    findings: list[P9Finding] = []
    by_line: dict[int, list[_P9Citation]] = defaultdict(list)
    for citation in citations:
        by_line[citation.line].append(citation)
    for match in _P9_NAME_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        candidates = by_line.get(line, [])
        if len(candidates) < 2:
            continue
        distances = [
            (
                (
                    citation.start - match.end()
                    if citation.start >= match.end()
                    else match.start() - citation.end
                ),
                citation,
            )
            for citation in candidates
        ]
        minimum = min(distance for distance, _citation in distances)
        tied = [citation for distance, citation in distances if distance == minimum]
        if len(tied) < 2:
            continue
        rendered = ",".join(citation.raw for citation in tied)
        findings.append(
            P9Finding(
                "HYGIENE",
                "AMBIGUOUS-ADJACENCY",
                display_path,
                line,
                f"name={match.group('name')} citations={rendered}",
                match.start(),
            )
        )
    return findings


def _analyze_p10(data: bytes, display_path: str) -> P10Report:
    text = data.decode("utf-8")
    fences = _p10_fences(text)
    sections = _p10_acceptance_sections(text)
    units = _p10_acceptance_units(text, sections)
    eligible = bool(fences and sections and units)
    if not eligible:
        return P10Report(
            path=display_path,
            population_eligible=False,
            covered=False,
            findings=(),
            statuses=("INELIGIBLE",),
            status_counts=P10StatusCounts(ineligible=1),
        )

    selected = [fence for fence in fences if _p10_branch_comment_prefixes(fence.body)]
    if not selected:
        return P10Report(
            path=display_path,
            population_eligible=True,
            covered=False,
            findings=(),
            statuses=("SKIPPED",),
            status_counts=P10StatusCounts(skipped=1),
        )

    findings: list[P10Finding] = []
    branch_tokens: list[_P10Token] = []
    skipped_lexical_ids: set[str] = set()
    status_kinds: list[str] = []
    undeclared = 0
    no_parser = 0
    unparseable = 0

    for fence_number, fence in enumerate(selected, start=1):
        lexical_tokens = _p10_branch_comment_tokens(fence.body, fence.body_line)
        findings.extend(
            _p10_branch_comment_malformed_findings(
                fence.body,
                fence.body_line,
                display_path,
                f"fence={fence_number} side=branch",
            )
        )
        declared, language = _p10_fence_metadata(fence.opener)
        if language is None:
            undeclared += 1
            status_kinds.append("SKIPPED-UNDECLARED")
            skipped_lexical_ids.update(token.value for token in lexical_tokens)
            continue
        if language != "python":
            no_parser += 1
            status_kinds.append("SKIPPED-NO-PARSER")
            skipped_lexical_ids.update(token.value for token in lexical_tokens)
            continue
        try:
            attached, parsed_count, pairing_errors = _p10_python_branches(fence)
        except SyntaxError:
            unparseable += 1
            status_kinds.append("SKIPPED-UNPARSEABLE")
            skipped_lexical_ids.update(token.value for token in lexical_tokens)
            continue
        branch_tokens.extend(attached)
        if declared != len(attached) or declared != parsed_count or pairing_errors:
            detail = (
                f"fence={fence_number} declared={declared if declared is not None else '-'} "
                f"annotated={len(attached)} parsed={parsed_count}"
            )
            if pairing_errors:
                detail += " pairing=" + ";".join(pairing_errors)
            findings.append(
                P10Finding(
                    "HYGIENE",
                    "branches-mismatch",
                    display_path,
                    fence.opener_line,
                    detail=detail,
                )
            )

    ac_tokens: list[_P10Token] = []
    for unit in units:
        ac_tokens.extend(_p10_tokens(unit.text, _P10_AC_TOKEN_RE, unit.start_line))
        findings.extend(
            _p10_malformed_findings(
                unit.text,
                _P10_AC_PREFIX_RE,
                _P10_AC_TOKEN_RE,
                unit.start_line,
                display_path,
                "side=AC",
            )
        )

    findings.extend(_p10_duplicate_findings(branch_tokens, "branch", display_path))
    findings.extend(_p10_duplicate_findings(ac_tokens, "AC", display_path))

    branch_by_id = _p10_first_by_id(branch_tokens)
    ac_by_id = _p10_first_by_id(ac_tokens)
    branch_ids = set(branch_by_id)
    ac_ids = set(ac_by_id)
    for branch_id in branch_ids - ac_ids:
        findings.append(
            P10Finding(
                "DEFECT",
                "no-AC",
                display_path,
                branch_by_id[branch_id].line,
                branch_id=branch_id,
            )
        )
    skipped_only = skipped_lexical_ids - branch_ids
    for branch_id in ac_ids - branch_ids - skipped_only:
        findings.append(
            P10Finding(
                "DEFECT",
                "no-branch",
                display_path,
                ac_by_id[branch_id].line,
                branch_id=branch_id,
            )
        )

    findings.sort(
        key=lambda finding: (
            finding.path,
            finding.line,
            finding.branch_id or "",
            finding.kind,
            finding.detail,
        )
    )
    statuses = tuple(kind for kind in _P10_STATUS_ORDER if kind in status_kinds)
    return P10Report(
        path=display_path,
        population_eligible=True,
        covered=True,
        findings=tuple(findings),
        statuses=statuses,
        status_counts=P10StatusCounts(
            undeclared=undeclared,
            no_parser=no_parser,
            unparseable=unparseable,
        ),
    )


def _p10_fences(text: str) -> list[_P10Fence]:
    lines = text.splitlines(keepends=True)
    fences: list[_P10Fence] = []
    index = 0
    while index < len(lines):
        match = _P10_FENCE_OPEN_RE.match(lines[index].rstrip("\r\n"))
        if match is None:
            index += 1
            continue
        marker = match.group("mark")
        marker_char = marker[0]
        marker_length = len(marker)
        opener_index = index
        index += 1
        body_start = index
        while index < len(lines):
            stripped = lines[index].rstrip("\r\n")
            close = re.match(
                rf"^[ \t]*{re.escape(marker_char)}{{{marker_length},}}[ \t]*$", stripped
            )
            if close is not None:
                fences.append(
                    _P10Fence(
                        opener=lines[opener_index],
                        body="".join(lines[body_start:index]),
                        opener_line=opener_index + 1,
                        body_line=body_start + 1,
                    )
                )
                index += 1
                break
            index += 1
    return fences


def _p10_acceptance_sections(text: str) -> list[tuple[int, int, int]]:
    lines = text.splitlines(keepends=True)
    sections: list[tuple[int, int, int]] = []
    for start, line in enumerate(lines):
        match = _P10_HEADING_RE.match(line.rstrip("\r\n"))
        if match is None or "acceptance" not in match.group("title").lower():
            continue
        level = len(match.group("marks"))
        end = len(lines)
        for cursor in range(start + 1, len(lines)):
            heading = _P10_HEADING_RE.match(lines[cursor].rstrip("\r\n"))
            if heading is not None and len(heading.group("marks")) <= level:
                end = cursor
                break
        sections.append((start, end, level))
    return sections


def _p10_acceptance_units(
    text: str, sections: Sequence[tuple[int, int, int]]
) -> list[_AcceptanceUnit]:
    lines = text.splitlines(keepends=True)
    units: list[_AcceptanceUnit] = []
    seen: set[tuple[int, int]] = set()
    for start, end, level in sections:
        cursor = start + 1
        while cursor < end:
            raw = lines[cursor].rstrip("\r\n")
            if _P10_AC_ROW_RE.match(raw):
                extent = (cursor, cursor + 1)
                if extent not in seen:
                    units.append(_AcceptanceUnit(lines[cursor], cursor + 1))
                    seen.add(extent)
                cursor += 1
                continue
            if not (_P10_AC_HEADING_RE.match(raw) or _P10_AC_BOLD_RE.match(raw)):
                cursor += 1
                continue
            unit_start = cursor
            cursor += 1
            while cursor < end:
                candidate = lines[cursor].rstrip("\r\n")
                heading = _P10_HEADING_RE.match(candidate)
                if (
                    _P10_AC_ROW_RE.match(candidate)
                    or _P10_AC_HEADING_RE.match(candidate)
                    or _P10_AC_BOLD_RE.match(candidate)
                ):
                    break
                if heading is not None and len(heading.group("marks")) <= level:
                    break
                cursor += 1
            extent = (unit_start, cursor)
            if extent not in seen:
                units.append(_AcceptanceUnit("".join(lines[unit_start:cursor]), unit_start + 1))
                seen.add(extent)
    return units


def _p10_tokens(text: str, pattern: re.Pattern[str], start_line: int) -> list[_P10Token]:
    return [
        _P10Token(match.group("id"), start_line + text.count("\n", 0, match.start()))
        for match in pattern.finditer(text)
    ]


def _p10_python_comments(body: str) -> list[tuple[int, str]]:
    comments: list[tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(body).readline):
            if token.type == tokenize.COMMENT:
                comments.append((token.start[0], token.string))
    except (IndentationError, tokenize.TokenError):
        # Language/parser STATUS is decided later. Comments emitted before a
        # lexical failure still remain valid annotation evidence.
        pass
    return comments


def _p10_branch_comment_prefixes(body: str) -> bool:
    return any(
        _P10_BRANCH_PREFIX_RE.search(comment)
        for _line_number, comment in _p10_python_comments(body)
    )


def _p10_branch_comment_tokens(body: str, start_line: int) -> list[_P10Token]:
    return [
        _P10Token(match.group("id"), start_line + line_number - 1)
        for line_number, comment in _p10_python_comments(body)
        for match in _P10_BRANCH_TOKEN_RE.finditer(comment)
    ]


def _p10_branch_comment_malformed_findings(
    body: str,
    start_line: int,
    display_path: str,
    detail: str,
) -> list[P10Finding]:
    findings: list[P10Finding] = []
    for line_number, comment in _p10_python_comments(body):
        token_starts = {match.start() for match in _P10_BRANCH_TOKEN_RE.finditer(comment)}
        findings.extend(
            P10Finding(
                "HYGIENE",
                "malformed-token",
                display_path,
                start_line + line_number - 1,
                detail=f"{detail} line={start_line + line_number - 1}",
            )
            for match in _P10_BRANCH_PREFIX_RE.finditer(comment)
            if match.start() not in token_starts
        )
    return findings


def _p10_malformed_findings(
    text: str,
    prefix_pattern: re.Pattern[str],
    token_pattern: re.Pattern[str],
    start_line: int,
    display_path: str,
    detail: str,
) -> list[P10Finding]:
    token_starts = {match.start() for match in token_pattern.finditer(text)}
    return [
        P10Finding(
            "HYGIENE",
            "malformed-token",
            display_path,
            start_line + text.count("\n", 0, match.start()),
            detail=f"{detail} line={start_line + text.count(chr(10), 0, match.start())}",
        )
        for match in prefix_pattern.finditer(text)
        if match.start() not in token_starts
    ]


def _p10_fence_metadata(opener: str) -> tuple[int | None, str | None]:
    totals = re.findall(r"@branches\s*:\s*(\d+)", opener)
    languages = re.findall(r"lang\s*=\s*([A-Za-z0-9_-]+)", opener)
    declared = int(totals[0]) if len(totals) == 1 else None
    language = languages[0] if len(languages) == 1 else None
    return declared, language


def _p10_strip_branch_annotations(body: str) -> str:
    return re.sub(
        rf"(?:[ \t]+#?[ \t]*@branch\s*:\s*{_P10_ID})+(?=\r?$)",
        "",
        body,
        flags=re.MULTILINE,
    )


def _p10_python_branches(
    fence: _P10Fence,
) -> tuple[list[_P10Token], int, list[str]]:
    tree = ast.parse(_p10_strip_branch_annotations(fence.body))
    body_lines = fence.body.splitlines()
    if_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.If)]
    header_end_lines = _p10_python_header_end_lines(fence.body, if_nodes)
    elif_ids = {
        id(node.orelse[0])
        for node in if_nodes
        if node.orelse and isinstance(node.orelse[0], ast.If)
    }
    roots = [node for node in if_nodes if id(node) not in elif_ids]
    explicit_else_nodes: list[ast.If] = []
    implicit_roots: list[ast.If] = []
    for root in roots:
        terminal = root
        while terminal.orelse and isinstance(terminal.orelse[0], ast.If):
            terminal = terminal.orelse[0]
        if terminal.orelse:
            explicit_else_nodes.append(terminal)
        else:
            implicit_roots.append(root)

    implicit_ids = {id(root) for root in implicit_roots}
    headers: list[tuple[int, int, bool]] = [
        (node.lineno, header_end_lines[id(node)], id(node) in implicit_ids) for node in if_nodes
    ]
    for node in explicit_else_nodes:
        else_line = _p10_else_line(node, body_lines)
        headers.append((else_line, else_line, False))

    attached: list[_P10Token] = []
    errors: list[str] = []
    comments_by_line: dict[int, list[str]] = defaultdict(list)
    for comment_line, comment in _p10_python_comments(fence.body):
        comments_by_line[comment_line].append(comment)
    for start_line, end_line, is_implicit in sorted(headers, key=lambda item: item[0]):
        tags = [
            (match.group("id"), line_number)
            for line_number in range(start_line, end_line + 1)
            for comment in comments_by_line.get(line_number, ())
            for match in _P10_BRANCH_TOKEN_RE.finditer(comment)
        ]
        ids = [branch_id for branch_id, _line_number in tags]
        expected = 2 if is_implicit else 1
        if len(ids) != expected:
            errors.append(f"line={start_line} expected-tags={expected} observed={len(ids)}")
        if tags:
            attached.append(_P10Token(tags[0][0], fence.body_line + tags[0][1] - 1))
        if is_implicit and len(tags) >= 2:
            expected_fallthrough = f"{ids[0]}-fallthrough"
            if ids[1] == expected_fallthrough:
                attached.append(_P10Token(tags[1][0], fence.body_line + tags[1][1] - 1))
            else:
                errors.append(
                    f"line={start_line} expected={expected_fallthrough} observed={ids[1]}"
                )
        if len(ids) > expected:
            errors.append(f"line={start_line} extra-tags={','.join(ids[expected:])}")

    parsed_count = len(if_nodes) + len(explicit_else_nodes) + len(implicit_roots)
    return attached, parsed_count, errors


def _p10_python_header_end_lines(body: str, nodes: Sequence[ast.If]) -> dict[int, int]:
    tokens = list(tokenize.generate_tokens(io.StringIO(body).readline))
    ends: dict[int, int] = {}
    for node in nodes:
        start = next(
            (
                index
                for index, token in enumerate(tokens)
                if token.type == tokenize.NAME
                and token.string in {"if", "elif"}
                and token.start == (node.lineno, node.col_offset)
            ),
            None,
        )
        if start is None:
            raise SyntaxError("could not locate Python branch header")
        depth = 0
        for token in tokens[start + 1 :]:
            if token.type != tokenize.OP:
                continue
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth -= 1
            elif token.string == ":" and depth == 0:
                ends[id(node)] = token.start[0]
                break
        if id(node) not in ends:
            raise SyntaxError("could not locate Python branch header colon")
    return ends


def _p10_else_line(node: ast.If, lines: Sequence[str]) -> int:
    assert node.orelse and not isinstance(node.orelse[0], ast.If)
    first_else_body_line = node.orelse[0].lineno
    body_end = max((getattr(item, "end_lineno", item.lineno) or item.lineno) for item in node.body)
    header = lines[node.lineno - 1]
    indent_match = re.match(r"^[ \t]*", header)
    assert indent_match is not None
    indent = indent_match.group(0)
    pattern = re.compile(rf"^{re.escape(indent)}else\s*:")
    for line_number in range(body_end + 1, first_else_body_line + 1):
        if pattern.match(lines[line_number - 1]):
            return line_number
    raise SyntaxError("could not locate explicit else header")


def _p10_duplicate_findings(
    tokens: Sequence[_P10Token], side: str, display_path: str
) -> list[P10Finding]:
    grouped: dict[str, list[_P10Token]] = defaultdict(list)
    for token in tokens:
        grouped[token.value].append(token)
    return [
        P10Finding(
            "HYGIENE",
            "duplicate-id",
            display_path,
            occurrences[1].line,
            detail=f"side={side} branch:{value}",
        )
        for value, occurrences in sorted(grouped.items())
        if len(occurrences) > 1
    ]


def _p10_first_by_id(tokens: Sequence[_P10Token]) -> dict[str, _P10Token]:
    result: dict[str, _P10Token] = {}
    for token in tokens:
        result.setdefault(token.value, token)
    return result


def _validate_target(path: Path) -> None:
    if path.suffix.lower() != ".md":
        raise FoldUsageError(f"{path}: cao fold edits UTF-8 Markdown files only")
    if not path.is_file():
        raise FoldUsageError(f"{path}: file does not exist")


def _read_markdown(path: Path) -> bytes:
    try:
        data = path.read_bytes()
        data.decode("utf-8", "strict")
        return data
    except UnicodeDecodeError as exc:
        raise FoldUsageError(f"{path}: file is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise FoldUsageError(f"{path}: cannot read file: {exc}") from exc


def _resolve_edits(
    path: Path, pre: bytes, hunks: Sequence[FoldHunk]
) -> tuple[list[_Edit], list[str]]:
    edits: list[_Edit] = []
    failures: list[str] = []
    edit_id = 0
    for hunk_index, hunk in enumerate(hunks, start=1):
        anchor = hunk.anchor.encode("utf-8")
        matches = _non_overlapping_matches(pre, anchor)
        if len(matches) != hunk.expect_count:
            failures.append(
                f"{path}: P1 failed for hunk {hunk_index}: expected anchor count "
                f"{hunk.expect_count}, observed {len(matches)}; hint: use an exact, "
                "unique anchor or declare --expect-count"
            )
            continue
        for start in matches:
            end = start + len(anchor)
            if hunk.operation == "replace":
                emitted = (hunk.value or "").encode("utf-8")
                footprint_kind: FootprintKind = "span"
                footprint_start, footprint_end = start, end
            elif hunk.operation == "strike":
                emitted = b"~~" + anchor + b"~~"
                footprint_kind = "span"
                footprint_start, footprint_end = start, end
            else:
                emitted = (hunk.value or "").encode("utf-8")
                footprint_kind = "point"
                footprint_start = footprint_end = end
            edits.append(
                _Edit(
                    edit_id=edit_id,
                    hunk_index=hunk_index,
                    operation=hunk.operation,
                    start=start,
                    end=end,
                    emitted=emitted,
                    footprint_kind=footprint_kind,
                    footprint_start=footprint_start,
                    footprint_end=footprint_end,
                )
            )
            edit_id += 1
    return edits, failures


def _non_overlapping_matches(data: bytes, anchor: bytes) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        position = data.find(anchor, cursor)
        if position < 0:
            return positions
        positions.append(position)
        cursor = position + len(anchor)


def _footprint_conflict(edits: Sequence[_Edit]) -> str | None:
    for index, left in enumerate(edits):
        for right in edits[index + 1 :]:
            if left.footprint_kind == right.footprint_kind == "span":
                if max(left.footprint_start, right.footprint_start) < min(
                    left.footprint_end, right.footprint_end
                ):
                    return _conflict_message(left, right, "span interior-overlap")
                continue
            if left.footprint_kind == right.footprint_kind == "point":
                if left.footprint_start == right.footprint_start:
                    return _conflict_message(left, right, "two points at one offset")
                continue
            point = left if left.footprint_kind == "point" else right
            span = right if point is left else left
            if span.footprint_start <= point.footprint_start <= span.footprint_end:
                relation = (
                    "point on span endpoint"
                    if point.footprint_start in {span.footprint_start, span.footprint_end}
                    else "point strictly inside span"
                )
                return _conflict_message(left, right, relation)
    return None


def _conflict_message(left: _Edit, right: _Edit, reason: str) -> str:
    return (
        f"edit-footprint conflict ({reason}) between hunks "
        f"{left.hunk_index} and {right.hunk_index}"
    )


def _assemble(pre: bytes, edits: Sequence[_Edit]) -> tuple[bytes, list[_Segment]]:
    ordered = sorted(
        edits,
        key=lambda edit: (
            edit.footprint_start,
            0 if edit.footprint_kind == "span" else 1,
            edit.edit_id,
        ),
    )
    output = bytearray()
    segments: list[_Segment] = []
    cursor = 0
    for edit in ordered:
        insertion = edit.operation == "after"
        position = edit.end if insertion else edit.start
        if cursor < position:
            start = len(output)
            output.extend(pre[cursor:position])
            segments.append(_Segment(start, len(output), cursor, position, None))
        edit.post_start = len(output)
        output.extend(edit.emitted)
        edit.post_end = len(output)
        if edit.emitted:
            pre_start = edit.start if edit.footprint_kind == "span" else None
            pre_end = edit.end if edit.footprint_kind == "span" else None
            segments.append(
                _Segment(edit.post_start, edit.post_end, pre_start, pre_end, edit.edit_id)
            )
        cursor = position if insertion else edit.end
    if cursor < len(pre):
        start = len(output)
        output.extend(pre[cursor:])
        segments.append(_Segment(start, len(output), cursor, len(pre), None))
    return bytes(output), segments


def _edit_postconditions(path: Path, pre: bytes, edits: Sequence[_Edit]) -> list[str]:
    failures: list[str] = []
    for edit in edits:
        if edit.operation == "replace" and edit.emitted == pre[edit.start : edit.end]:
            failures.append(
                f"{path}: P2 failed for hunk {edit.hunk_index}: expected old text gone at "
                "the matched span, observed it unchanged; hint: supply a real replacement"
            )
        insertion = edit.end if edit.operation == "after" else edit.start
        consumed_end = insertion if edit.operation == "after" else edit.end
        candidate = pre[:insertion] + edit.emitted + pre[consumed_end:]
        for seam in {insertion, insertion + len(edit.emitted)}:
            violation = _seam_violation(candidate, seam)
            if violation is not None:
                failures.append(
                    f"{path}: P3 failed for hunk {edit.hunk_index}: expected an intact seam, "
                    f"observed {violation}; hint: include enough anchor context to own the join"
                )
    return failures


def _seam_violation(data: bytes, seam: int) -> str | None:
    if seam <= 0 or seam >= len(data):
        return None
    text = data.decode("utf-8")
    char_seam = len(data[:seam].decode("utf-8"))
    if not _same_markdown_block(text, char_seam):
        return None
    line_start = text.rfind("\n", 0, char_seam) + 1
    line_end = text.find("\n", char_seam)
    if line_end < 0:
        line_end = len(text)
    left_tokens = _TOKEN_RE.findall(text[line_start:char_seam])
    right_tokens = _TOKEN_RE.findall(text[char_seam:line_end])
    for width in range(min(len(left_tokens), len(right_tokens)), 0, -1):
        repeated = left_tokens[-width:]
        if repeated == right_tokens[:width] and any(_word_token(token) for token in repeated):
            return f"duplicated token sequence {' '.join(repeated)!r} at byte {seam}"
    return None


def _same_markdown_block(text: str, seam: int) -> bool:
    if text[max(0, seam - 2) : seam + 2].count("\n") >= 2:
        return False
    if seam > 0 and text[seam - 1] == "\n":
        right_line = text[seam : text.find("\n", seam) if "\n" in text[seam:] else len(text)]
        if re.match(r"\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|\||```|~~~)", right_line):
            return False
    if seam < len(text) and text[seam] == "\n":
        left_start = text.rfind("\n", 0, seam) + 1
        if re.match(r"\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|\||```|~~~)", text[left_start:seam]):
            return False
    return True


def _word_token(value: str) -> bool:
    return any(character.isalnum() for character in value)


def _structural_delta_failures(
    path: Path,
    pre: bytes,
    post: bytes,
    segments: Sequence[_Segment],
    edits: Sequence[_Edit],
) -> list[str]:
    before = _parse_structure(pre)
    after = _parse_structure(post)
    blocking = _unmapped_post_violations(before, after, segments, edits)
    return [
        f"{path}: {violation.condition} failed: expected no new structural violation, "
        f"observed {violation.message}; hint: repair the edited table/list in the same fold"
        for violation in blocking
    ]


def _parse_structure(data: bytes) -> _Structure:
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(lines)
    parser = MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(text)
    headings = _heading_stacks(tokens)
    containers: list[_Container] = []
    violations: list[_StructuralViolation] = []

    for token_index, token in enumerate(tokens):
        if token.type == "table_open" and token.map is not None:
            container, found = _table_container(
                len(containers), token_index, token, tokens, lines, offsets, headings
            )
            containers.append(container)
            violations.extend(found)
        elif token.type == "ordered_list_open" and token.map is not None:
            depth = _list_depth(tokens, token_index)
            container, found = _list_container(
                len(containers), token_index, token, depth, tokens, lines, offsets, headings
            )
            containers.append(container)
            violations.extend(found)
    return _Structure(tuple(containers), tuple(violations))


def _line_offsets(lines: Sequence[str]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    return offsets


def _heading_stacks(tokens: Sequence[Token]) -> list[tuple[int, tuple[str, ...]]]:
    stack: list[str] = []
    result: list[tuple[int, tuple[str, ...]]] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        level = int(token.tag[1])
        title = tokens[index + 1].content if index + 1 < len(tokens) else ""
        stack = stack[: level - 1]
        stack.append(_normalize(title))
        result.append((token.map[0], tuple(stack)))
    return result


def _heading_at(line: int, headings: Sequence[tuple[int, tuple[str, ...]]]) -> tuple[str, ...]:
    current: tuple[str, ...] = ()
    for heading_line, stack in headings:
        if heading_line > line:
            break
        current = stack
    return current


def _table_container(
    container_id: int,
    token_index: int,
    token: Token,
    tokens: Sequence[Token],
    lines: Sequence[str],
    offsets: Sequence[int],
    headings: Sequence[tuple[int, tuple[str, ...]]],
) -> tuple[_Container, list[_StructuralViolation]]:
    assert token.map is not None
    start_line, end_line = token.map
    row_lines: list[int] = []
    for child in tokens[token_index + 1 :]:
        if child.type == "table_close" and child.level == token.level:
            break
        if child.type == "tr_open" and child.map is not None:
            row_lines.append(child.map[0])
    assert row_lines
    header_cells = _split_table_row(lines[row_lines[0]])
    header_identity = _normalize(" | ".join(header_cells))
    items: list[_Item] = []
    violations: list[_StructuralViolation] = []
    for ordinal, line_number in enumerate(row_lines[1:], start=1):
        raw = lines[line_number]
        cells = _split_table_row(raw)
        identity = (header_identity, _normalize(raw))
        item = _Item(
            start=offsets[line_number],
            end=offsets[line_number + 1],
            ordinal=ordinal,
            identity=identity,
        )
        items.append(item)
        is_lazy_continuation = "|" not in raw
        if len(cells) != len(header_cells) and not is_lazy_continuation:
            violations.append(
                _StructuralViolation(
                    condition="P5",
                    container_id=container_id,
                    item_ordinal=ordinal,
                    item_start=item.start,
                    item_end=item.end,
                    identity=identity,
                    message=(
                        f"table body row {ordinal} has {len(cells)} cells; "
                        f"header has {len(header_cells)}"
                    ),
                )
            )
    return (
        _Container(
            container_id,
            "P5",
            None,
            offsets[start_line],
            offsets[end_line],
            tuple(items),
        ),
        violations,
    )


def _split_table_row(raw: str) -> list[str]:
    line = raw.rstrip("\r\n").strip()
    cells: list[str] = []
    current: list[str] = []
    code_run: int | None = None
    index = 0
    while index < len(line):
        character = line[index]
        if character == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            run = end - index
            if code_run is None:
                code_run = run
            elif code_run == run:
                code_run = None
            current.extend(line[index:end])
            index = end
            continue
        if character == "|" and code_run is None and not _escaped_pipe(line, index):
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        index += 1
    cells.append("".join(current).strip())
    if line.startswith("|"):
        cells = cells[1:]
    if line.endswith("|") and not _escaped_pipe(line, len(line) - 1):
        cells = cells[:-1]
    return cells


def _escaped_pipe(line: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and line[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _list_depth(tokens: Sequence[Token], target: int) -> int:
    depth = 0
    stack: list[str] = []
    for token in tokens[:target]:
        if token.type in {"ordered_list_open", "bullet_list_open"}:
            stack.append(token.type)
        elif token.type in {"ordered_list_close", "bullet_list_close"} and stack:
            stack.pop()
    depth = len(stack)
    return depth


def _list_container(
    container_id: int,
    token_index: int,
    token: Token,
    depth: int,
    tokens: Sequence[Token],
    lines: Sequence[str],
    offsets: Sequence[int],
    headings: Sequence[tuple[int, tuple[str, ...]]],
) -> tuple[_Container, list[_StructuralViolation]]:
    assert token.map is not None
    start_line, end_line = token.map
    heading = _heading_at(start_line, headings)
    items: list[_Item] = []
    for child in tokens[token_index + 1 :]:
        if child.type == "ordered_list_close" and child.level == token.level:
            break
        if (
            child.type == "list_item_open"
            and child.level == token.level + 1
            and child.map is not None
        ):
            line_number = child.map[0]
            marker = _LIST_MARKER_RE.match(lines[line_number])
            if marker is None:
                continue
            number = int(marker.group("number"))
            raw = lines[line_number]
            items.append(
                _Item(
                    start=offsets[child.map[0]],
                    end=offsets[child.map[1]],
                    ordinal=len(items) + 1,
                    identity=heading + (_normalize(raw),),
                    marker=number,
                )
            )
    violations: list[_StructuralViolation] = []
    for previous, current in zip(items, items[1:]):
        assert previous.marker is not None and current.marker is not None
        if current.marker != previous.marker + 1:
            violations.append(
                _StructuralViolation(
                    condition="P6",
                    container_id=container_id,
                    item_ordinal=current.ordinal,
                    item_start=current.start,
                    item_end=current.end,
                    identity=current.identity,
                    message=(
                        f"ordered-list item {current.ordinal} is numbered {current.marker}; "
                        f"expected {previous.marker + 1}"
                    ),
                )
            )
    return (
        _Container(
            container_id,
            "P6",
            depth,
            offsets[start_line],
            offsets[end_line],
            tuple(items),
        ),
        violations,
    )


def _normalize(value: str) -> str:
    collapsed = " ".join(value.split())
    return collapsed.rstrip(".,;:!?")


def _unmapped_post_violations(
    before: _Structure,
    after: _Structure,
    segments: Sequence[_Segment],
    edits: Sequence[_Edit],
) -> list[_StructuralViolation]:
    pre_by_id = {container.container_id: container for container in before.containers}
    post_by_id = {container.container_id: container for container in after.containers}
    edges: dict[tuple[str, int], set[tuple[str, int]]] = defaultdict(set)
    for post in after.containers:
        for pre in before.containers:
            if pre.partition != post.partition:
                continue
            if _post_has_pre_origin(post, pre, segments):
                left = ("pre", pre.container_id)
                right = ("post", post.container_id)
                edges[left].add(right)
                edges[right].add(left)

    violations_by_pre_item = {
        (violation.container_id, violation.item_ordinal): violation
        for violation in before.violations
    }
    blocking: list[_StructuralViolation] = []
    for violation in after.violations:
        component = _component(("post", violation.container_id), edges)
        pre_ids = [node[1] for node in component if node[0] == "pre"]
        post_ids = [node[1] for node in component if node[0] == "post"]
        if len(pre_ids) != 1 or len(post_ids) != 1:
            blocking.append(violation)
            continue
        pre = pre_by_id[pre_ids[0]]
        post = post_by_id[post_ids[0]]
        if not _violation_maps_to_pre(
            violation,
            pre,
            post,
            segments,
            edits,
            violations_by_pre_item,
        ):
            blocking.append(violation)
    return blocking


def _post_has_pre_origin(post: _Container, pre: _Container, segments: Sequence[_Segment]) -> bool:
    for segment in segments:
        if segment.pre_start is None or segment.pre_end is None:
            continue
        origin = _segment_pre_origin(post.start, post.end, segment)
        if origin is not None and _intersects(pre.start, pre.end, *origin):
            return True
    return False


def _component(
    start: tuple[str, int], edges: dict[tuple[str, int], set[tuple[str, int]]]
) -> set[tuple[str, int]]:
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in edges.get(node, set()):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return seen


def _violation_maps_to_pre(
    violation: _StructuralViolation,
    pre: _Container,
    post: _Container,
    segments: Sequence[_Segment],
    edits: Sequence[_Edit],
    pre_violations: dict[tuple[int, int], _StructuralViolation],
) -> bool:
    # Unchanged source bytes retain their exact item origin.
    for segment in segments:
        if segment.edit_id is not None or segment.pre_start is None or segment.pre_end is None:
            continue
        origin = _segment_pre_origin(violation.item_start, violation.item_end, segment)
        if origin is None:
            continue
        for item in pre.items:
            prior = pre_violations.get((pre.container_id, item.ordinal))
            if (
                _intersects(item.start, item.end, *origin)
                and prior is not None
                and prior.identity == violation.identity
            ):
                return True

    # Span replacements pair affected rows/direct items in document order.
    edit_by_id = {edit.edit_id: edit for edit in edits}
    for segment in segments:
        if segment.edit_id is None or not _intersects(
            violation.item_start, violation.item_end, segment.post_start, segment.post_end
        ):
            continue
        edit = edit_by_id[segment.edit_id]
        if edit.footprint_kind != "span":
            continue
        pre_items = [
            item for item in pre.items if _intersects(item.start, item.end, edit.start, edit.end)
        ]
        post_items = [
            item
            for item in post.items
            if _intersects(item.start, item.end, edit.post_start, edit.post_end)
        ]
        for old, new in zip(pre_items, post_items):
            prior = pre_violations.get((pre.container_id, old.ordinal))
            if (
                new.ordinal == violation.item_ordinal
                and prior is not None
                and prior.identity == violation.identity
            ):
                return True
    return False


def _segment_pre_origin(
    post_start: int, post_end: int, segment: _Segment
) -> tuple[int, int] | None:
    overlap_start = max(post_start, segment.post_start)
    overlap_end = min(post_end, segment.post_end)
    if overlap_start >= overlap_end or segment.pre_start is None or segment.pre_end is None:
        return None
    if segment.edit_id is not None:
        return (segment.pre_start, segment.pre_end)
    return (
        segment.pre_start + overlap_start - segment.post_start,
        segment.pre_start + overlap_end - segment.post_start,
    )


def _intersects(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return max(left_start, right_start) < min(left_end, right_end)


def _atomic_write(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode
    directory = path.parent
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=directory, prefix=".cao-fold-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode & 0o7777)
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
