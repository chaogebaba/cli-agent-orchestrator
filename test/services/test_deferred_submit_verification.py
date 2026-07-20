"""Tests for guarded deferred-init submit verification."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.terminal_service import TerminalInputBlockedError


class TestMessageVisibleInBox:
    def _dependencies(self, draft: str):
        provider = MagicMock()
        provider.composer_parse_accepts_escapes = False
        provider.read_composer_draft.return_value = draft
        backend = MagicMock()
        backend.get_history.return_value = "rendered composer"
        return provider, backend

    def test_true_when_provider_parser_returns_exact_task(self):
        provider, backend = self._dependencies("Analyze the logs")
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w"},
            ),
            patch.object(ts.provider_manager, "get_provider", return_value=provider),
            patch.object(ts, "get_backend", return_value=backend),
        ):
            assert ts._message_visible_in_box("t1", "Analyze the logs") is True

        provider.read_composer_draft.assert_called_once_with(["rendered composer"])

    def test_false_when_composer_contains_foreign_draft(self):
        provider, backend = self._dependencies("Human draft: do not submit")
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w"},
            ),
            patch.object(ts.provider_manager, "get_provider", return_value=provider),
            patch.object(ts, "get_backend", return_value=backend),
        ):
            assert ts._message_visible_in_box("t1", "Analyze the logs") is False

    def test_false_when_message_too_short_without_reading_terminal(self):
        with patch.object(ts, "get_terminal_metadata") as metadata:
            assert ts._message_visible_in_box("t1", "go") is False
        metadata.assert_not_called()

    def test_false_when_capture_raises(self):
        provider, backend = self._dependencies("Analyze the logs")
        backend.get_history.side_effect = RuntimeError("boom")
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w"},
            ),
            patch.object(ts.provider_manager, "get_provider", return_value=provider),
            patch.object(ts, "get_backend", return_value=backend),
        ):
            assert ts._message_visible_in_box("t1", "Analyze the logs") is False

    def test_exact_match_ignores_wrapping_whitespace_and_punctuation(self):
        provider, backend = self._dependencies("Analyze the\nlogs carefully!")
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w"},
            ),
            patch.object(ts.provider_manager, "get_provider", return_value=provider),
            patch.object(ts, "get_backend", return_value=backend),
        ):
            assert ts._message_visible_in_box("t1", "Analyze the logs carefully") is True


@pytest.mark.asyncio
class TestConfirmWorkerStartedOrResubmit:
    async def test_started_on_first_confirm_no_resubmit(self):
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=True)),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )

        assert ok is True
        key.assert_not_called()
        send.assert_not_called()

    async def test_enter_resubmit_requires_two_stable_exact_reads(self):
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts.status_monitor, "get_status", return_value=TerminalStatus.IDLE),
            patch.object(ts, "_message_visible_in_box", side_effect=[True, True]) as visible,
            patch.object(ts.asyncio, "sleep", new=AsyncMock()),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )

        assert ok is True
        assert visible.call_count == 2
        key.assert_called_once_with("t1", "Enter")
        send.assert_not_called()

    async def test_changing_draft_never_receives_bare_enter(self):
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts.status_monitor, "get_status", return_value=TerminalStatus.IDLE),
            patch.object(ts, "_message_visible_in_box", side_effect=[True, False]),
            patch.object(ts.asyncio, "sleep", new=AsyncMock()),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", "reg", "sup", None
            )

        assert ok is True
        key.assert_not_called()
        send.assert_called_once()

    async def test_waiting_dialog_blocks_resubmit(self):
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(
                ts.status_monitor,
                "get_status",
                return_value=TerminalStatus.WAITING_USER_ANSWER,
            ),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            with pytest.raises(TerminalInputBlockedError, match="waiting for a user answer"):
                await ts._confirm_worker_started_or_resubmit(
                    "t1", "Analyze the logs", None, "sup", None
                )

        key.assert_not_called()
        send.assert_not_called()

    async def test_error_terminal_returns_false_without_resubmit(self):
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(ts.status_monitor, "get_status", return_value=TerminalStatus.ERROR),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )

        assert ok is False
        key.assert_not_called()
        send.assert_not_called()

    async def test_full_redelivery_flows_through_send_input_guards(self):
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts.status_monitor, "get_status", return_value=TerminalStatus.IDLE),
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", "reg", "sup", None
            )

        assert ok is True
        key.assert_not_called()
        send.assert_called_once_with(
            "t1",
            "Analyze the logs",
            registry="reg",
            sender_id="sup",
            orchestration_type=None,
            defer_on_dialog=True,
            expect_callback=False,
        )

    async def test_returns_false_when_worker_never_starts(self):
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(ts.status_monitor, "get_status", return_value=TerminalStatus.IDLE),
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts.asyncio, "sleep", new=AsyncMock()),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )

        assert ok is False
        assert key.call_count == ts._DEFERRED_SUBMIT_MAX_RESUBMITS
        send.assert_not_called()
