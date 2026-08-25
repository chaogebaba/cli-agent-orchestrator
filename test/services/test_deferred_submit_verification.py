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


class TestRedeliverDroppedMessageHelper:
    """The shared one-attempt helper: a caller without a provider instance
    (the synchronous step path, #562) gets it resolved from the registry,
    best-effort — a resolution failure means no probe, never a lost
    redelivery."""

    def test_resolves_provider_from_registry_for_direct_probe(self):
        # Provider without explicit pass + direct probe True → started, no send.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "provider_manager") as mgr,
            patch.object(ts, "_worker_is_started_direct", return_value=True) as probe,
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            mgr.get_provider.return_value = provider
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1)
        assert started is True
        mgr.get_provider.assert_called_once_with("t1")
        probe.assert_called_once_with("t1", provider)
        key.assert_not_called()
        send.assert_not_called()

    def test_provider_resolution_failure_falls_through_to_box_check(self):
        # Registry blowup must not lose the redelivery — box check still runs.
        with (
            patch.object(ts, "provider_manager") as mgr,
            patch.object(ts, "_message_visible_in_box", return_value=True) as box,
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            mgr.get_provider.side_effect = ValueError("Terminal t1 not found")
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1)
        assert started is False
        box.assert_called_once_with("t1", "Analyze the logs")
        key.assert_called_once_with("t1", "Enter")
        send.assert_not_called()

    def test_gate_on_probe_capable_still_full_resends_when_box_empty(self):
        # Gated step path: probe ran and said not-started, text absent → the
        # probe ruled out a working worker, so the full re-send is safe.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "_worker_is_started_direct", return_value=False),
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message(
                "t1", "Analyze the logs", 1, provider, full_resend_requires_probe=True
            )
        assert started is False
        key.assert_not_called()
        send.assert_called_once()

    def test_gate_on_skips_full_resend_without_probe(self):
        # Gated step path + non-probe provider + text absent: cannot tell
        # "paste dropped" from "worker running, prompt scrolled off" — the
        # full re-send would risk a duplicate task, so nothing is sent.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_worker_is_started_direct") as probe,
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message(
                "t1", "Analyze the logs", 1, provider, full_resend_requires_probe=True
            )
        assert started is False
        probe.assert_not_called()
        key.assert_not_called()
        send.assert_not_called()

    def test_gate_on_still_sends_bare_enter_without_probe(self):
        # Gated step path + non-probe provider + text VISIBLE: a bare Enter
        # cannot duplicate a task, so the Enter-swallowed recovery survives
        # the gate.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message(
                "t1", "Analyze the logs", 1, provider, full_resend_requires_probe=True
            )
        assert started is False
        key.assert_called_once_with("t1", "Enter")
        send.assert_not_called()

    def test_gate_off_default_keeps_deferred_init_behavior(self):
        # Deferred-init path (default): non-probe provider + text absent →
        # full re-send, exactly as before the helper was extracted.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1, provider)
        assert started is False
        key.assert_not_called()
        send.assert_called_once()


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

    async def test_direct_probe_short_circuits_when_worker_started(self):
        # Provider with supports_direct_status_probe=True + direct probe True →
        # returns True without calling send_input or send_special_key.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(ts, "_worker_is_started_direct", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=provider,
            )
        assert ok is True
        key.assert_not_called()
        send.assert_not_called()

    async def test_direct_probe_falls_through_when_worker_not_started(self):
        # Direct probe returns False → continues to existing resubmit logic.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_worker_is_started_direct", return_value=False),
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=provider,
            )
        assert ok is True
        key.assert_called_once()
        send.assert_not_called()

    async def test_direct_probe_skipped_when_provider_not_opted_in(self):
        # Provider without supports_direct_status_probe → direct probe never
        # invoked; falls through to existing resubmit logic.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_worker_is_started_direct") as probe,
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key"),
            patch.object(ts, "send_input"),
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=provider,
            )
        assert ok is True
        probe.assert_not_called()

    async def test_provider_none_skips_direct_probe(self):
        # The existing None-provider path still works unchanged.
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_worker_is_started_direct") as probe,
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key"),
            patch.object(ts, "send_input"),
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=None,
            )
        assert ok is True
        probe.assert_not_called()


class TestWorkerIsStartedDirect:
    """Unit tests for the capture-pane direct status probe."""

    def test_returns_false_when_metadata_is_none(self):
        with patch.object(ts, "get_terminal_metadata", return_value=None):
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_session_key_missing(self):
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_window": "w1"}):
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_window_key_missing(self):
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_session": "s1"}):
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_get_history_raises(self):
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            mock_be.return_value.get_history.side_effect = Exception("capture failed")
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_get_status_raises(self):
        provider = MagicMock()
        provider.get_status.side_effect = Exception("parse failure")
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            assert ts._worker_is_started_direct("t1", provider) is False

    def test_returns_true_when_status_is_processing(self):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            assert ts._worker_is_started_direct("t1", provider) is True

    def test_returns_false_when_status_is_idle(self):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            assert ts._worker_is_started_direct("t1", provider) is False
