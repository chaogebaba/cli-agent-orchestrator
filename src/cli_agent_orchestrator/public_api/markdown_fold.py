"""Per-document Markdown analysis (P5/P6 structure, P9 citations, P10 branches).

``cao fold FILE`` stays on base ``cao`` as a generic transactional Markdown
editor; *corpus discovery* — which documents a bridge repository considers its
pinned corpus — is skill knowledge and lives in
``cli/orchestrator_commands/fold_corpus.py`` (wp-arch-modular-core A.4).  The
skill command needs the per-file analysis that discovery drives, and before
F1004 (#852) it reached four underscore-prefixed helpers of
``services.fold_service`` to get it.  :func:`analyze_document` is that one
operation, published: read one Markdown file, return its three findings sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from cli_agent_orchestrator.services.fold_service import (
    FoldUsageError,
    P9Report,
    P10Report,
    P10StatusCounts,
    RepoMapping,
    _analyze_p9,
    _analyze_p10,
    _parse_structure,
    _read_markdown,
)

__all__ = [
    "DocumentAnalysis",
    "FoldUsageError",
    "P9Report",
    "P10Report",
    "P10StatusCounts",
    "RepoMapping",
    "analyze_document",
]


@dataclass(frozen=True)
class DocumentAnalysis:
    """What one Markdown document yields: structural violations, P9 and P10.

    ``violations`` carries the P5/P6 structural messages already rendered as
    strings — the parser's own violation objects are internal.
    """

    violations: tuple[str, ...]
    p9: P9Report
    p10: P10Report


def analyze_document(
    path: Path,
    display_path: str,
    repos: Sequence[RepoMapping] = (),
) -> DocumentAnalysis:
    """Analyse one Markdown file. No writes, no discovery, no default paths.

    ``path`` is read as-is; ``display_path`` is the string the reports quote, so
    the caller owns how a finding is addressed (a corpus quotes repo-relative
    paths, ``cao fold --check`` quotes the path it was given).  ``repos`` are the
    P9 citation mappings.  Raises :class:`FoldUsageError` if the file cannot be
    read or is not UTF-8.
    """
    data = _read_markdown(path)
    structure = _parse_structure(data)
    return DocumentAnalysis(
        violations=tuple(violation.message for violation in structure.violations),
        p9=_analyze_p9(data, display_path, repos),
        p10=_analyze_p10(data, display_path),
    )
