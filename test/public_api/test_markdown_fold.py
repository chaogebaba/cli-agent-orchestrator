"""F1004 (#852) — ``analyze_document`` is the published per-document analysis.

The corpus command used to call four private helpers of ``services.fold_service`` in
sequence. The public entry must be that sequence and nothing else: same violations, same
P9 report, same P10 report. The equivalence is asserted against ``fold_service.check_file``,
the other caller of the same four helpers, so a change to any of them moves both sides
together or the test fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.public_api.markdown_fold import (
    DocumentAnalysis,
    FoldUsageError,
    analyze_document,
)
from cli_agent_orchestrator.services import fold_service

DOCUMENT = """# Title

Some prose citing `fold_service.py:1`.

| a | b |
|---|---|
| 1 | 2 |

```python
# @branch:one
if True:
    pass
```
"""


@pytest.fixture()
def document(tmp_path: Path) -> Path:
    path = tmp_path / "doc.md"
    path.write_text(DOCUMENT, encoding="utf-8")
    return path


def test_analyze_document_matches_check_file(document: Path) -> None:
    analysis = analyze_document(document, str(document))
    reference = fold_service.check_file(document)

    assert isinstance(analysis, DocumentAnalysis)
    assert analysis.violations == reference.violations
    assert analysis.p9 == reference.p9
    assert analysis.p10 == reference.p10


def test_display_path_is_the_callers_operand(document: Path) -> None:
    """The corpus quotes repo-relative paths; the API must not impose its own."""
    analysis = analyze_document(document, "orchestrator/blueprints/doc.md")
    assert analysis.p9.path == "orchestrator/blueprints/doc.md"
    assert analysis.p10.path == "orchestrator/blueprints/doc.md"


def test_no_discovery_and_no_default_path(document: Path) -> None:
    """Every target is an explicit operand: a missing file raises, it does not search."""
    with pytest.raises(FoldUsageError):
        analyze_document(document.with_name("absent.md"), "absent.md")


def test_invalid_utf8_is_a_usage_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_bytes(b"# Title\n\xff\xfe not utf-8\n")
    with pytest.raises(FoldUsageError):
        analyze_document(path, "bad.md")
