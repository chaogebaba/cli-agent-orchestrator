"""Corpus-derived Codex statusline leak checks for lifecycle E2E tests."""

import re
from pathlib import Path

from cli_agent_orchestrator.providers.codex import _has_tui_footer_in_tail, strip_terminal_escapes

CORPUS_DIR = Path(__file__).parents[1] / "providers" / "fixtures" / "codex_statusline_corpus"
_DYNAMIC_STATUS_PATTERNS = (
    re.compile(r"(?i)context\s+\d+%\s+left"),
    re.compile(r"(?i)\d+%\s+(?:context\s+)?left"),
    re.compile(r"(?i)(?:\d+\s*h(?:\s+\d+\s*m)?|\d+\s*m)\s+left"),
)


def corpus_footer_rows() -> tuple[str, ...]:
    """Return rendered footer rows from every corpus variant that has one."""
    rows: set[str] = set()
    for capture in CORPUS_DIR.glob("*.plain.txt"):
        screen = capture.read_text(encoding="utf-8").splitlines()
        if not _has_tui_footer_in_tail(screen):
            continue
        last = next(row for row in reversed(screen) if row.strip())
        rows.add(strip_terminal_escapes(last).strip())
    return tuple(sorted(rows))


def assert_no_codex_statusline_leak(output: str) -> None:
    """Reject exact corpus rows and dynamic token variants in extracted output."""
    leaked_rows = [row for row in corpus_footer_rows() if row in output]
    assert not leaked_rows, f"Codex statusline leaked into output: {leaked_rows!r}"
    for pattern in _DYNAMIC_STATUS_PATTERNS:
        assert pattern.search(output) is None, "Codex statusline token leaked into output"
