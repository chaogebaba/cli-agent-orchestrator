"""``cao-orchestrator fold-corpus`` — the relocated ``cao fold --corpus`` check.

Corpus *discovery* is skill knowledge: it globs ``orchestrator/blueprints/*.md``,
``doctrine/**/*.md`` and ``orchestrator/GOLDEN-TIPS.md``. The per-file analysis it
drives is unchanged and stays in infrastructure, reached across the seam through
``public_api.markdown_fold.analyze_document`` (F1004 #852); the discovery, the
corpus aggregate and the command surface live here (wp-arch-modular-core A.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import click

from cli_agent_orchestrator.public_api.markdown_fold import (
    FoldUsageError,
    P9Report,
    P10Report,
    P10StatusCounts,
    RepoMapping,
    analyze_document,
)


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


def corpus_paths(root: Path) -> tuple[Path, ...]:
    """Return the pinned bridge-document corpus under ``root``."""
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


def check_corpus(root: Path, repos: Sequence[RepoMapping] = ()) -> FoldCorpusResult:
    """Run file-global checks over the pinned bridge-document corpus."""
    paths = corpus_paths(root)
    if not paths:
        raise FoldUsageError(
            f"{root}: corpus is empty; expected orchestrator/blueprints/ (or blueprints/), "
            "doctrine/, and orchestrator/GOLDEN-TIPS.md (or GOLDEN-TIPS.md)"
        )
    violations: list[str] = []
    p9_reports: list[P9Report] = []
    reports: list[P10Report] = []
    for path in paths:
        analysis = analyze_document(path, path.relative_to(root.resolve()).as_posix(), repos)
        violations.extend(analysis.violations)
        p9_reports.append(analysis.p9)
        reports.append(analysis.p10)
    used_mappings = set().union(*(report.used_mappings for report in p9_reports))
    unused = tuple(mapping for mapping in repos if mapping.name not in used_mappings)
    return FoldCorpusResult(tuple(violations), tuple(p9_reports), unused, tuple(reports))


def _repo_mappings(specs: tuple[str, ...]) -> tuple[RepoMapping, ...]:
    mappings: dict[str, RepoMapping] = {}
    for spec in specs:
        name, separator, raw_path = spec.partition("=")
        if not separator or not name or not raw_path:
            raise click.UsageError("--repo must have the form NAME=PATH")
        mapping = RepoMapping(name, Path(raw_path).resolve())
        previous = mappings.get(name)
        if previous is not None and previous.path != mapping.path:
            raise click.UsageError(f"duplicate --repo mapping for {name!r}")
        mappings[name] = mapping
    return tuple(mappings[name] for name in sorted(mappings))


@click.command("fold-corpus")
@click.option(
    "--repo",
    "repo_specs",
    multiple=True,
    help="Repository mapping NAME=PATH for P9 citation resolution (repeatable).",
)
def fold_corpus(repo_specs: tuple[str, ...]) -> None:
    """Check the pinned bridge Markdown corpus from the current directory."""
    try:
        repos = _repo_mappings(repo_specs)
        corpus_result = check_corpus(Path.cwd(), repos)
    except FoldUsageError as exc:
        raise click.UsageError(
            f"{Path.cwd()}: expected a valid fold request; observed {exc}; "
            "hint: correct the edit spec and retry"
        ) from exc
    click.echo("skipped: P1, P2, P3 (no edit span under --check)")
    if corpus_result.violations:
        for violation in corpus_result.violations:
            click.echo(violation)
    else:
        click.echo("P5/P6: no violations")
    p9_lines = [line for report in corpus_result.p9_reports for line in report.render_lines()]
    p9_lines.extend(corpus_result.p9_unused_mapping_lines)
    if p9_lines:
        for line in p9_lines:
            click.echo(line)
    else:
        click.echo("P9: no violations")
    for line in corpus_result.p9_summary_lines:
        click.echo(line)
    p10_lines = [line for report in corpus_result.p10_reports for line in report.render_lines()]
    if p10_lines:
        for line in p10_lines:
            click.echo(line)
    else:
        click.echo("P10: no violations")
    for line in corpus_result.p10_summary_lines:
        click.echo(line)
