"""Regression tests for captured Codex modal-dialog screens."""

import hashlib
import re
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import (
    TRUST_PROMPT_PATTERN,
    CodexProvider,
    strip_terminal_escapes,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "codex_dialogs"
DIALOG_FIXTURES = [
    "command-approval.ansi.txt",
    "experimental-checkboxes.ansi.txt",
    "hooks-browser.ansi.txt",
    "keymap-browser.ansi.txt",
    "memories-enable.ansi.txt",
    "model-picker.ansi.txt",
    "permissions-picker.ansi.txt",
    "skills-menu.ansi.txt",
    "theme-picker.ansi.txt",
    "trust.ansi.txt",
    "usage-picker-no-reset.ansi.txt",
]
NORMAL_FIXTURES = [
    ("idle.ansi.txt", TerminalStatus.IDLE, "normal-idle"),
    ("composer-draft.ansi.txt", TerminalStatus.IDLE, "normal-draft"),
    ("working.ansi.txt", TerminalStatus.PROCESSING, "normal-working"),
]
QUOTED_TRUST_PROSE = (
    "codex\n"
    "• Reviewed providers/codex.py: the trust check greps for\n"
    '  "allow Codex to work in this folder" across the whole buffer.\n'
    "› \n"
    "  ? for shortcuts                     100% context left\n"
)


def _provider() -> CodexProvider:
    return CodexProvider("term1", "cao-sess", "win")


def _screen(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("fixture_name", DIALOG_FIXTURES)
def test_dialog_screen_is_waiting(fixture_name):
    assert (
        _provider().get_status_from_screen(_screen(fixture_name))
        == TerminalStatus.WAITING_USER_ANSWER
    )


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [(name, expected) for name, expected, _test_id in NORMAL_FIXTURES],
    ids=[test_id for _name, _expected, test_id in NORMAL_FIXTURES],
)
def test_normal_screen_status(fixture_name, expected):
    assert _provider().get_status_from_screen(_screen(fixture_name)) == expected


def test_visible_spinner_wins_over_final_dialog_footer():
    screen = _screen("model-picker.ansi.txt")
    footer_index = next(
        index
        for index in range(len(screen) - 1, -1, -1)
        if strip_terminal_escapes(screen[index]).strip()
    )
    screen.insert(footer_index, "• Working (3s • esc to interrupt)")

    assert _provider().get_status_from_screen(screen) == TerminalStatus.PROCESSING


@pytest.mark.parametrize(
    "text",
    [
        "allow Codex to work in this folder",
        "Do you trust the contents of this directory?",
    ],
)
def test_trust_pattern_matches_both_wordings(text):
    assert re.search(TRUST_PROMPT_PATTERN, text)


def test_quoted_trust_stall_screen_is_not_waiting():
    status = _provider().get_status_from_screen(_screen("quoted-trust-stall.ansi.txt"))

    assert status != TerminalStatus.WAITING_USER_ANSWER
    assert status == TerminalStatus.PROCESSING


def test_quoted_trust_prose_raw_path_is_not_waiting():
    assert _provider().get_status(QUOTED_TRUST_PROSE) != TerminalStatus.WAITING_USER_ANSWER


def test_real_trust_screen_raw_path_is_waiting():
    content = (FIXTURES / "trust.ansi.txt").read_text(encoding="utf-8")

    assert _provider().get_status(content) == TerminalStatus.WAITING_USER_ANSWER


def test_fixture_manifest_hashes():
    entries = [
        line.split(maxsplit=1)
        for line in (FIXTURES / "SHA256SUMS").read_text().splitlines()
    ]
    assert {name for _digest, name in entries} == {
        path.name for path in FIXTURES.glob("*.ansi.txt")
    }
    for expected_digest, name in entries:
        path = FIXTURES / name
        assert path.exists()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_digest
