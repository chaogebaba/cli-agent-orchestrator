"""Regression coverage for unanchored Claude WAITING classification."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import (
    ClaudeCodeProvider,
    _is_ink_selection_waiting,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "issue405"
RAW_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "issue405_raw"
WPQ1_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "wpq1_claude_2_1_211"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _provider() -> ClaudeCodeProvider:
    provider = ClaudeCodeProvider("issue405", "session", "window")
    provider._resolve_native_status = lambda: None  # type: ignore[method-assign]
    return provider


@pytest.mark.parametrize(
    ("fixture", "waiting", "uses_geometry"),
    [
        ("01-askuser-single.raw", True, True),
        ("02-askuser-multi.raw", True, True),
        ("03-plan-approval.raw", True, True),
        ("04-trust-prompt.raw", False, False),
        ("05-bypass-prompt.raw", False, False),
    ],
)
def test_byte_exact_pipe_pane_replay_uses_recorded_geometry(fixture, waiting, uses_geometry):
    raw = (RAW_FIXTURES / fixture).read_bytes().decode("utf-8", errors="surrogateescape")
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
            return_value=None,
        ),
        patch("cli_agent_orchestrator.providers.claude_code.get_backend") as backend,
    ):
        backend.return_value.get_pane_size.return_value = (134, 49)
        status = _provider().get_status(raw)

    if uses_geometry:
        backend.return_value.get_pane_size.assert_called_once_with("session", "window")
    else:
        backend.return_value.get_pane_size.assert_not_called()
    assert (status == TerminalStatus.WAITING_USER_ANSWER) is waiting


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("00-askuserquestion-2.1.205-incident.plain.txt", TerminalStatus.WAITING_USER_ANSWER),
        ("01-askuserquestion-2.1.209.plain.txt", TerminalStatus.WAITING_USER_ANSWER),
        ("02-plan-approval-2.1.209.plain.txt", TerminalStatus.WAITING_USER_ANSWER),
        ("03-idle-false-positive-2.1.209.plain.txt", TerminalStatus.COMPLETED),
    ],
)
def test_committed_frames_use_live_rendered_bottom_region(fixture, expected):
    raw = _read(fixture)
    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
        return_value=raw.splitlines(),
    ):
        status = _provider().get_status(raw)

    assert status == expected
    if fixture.startswith("03-"):
        assert status != TerminalStatus.WAITING_USER_ANSWER


def test_cup_split_raw_dialog_reassembles_offline_and_waits():
    raw = (
        "Choose targets\r\n"
        "  1. Alpha\r\n"
        "  2. Beta\r\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\r\n"
        "\x1b[2;1H❯"
    )
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
            return_value=None,
        ),
        patch("cli_agent_orchestrator.providers.claude_code.get_backend") as backend,
    ):
        backend.return_value.get_pane_size.return_value = (80, 10)
        status = _provider().get_status(raw)

    assert status == TerminalStatus.WAITING_USER_ANSWER


def test_poisoned_dialog_history_is_erased_before_classification():
    stale_dialog = (
        "Choose targets\r\n"
        "❯ 1. Alpha\r\n"
        "  2. Beta\r\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\r\n"
    )
    current_idle = "\x1b[2J\x1b[H" + "─" * 30 + "\r\n❯ \r\n" + "─" * 30
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
            return_value=None,
        ),
        patch("cli_agent_orchestrator.providers.claude_code.get_backend") as backend,
    ):
        backend.return_value.get_pane_size.return_value = (80, 10)
        status = _provider().get_status(stale_dialog + current_idle)

    assert status != TerminalStatus.WAITING_USER_ANSWER


def test_live_screen_is_preferred_without_geometry_lookup():
    raw = "❯ 1. Alpha\n  2. Beta\nEnter to select · ↑/↓ to navigate · Esc to cancel"
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
            return_value=raw.splitlines(),
        ),
        patch("cli_agent_orchestrator.providers.claude_code.get_backend") as backend,
    ):
        assert _provider().get_status(raw) == TerminalStatus.WAITING_USER_ANSWER
    backend.assert_not_called()


def test_render_failure_is_nonfatal_uncertain_and_recovers(caplog):
    raw = "❯ 1. Alpha\n  2. Beta\nEnter to select · ↑/↓ to navigate · Esc to cancel"
    provider = _provider()
    caplog.set_level(logging.DEBUG)
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
            side_effect=[None, None, None, raw.splitlines()],
        ),
        patch("cli_agent_orchestrator.providers.claude_code.get_backend") as backend,
    ):
        backend.return_value.get_pane_size.return_value = None
        assert provider.get_status(raw) == TerminalStatus.RENDER_UNCERTAIN
        assert provider.get_status(raw) == TerminalStatus.RENDER_UNCERTAIN
        assert provider.get_status("⏺ done\n❯ ") == TerminalStatus.COMPLETED
        assert provider.get_status(raw) == TerminalStatus.RENDER_UNCERTAIN
        assert provider.get_status(raw) == TerminalStatus.WAITING_USER_ANSWER

    assert backend.return_value.get_pane_size.call_count == 3
    assert caplog.text.count("Claude screen render uncertain") == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("⏺ finished; press Enter to confirm the example\n❯ ", TerminalStatus.COMPLETED),
        ("Instructions: press Enter to confirm the example\n❯ ", TerminalStatus.IDLE),
    ],
)
def test_enter_to_confirm_prose_stays_on_raw_ready_path(raw, expected):
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
            return_value=None,
        ) as rendered,
        patch("cli_agent_orchestrator.providers.claude_code.get_backend") as backend,
    ):
        backend.return_value.get_pane_size.return_value = None
        assert _provider().get_status(raw) == expected

    rendered.assert_not_called()
    backend.return_value.get_pane_size.assert_not_called()


@pytest.mark.parametrize(
    "prose",
    [
        "Enter to select · ↑/↓ to navigate · Esc to cancel",
        "Instructions: press Enter to confirm the example",
    ],
)
def test_screen_ready_output_with_footer_or_enter_prose_stays_completed(prose):
    screen = [
        "● Done — example documented.",
        prose,
        "─" * 60,
        "❯ ",
        "─" * 60,
    ]

    assert _provider().get_status_from_screen(screen) == TerminalStatus.COMPLETED


@pytest.mark.parametrize(
    "fixture",
    [
        "00-askuserquestion-2.1.205-incident.plain.txt",
        "02-plan-approval-2.1.209.plain.txt",
    ],
)
def test_screen_real_dialog_waits_via_each_structural_arm(fixture):
    assert (
        _provider().get_status_from_screen(_read(fixture).splitlines())
        == TerminalStatus.WAITING_USER_ANSWER
    )


def test_non_waiting_raw_status_skips_rendering():
    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen"
    ) as rendered:
        assert _provider().get_status("⏺ done\n❯ ") == TerminalStatus.COMPLETED
    rendered.assert_not_called()


def test_quoted_bottom_dialog_is_documented_limit():
    quoted = (
        "Example dialog chrome:\n"
        "❯ 1. First choice\n"
        "  2. Second choice\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )
    assert _is_ink_selection_waiting(quoted) is True


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("askuser-tab-arrow.txt", TerminalStatus.WAITING_USER_ANSWER),
        ("resume-picker.txt", TerminalStatus.WAITING_USER_ANSWER),
        ("quoted-resume-picker-completed.txt", TerminalStatus.COMPLETED),
        ("completed-composer.txt", TerminalStatus.COMPLETED),
        ("initial-empty-composer.txt", TerminalStatus.IDLE),
    ],
)
def test_wpq1_claude_2_1_211_interactive_screen_roster(fixture, expected):
    rows = (WPQ1_FIXTURES / fixture).read_text(encoding="utf-8").splitlines()
    assert _provider().get_status_from_screen(rows) == expected
