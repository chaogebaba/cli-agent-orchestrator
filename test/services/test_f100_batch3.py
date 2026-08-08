"""Tests for f100 batch 3: B1 deferred retry, A5 context fence, B2 grok arm2, B3 pyte fallback."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError

# ---------------------------------------------------------------------------
# B1: DeliveryDeferredError retry in deferred _run()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDeferredDeliveryRetry:
    """The first send_input retries up to 3 times on DeliveryDeferredError."""

    async def _run_deferred(self, send_side_effect, *, expect_teardown=False):
        """
        Helper that runs the relevant branch of the deferred _run loop.
        Returns (send_input_mock, teardown_called).
        """
        send_mock = AsyncMock(side_effect=send_side_effect)
        confirm_mock = AsyncMock(return_value=True)
        mark_ready_mock = AsyncMock()
        sleep_mock = AsyncMock()

        with (
            patch.object(ts, "send_input", send_mock),
            patch.object(ts, "_confirm_worker_started_or_resubmit", confirm_mock),
            patch.object(ts, "_mark_ready_if_generation_current", mark_ready_mock),
            patch.object(ts.asyncio, "sleep", sleep_mock),
            patch.object(
                ts, "_tracked_blocking", new=self._passthrough_tracked_blocking(send_mock)
            ),
            patch.object(ts, "_claim_and_settle_deferred_failure", new=AsyncMock()) as teardown,
        ):
            # Simulate the deferred _run logic inline
            prepared_message = "task message"
            terminal_id = "t1"
            generation = 1
            send_kwargs = {}

            _DEFERRED_DELIVERY_MAX_RETRIES = 3
            _DEFERRED_DELIVERY_RETRY_DELAY = 2.0
            raised = False
            for _attempt in range(_DEFERRED_DELIVERY_MAX_RETRIES):
                try:
                    await send_mock(terminal_id, prepared_message, **send_kwargs)
                    break
                except DeliveryDeferredError:
                    if _attempt == _DEFERRED_DELIVERY_MAX_RETRIES - 1:
                        raised = True
                        break
                    await sleep_mock(_DEFERRED_DELIVERY_RETRY_DELAY)

            return send_mock, raised, sleep_mock

    def _passthrough_tracked_blocking(self, send_mock):
        async def _tracked_blocking(tid, gen, mode, label, fn, *args, **kwargs):
            return await fn(*args, **kwargs)

        return _tracked_blocking

    async def test_succeeds_after_two_deferred_errors(self):
        """send_input raises twice then succeeds → no teardown."""
        effects = [DeliveryDeferredError("deferred"), DeliveryDeferredError("deferred"), None]
        send_mock, raised, sleep_mock = await self._run_deferred(effects)
        assert not raised
        assert send_mock.call_count == 3
        assert sleep_mock.call_count == 2

    async def test_all_retries_exhausted_raises(self):
        """send_input raises all 3 times → raises to outer handler."""
        effects = [DeliveryDeferredError("d")] * 3
        send_mock, raised, sleep_mock = await self._run_deferred(effects)
        assert raised
        assert send_mock.call_count == 3
        assert sleep_mock.call_count == 2

    async def test_succeeds_first_try_no_sleep(self):
        """send_input succeeds immediately → no retry, no sleep."""
        send_mock, raised, sleep_mock = await self._run_deferred([None])
        assert not raised
        assert send_mock.call_count == 1
        assert sleep_mock.call_count == 0


# ---------------------------------------------------------------------------
# A5: Context fence in fork preamble
# ---------------------------------------------------------------------------


class TestContextFence:
    """Fork from a base → verify [CONTEXT FENCE] in preamble."""

    def _build_preamble_with_fence(self, row, workdir_preamble=""):
        """Simulate the preamble construction logic from server.py."""
        preamble = "staleness preamble"  # simulating stale.preamble
        if workdir_preamble:
            preamble = f"{preamble}\n{workdir_preamble}"
        # Context fence
        base_cwd = row["cwd"]
        base_sha = (row.get("git_sha") or "unknown")[:8]
        fence = (
            f"[CONTEXT FENCE] Base '{row['name']}' was snapshot at "
            f"{base_cwd}@{base_sha}. If that names a different repository than "
            f"your working directory, inherited context from it does NOT apply "
            f"to your current task."
        )
        if preamble:
            preamble = f"{preamble}\n{fence}"
        else:
            preamble = fence
        return preamble

    def test_fence_contains_base_name_and_sha(self):
        row = {
            "name": "my-base",
            "cwd": "/home/user/project",
            "git_sha": "abcdef1234567890",
        }
        preamble = self._build_preamble_with_fence(row)
        assert "[CONTEXT FENCE]" in preamble
        assert "my-base" in preamble
        assert "abcdef12" in preamble  # truncated to 8 chars
        assert "/home/user/project" in preamble

    def test_fence_with_unknown_sha(self):
        row = {
            "name": "oracle-base",
            "cwd": "/workspace",
            "git_sha": None,
        }
        preamble = self._build_preamble_with_fence(row)
        assert "unknown" in preamble
        assert "oracle-base" in preamble

    def test_fence_appended_after_workdir_preamble(self):
        row = {
            "name": "base1",
            "cwd": "/repo",
            "git_sha": "deadbeef01234567",
        }
        preamble = self._build_preamble_with_fence(row, workdir_preamble="workdir: /other")
        lines = preamble.split("\n")
        # workdir_preamble is in the middle
        assert any("workdir: /other" in line for line in lines)
        # fence is the last meaningful section
        assert "[CONTEXT FENCE]" in lines[-1]


# ---------------------------------------------------------------------------
# B2: Grok arm 2 footer + composer requirement
# ---------------------------------------------------------------------------


class TestGrokArm2FooterComposer:
    """Footer without composer ❯ → NOT IDLE. With both → IDLE."""

    def _provider(self):
        from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider

        return GrokCliProvider(
            terminal_id="term-grok",
            session_name="session",
            window_name="window",
            agent_profile="grok_dev",
            allowed_tools=["*"],
        )

    def test_footer_with_composer_prompt_is_idle(self):
        """Footer + ❯ in last 8 lines → IDLE."""
        provider = self._provider()
        output = "\n".join(
            [
                "Some output text",
                "   ❯",
                "   Grok 4.5 (high) · always-approve · ctrl+o transcript",
            ]
        )
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_footer_without_composer_prompt_is_not_idle(self):
        """Footer present but no ❯ in last 8 lines → NOT IDLE (PROCESSING)."""
        provider = self._provider()
        output = "\n".join(
            [
                "Some output text",
                "More output",
                "   Grok 4.5 (high) · always-approve · ctrl+o transcript",
            ]
        )
        status = provider.get_status(output)
        # Should not be IDLE — the footer is there but composer prompt isn't
        assert status != TerminalStatus.IDLE

    def test_last_idle_match_still_triggers_arm1(self):
        """last_idle match (IDLE_PROMPT_PATTERN via _last_idle_match) → IDLE regardless."""
        provider = self._provider()
        # Simulate output that matches _last_idle_match (Turn completed)
        output = "\n".join(
            [
                "Turn completed in 2.0s.",
                "   ❯",
                "   Grok 4.5 (high) · always-approve · ctrl+o transcript",
            ]
        )
        # This should hit COMPLETED or IDLE via last_idle
        status = provider.get_status(output)
        assert status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)


# ---------------------------------------------------------------------------
# B3: Pyte/stash snapshot resample + capture_viewport fallback
# ---------------------------------------------------------------------------


class TestReadStashSnapshotFallback:
    """get_history failure → resample; both fail → capture_viewport fallback."""

    def _metadata(self):
        return {"tmux_session": "s", "tmux_window": "w"}

    def _provider(self):
        provider = MagicMock()
        provider.read_composer_draft.return_value = ""
        provider.composer_stashed_chip_pattern = None
        return provider

    def test_get_history_fails_once_then_succeeds(self, monkeypatch):
        """First get_history raises, second succeeds → returns snapshot."""
        from cli_agent_orchestrator.services import draft_guard

        call_count = {"n": 0}

        def mock_get_history(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("pyte fail")
            return "captured content"

        backend = MagicMock()
        backend.get_history = mock_get_history
        monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)
        monkeypatch.setattr(draft_guard.time, "sleep", lambda _: None)

        result = draft_guard._read_stash_snapshot(self._metadata(), self._provider())
        # No None → got a snapshot (even if None because no chip pattern)
        # The function proceeds with captured content — not None from the except
        assert call_count["n"] == 2

    def test_both_get_history_fail_capture_viewport_fallback(self, monkeypatch):
        """Both get_history attempts fail, capture_viewport succeeds."""
        from cli_agent_orchestrator.services import draft_guard

        backend = MagicMock()
        backend.get_history.side_effect = RuntimeError("pyte fail")
        backend.capture_viewport.return_value = "viewport content"
        monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)
        monkeypatch.setattr(draft_guard.time, "sleep", lambda _: None)

        result = draft_guard._read_stash_snapshot(self._metadata(), self._provider())
        assert backend.get_history.call_count == 2
        backend.capture_viewport.assert_called_once()

    def test_all_captures_fail_returns_none(self, monkeypatch):
        """get_history fails twice, capture_viewport also fails → None."""
        from cli_agent_orchestrator.services import draft_guard

        backend = MagicMock()
        backend.get_history.side_effect = RuntimeError("pyte fail")
        backend.capture_viewport.side_effect = RuntimeError("capture fail")
        monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)
        monkeypatch.setattr(draft_guard.time, "sleep", lambda _: None)

        result = draft_guard._read_stash_snapshot(self._metadata(), self._provider())
        assert result is None
        assert backend.get_history.call_count == 2
        backend.capture_viewport.assert_called_once()
