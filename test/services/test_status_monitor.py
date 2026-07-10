"""Tests for StatusMonitor — focus on backend-aware get_status().

get_status() is the single source of truth for terminal status. For pipe-pane
backends (tmux) it returns the pushed pipeline status; for event-inbox backends
(herdr), which never feed the pipeline, it derives status on demand from the
provider's native status. These tests pin both paths.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


def _backend(event_inbox):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = event_inbox
    return backend


class TestGetStatusTmux:
    """Pipe-pane backend: get_status returns the pushed _last_status, unchanged."""

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_returns_pushed_status(self, mock_get_backend):
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING

        assert sm.get_status("t1") == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_never_seen(self, mock_get_backend):
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()

        assert sm.get_status("missing") == TerminalStatus.UNKNOWN


class TestGetStatusEventInbox:
    """Event-inbox backend (herdr): derive status on demand from the provider."""

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_derives_from_provider_native_status(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        # _last_status is empty (herdr never feeds the pipeline) — the old code
        # would return UNKNOWN here.
        assert sm.get_status("t1") == TerminalStatus.IDLE
        provider.get_status.assert_called_once()

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_no_provider(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        mock_pm.get_provider.return_value = None

        sm = StatusMonitor()
        assert sm.get_status("t1") == TerminalStatus.UNKNOWN

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_provider_lookup_raises(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        mock_pm.get_provider.side_effect = ValueError("terminal not in db")

        sm = StatusMonitor()
        assert sm.get_status("t1") == TerminalStatus.UNKNOWN

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_provider_get_status_raises(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.side_effect = RuntimeError("herdr cli failed")
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        assert sm.get_status("t1") == TerminalStatus.UNKNOWN


class TestScreenDetection:
    """Rendered-screen detection should fail soft and keep monitoring alive."""

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_render_error_falls_back_to_raw_buffer_detection(self, mock_pm):
        class BrokenScreen:
            @property
            def display(self):
                raise RuntimeError("torn pyte frame")

        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        mock_pm.get_provider.side_effect = AssertionError("provider should not be refetched")

        sm = StatusMonitor()
        sm._screens["t1"] = (BrokenScreen(), MagicMock())
        sm._buffers["t1"] = "raw buffer with idle footer"

        assert sm._detect_screen("t1", provider) == TerminalStatus.IDLE
        provider.get_status.assert_called_once_with("raw buffer with idle footer")
        mock_pm.get_provider.assert_not_called()

    def test_render_error_raw_processing_fallback_does_not_override_ready_latch(self):
        class BrokenScreen:
            @property
            def display(self):
                raise RuntimeError("torn pyte frame")

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.IDLE
        sm._screens["t1"] = (BrokenScreen(), MagicMock())
        sm._buffers["t1"] = "stale raw processing marker"
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        bus = MagicMock()

        with patch("cli_agent_orchestrator.services.status_monitor.bus", bus):
            sm._on_screen_quiescent("t1", provider)

        provider.get_status.assert_called_once_with("stale raw processing marker")
        provider.get_status_from_screen.assert_not_called()
        bus.publish.assert_not_called()
        assert sm._last_status["t1"] == TerminalStatus.IDLE

    def test_screen_uses_backend_pane_size_when_creating_pyte_screen(self, caplog):
        sm = StatusMonitor()
        caplog.set_level("INFO", logger="cli_agent_orchestrator.services.status_monitor")

        sm._feed_screen_locked("t1", "hello", screen_size=(12, 3))

        screen, _stream = sm._screens["t1"]
        assert screen.columns == 12
        assert screen.lines == 3
        assert "pyte screen created for t1 at 12x3" in caplog.text

    def test_screen_defers_creation_when_pane_size_unknown(self, caplog):
        """Regression: fallback-sized screens (220x50 vs real 139x49) freeze the
        wrong viewport height and can leave stale prompt rows composited over a
        busy turn. Unknown first size must not create that palimpsest screen.
        """
        sm = StatusMonitor()
        caplog.set_level("WARNING", logger="cli_agent_orchestrator.services.status_monitor")

        assert sm._feed_screen_locked("t1", "hello", screen_size=None) is False

        assert "t1" not in sm._screens
        assert "pyte screen creation deferred for t1: screen size unresolved" in caplog.text

    @patch("cli_agent_orchestrator.services.status_monitor.CAO_PYTE_STATUS", True)
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_first_chunk_before_metadata_replays_into_real_sized_screen(self, mock_pm, caplog):
        sm = StatusMonitor()
        caplog.set_level("INFO", logger="cli_agent_orchestrator.services.status_monitor")
        provider = MagicMock()
        provider.supports_screen_detection = True

        def detect(lines):
            return (
                TerminalStatus.PROCESSING
                if "working spinner" in "\n".join(lines)
                else TerminalStatus.UNKNOWN
            )

        provider.get_status_from_screen.side_effect = detect
        mock_pm.get_provider.return_value = provider
        bus = MagicMock()

        with (
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
            patch.object(sm, "_resolve_screen_size", side_effect=[None, (139, 49)]),
        ):
            sm._process_chunk("t1", "working spinner")
            assert "t1" not in sm._screens
            provider.get_status_from_screen.assert_not_called()

            sm._process_chunk("t1", "\n")

        screen, _stream = sm._screens["t1"]
        assert screen.columns == 139
        assert screen.lines == 49
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        provider.get_status_from_screen.assert_called_once()
        assert "pyte screen creation deferred for t1: screen size unresolved" in caplog.text
        assert "pyte screen created for t1 at 139x49" in caplog.text

    def test_armed_screen_detection_runs_even_when_already_bursting(self):
        sm = StatusMonitor()
        provider = MagicMock()
        statuses = iter([
            TerminalStatus.IDLE,
            TerminalStatus.COMPLETED,
            TerminalStatus.PROCESSING,
        ])
        provider.get_status_from_screen.side_effect = lambda _lines: next(statuses)
        bus = MagicMock()

        with (
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
            patch.object(sm, "_running_loop", return_value=MagicMock()),
        ):
            sm._feed_screen_locked("t1", "ready", screen_size=(80, 24))
            sm._schedule_screen_detection("t1", provider)
            sm.notify_input_sent("t1")
            sm._feed_screen_locked("t1", "torn ready")
            sm._schedule_screen_detection("t1", provider)
            assert sm._last_status["t1"] == TerminalStatus.IDLE
            sm._feed_screen_locked("t1", "working")
            sm._schedule_screen_detection("t1", provider)

        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        assert provider.get_status_from_screen.call_count == 3

    def test_unarmed_mid_burst_does_not_detect_ready_state(self):
        sm = StatusMonitor()
        provider = MagicMock()
        provider.get_status_from_screen.return_value = TerminalStatus.COMPLETED
        bus = MagicMock()

        with (
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
            patch.object(sm, "_running_loop", return_value=MagicMock()),
        ):
            sm._feed_screen_locked("t1", "ready", screen_size=(80, 24))
            sm._schedule_screen_detection("t1", provider)
            sm._feed_screen_locked("t1", "repaint")
            sm._schedule_screen_detection("t1", provider)

        assert provider.get_status_from_screen.call_count == 2
        assert sm._last_status["t1"] == TerminalStatus.COMPLETED
        assert bus.publish.call_count == 1

    def test_run9_screen_processing_after_idle_flap_reopens_busy_state(self):
        sm = StatusMonitor()
        sm._screens["t1"] = (MagicMock(display=["Working"]), MagicMock())
        provider = MagicMock()
        provider.get_status_from_screen.side_effect = [
            TerminalStatus.PROCESSING,
            TerminalStatus.IDLE,
            TerminalStatus.PROCESSING,
            TerminalStatus.PROCESSING,
        ]
        bus = MagicMock()
        published = []
        bus.publish.side_effect = lambda _topic, data: published.append(data["status"])

        with (
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
            patch(
                "cli_agent_orchestrator.services.auto_responder.auto_responder.on_screen",
                return_value=None,
            ),
        ):
            sm.notify_input_sent("t1")
            sm._schedule_screen_detection("t1", provider)
            sm._schedule_screen_detection("t1", provider)
            sm._schedule_screen_detection("t1", provider)
            sm._schedule_screen_detection("t1", provider)

        assert provider.get_status_from_screen.call_count == 4
        assert published == ["processing", "idle", "processing"]
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.CAO_PYTE_STATUS", True)
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    async def test_real_loop_codex_spinner_after_idle_flap_recovers_processing(self, mock_pm):
        """Real-loop regression for run 11: a stale idle quiescence result must
        not overwrite newer Codex spinner chunks and strand status at idle.
        """
        sm = StatusMonitor()
        sm._loop = asyncio.get_running_loop()
        provider = CodexProvider("t1", "session", "window")
        real_screen_status = provider.get_status_from_screen
        idle_detection_started = threading.Event()
        release_idle_detection = threading.Event()
        delay_next_idle_detection = threading.Event()

        def delayed_status_from_screen(lines):
            status = real_screen_status(lines)
            if status == TerminalStatus.IDLE and delay_next_idle_detection.is_set():
                delay_next_idle_detection.clear()
                idle_detection_started.set()
                assert release_idle_detection.wait(timeout=1)
            return status

        provider.get_status_from_screen = delayed_status_from_screen
        mock_pm.get_provider.return_value = provider
        published = []
        bus = MagicMock()
        bus.publish.side_effect = lambda _topic, data: published.append(data["status"])

        def frame(*lines: str) -> str:
            return "\x1b[2J\x1b[H" + "\r\n".join(lines)

        async def feed(chunk: str) -> None:
            await asyncio.to_thread(sm._process_chunk, "t1", chunk)
            # Let call_soon_threadsafe timer setup run, but do not wait long
            # enough for the 200ms quiescence callback to rescue the status.
            await asyncio.sleep(0.01)

        with (
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
            patch.object(sm, "_resolve_screen_size", return_value=(139, 49)),
            patch(
                "cli_agent_orchestrator.services.auto_responder.auto_responder.on_screen",
                return_value=None,
            ),
        ):
            await feed(frame("› ", "? for shortcuts / 99% context left"))
            delay_next_idle_detection.set()
            assert await asyncio.to_thread(idle_detection_started.wait, 1)

            sm.notify_input_sent("t1")
            await feed(
                frame(
                    "› STILL false-idle probe",
                    "• Working (0s • esc to interrupt)",
                    "› ",
                    "? for shortcuts / 99% context left",
                )
            )
            assert sm._last_status["t1"] == TerminalStatus.PROCESSING

            release_idle_detection.set()
            await asyncio.sleep(0.05)
            assert sm._last_status["t1"] == TerminalStatus.PROCESSING

            await feed(
                frame(
                    "› STILL false-idle probe",
                    "• Working (1s • esc to interrupt)",
                    "› ",
                    "? for shortcuts / 99% context left",
                )
            )

        screen_lines = sm.get_rendered_screen("t1") or []
        screen_text = "\n".join(screen_lines)
        nonblank_tail = "\n".join([line for line in screen_lines if line.strip()][-8:])
        assert "esc to interrupt" in screen_text
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING, nonblank_tail
        assert published[-1] == "processing"
        sm.clear_terminal("t1")

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.CAO_PYTE_STATUS", True)
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    async def test_ready_bursting_codex_one_line_spinner_repaint_recovers_processing(
        self, mock_pm
    ):
        """Small Codex spinner repaints must recover even if debounce state is
        already bursting; waiting for a later large burst is too late.
        """
        sm = StatusMonitor()
        sm._loop = asyncio.get_running_loop()
        provider = CodexProvider("t1", "session", "window")
        mock_pm.get_provider.return_value = provider
        bus = MagicMock()
        published = []
        bus.publish.side_effect = lambda _topic, data: published.append(data["status"])

        async def feed(chunk: str) -> None:
            await asyncio.to_thread(sm._process_chunk, "t1", chunk)
            await asyncio.sleep(0.01)

        with (
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
            patch.object(sm, "_resolve_screen_size", return_value=(139, 49)),
            patch(
                "cli_agent_orchestrator.services.auto_responder.auto_responder.on_screen",
                return_value=None,
            ),
        ):
            await feed("\x1b[2J\x1b[H› \r\n? for shortcuts / 99% context left")
            await asyncio.sleep(0.25)
            assert sm._last_status["t1"] == TerminalStatus.IDLE

            sm._bursting["t1"] = True
            sm._allow_processing_revert["t1"] = False
            await feed("\x1b[2;1H• Working (1s • esc to interrupt)\x1b[K")

        screen_text = "\n".join(sm.get_rendered_screen("t1") or [])
        assert "esc to interrupt" in screen_text
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        assert published[-1] == "processing"
        sm.clear_terminal("t1")


class _SequencedMonitor:
    """Drive _process_chunk with a scripted sequence of detected statuses.

    Patches provider get_status to pop from the script and the event bus to
    record published status events, so each test reads as: feed detections,
    assert latched status + published transitions.
    """

    def __init__(self):
        self.sm = StatusMonitor()
        self.published = []

    def feed(self, status):
        provider = MagicMock()
        provider.get_status.return_value = status
        # These tests exercise the RAW detection path's latch logic. Pin
        # supports_screen_detection False so they are independent of the
        # CAO_PYTE_STATUS default (a bare MagicMock would be truthy and route
        # through the pyte screen path).
        provider.supports_screen_detection = False
        bus = MagicMock()
        bus.publish.side_effect = lambda topic, data: self.published.append(data["status"])
        with (
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
        ):
            mock_pm.get_provider.return_value = provider
            self.sm._process_chunk("t1", "x")

    def status(self):
        return self.sm._last_status.get("t1")


class TestStickyLatching:
    """Pin the sticky ready-status latch + notify_input_sent state machine."""

    def test_idle_to_processing_blocked_without_arm(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.feed(TerminalStatus.PROCESSING)  # eviction flap
        assert m.status() == TerminalStatus.IDLE
        assert m.published == ["idle"]

    def test_ready_to_unknown_blocked_without_arm(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.UNKNOWN)
        assert m.status() == TerminalStatus.COMPLETED

    def test_completed_to_idle_blocked_without_arm(self):
        """Codex-style: user marker evicts before assistant bullet."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.IDLE)
        assert m.status() == TerminalStatus.COMPLETED

    def test_idle_to_completed_always_allowed(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.feed(TerminalStatus.COMPLETED)
        assert m.status() == TerminalStatus.COMPLETED

    def test_arm_allows_processing_then_reblocks(self):
        """The normal cycle: input → PROCESSING accepted → COMPLETED → flap blocked."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.PROCESSING)
        assert m.status() == TerminalStatus.PROCESSING
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.PROCESSING)  # post-completion eviction flap
        assert m.status() == TerminalStatus.COMPLETED

    def test_arm_survives_ready_to_ready_flap(self):
        """A large paste can evict the response markers BEFORE the agent
        starts working, flapping COMPLETED → IDLE. That flap must not consume
        the arm — otherwise the genuine PROCESSING that follows is blocked,
        the terminal reads IDLE while the agent is busy, and InboxService
        (which delivers on IDLE/COMPLETED) can paste a queued message into
        the middle of an active response."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.IDLE)  # paste evicted markers — flap
        assert m.status() == TerminalStatus.IDLE
        m.feed(TerminalStatus.PROCESSING)  # genuine cycle start
        assert m.status() == TerminalStatus.PROCESSING
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.PROCESSING)  # post-completion flap re-blocked
        assert m.status() == TerminalStatus.COMPLETED

    def test_arm_survives_waiting_user_answer_to_idle(self):
        """Answering a permission prompt (send_special_key arms the gate)
        can flap WAITING_USER_ANSWER → IDLE before the agent resumes."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.WAITING_USER_ANSWER)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.IDLE)  # prompt cleared, redraw flap
        m.feed(TerminalStatus.PROCESSING)  # agent resumes the task
        assert m.status() == TerminalStatus.PROCESSING

    def test_arm_consumed_by_init_style_upgrade(self):
        """non-ready → ready latch consumes the arm (CLI launch reaching its
        first idle prompt without a visible PROCESSING window)."""
        m = _SequencedMonitor()
        m.sm.notify_input_sent("t1")  # launch keystroke
        m.feed(TerminalStatus.IDLE)  # TUI ready
        m.feed(TerminalStatus.PROCESSING)  # redraw flap — must be blocked
        assert m.status() == TerminalStatus.IDLE

    def test_processing_consumes_arm_once(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.PROCESSING)
        m.feed(TerminalStatus.IDLE)
        m.feed(TerminalStatus.PROCESSING)  # no new input — blocked
        assert m.status() == TerminalStatus.IDLE

    def test_raw_processing_after_idle_flap_still_blocks_without_new_arm(self):
        """Screen detections may override ready->PROCESSING, but raw buffer
        detections keep the sticky latch because stale-buffer risk remains."""
        m = _SequencedMonitor()
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.PROCESSING)
        m.feed(TerminalStatus.IDLE)
        m.feed(TerminalStatus.PROCESSING)
        m.feed(TerminalStatus.PROCESSING)
        assert m.status() == TerminalStatus.IDLE
        assert m.published == ["processing", "idle"]

    def test_reset_buffer_clears_arm(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.sm.reset_buffer("t1")
        m.feed(TerminalStatus.IDLE)
        m.feed(TerminalStatus.PROCESSING)  # arm gone — blocked
        assert m.status() == TerminalStatus.IDLE

    def test_clear_rolling_buffer_preserves_arm(self):
        """clear_rolling_buffer is byte-only — arm survives so the next
        IDLE→PROCESSING transition (after send_input) is honored.

        Regression guard for test_supervisor_assign_and_handoff: send_input
        must clear the rolling buffer to drop stale idle placeholders, but
        the arm must survive so the agent's PROCESSING signal isn't blocked
        by stickiness.
        """
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.sm.clear_rolling_buffer("t1")
        # Arm and last-status preserved
        assert m.sm._allow_processing_revert.get("t1") is True
        assert m.sm._last_status.get("t1") == TerminalStatus.IDLE
        # PROCESSING transition honored (arm consumed on genuine PROCESSING)
        m.feed(TerminalStatus.PROCESSING)
        assert m.status() == TerminalStatus.PROCESSING

    def test_clear_terminal_clears_arm(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.sm.clear_terminal("t1")
        assert "t1" not in m.sm._allow_processing_revert

    def test_no_event_published_for_blocked_downgrade(self):
        """Blocked flaps must not publish status events — InboxService
        subscribes to them and a spurious ready event could double-deliver."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.PROCESSING)
        m.feed(TerminalStatus.UNKNOWN)
        m.feed(TerminalStatus.IDLE)
        assert m.published == ["completed"]

    def test_unknown_does_not_overwrite_known_processing(self):
        """UNKNOWN is 'no signal', not a state: a mid-turn UNKNOWN (e.g. the
        screen momentarily shows neither spinner nor prompt while a tool runs)
        must not downgrade a known PROCESSING. Observed live as a spurious
        processing→unknown→completed blip."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.PROCESSING)
        m.feed(TerminalStatus.UNKNOWN)
        assert m.status() == TerminalStatus.PROCESSING

    def test_armed_unknown_then_ready_rerender_keeps_processing(self):
        """Guards against a tempting-but-wrong "suppress UNKNOWN only when not
        armed" change (so an armed new turn could clear a stale ready status).

        If an armed terminal's rising-edge frame reads UNKNOWN (a torn paste
        frame) and then re-renders the PRIOR turn's COMPLETED before the new
        spinner draws, letting that UNKNOWN through would make the
        UNKNOWN->COMPLETED bounce a non-ready->ready upgrade that CONSUMES the
        revert arm. The genuine PROCESSING that follows would then be latch-
        blocked, stranding the terminal at COMPLETED for the whole busy turn —
        and InboxService (delivers on IDLE/COMPLETED) would paste into a working
        agent. Suppressing UNKNOWN unconditionally keeps the arm intact so the
        real PROCESSING wins."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        assert m.sm.get_status_gen("t1") == 0
        m.sm.notify_input_sent("t1")
        new_gen = m.sm.get_input_gen("t1")
        m.feed(TerminalStatus.UNKNOWN)  # torn rising-edge frame after the paste
        m.feed(TerminalStatus.COMPLETED)  # prior turn re-rendered at quiescence
        assert m.sm.get_status_gen("t1") < new_gen
        m.feed(TerminalStatus.PROCESSING)  # genuine new-turn processing
        assert m.sm.get_status_gen("t1") < new_gen
        m.feed(TerminalStatus.COMPLETED)
        assert m.sm.get_status_gen("t1") == new_gen
        assert m.status() == TerminalStatus.COMPLETED
        assert m.published == ["completed", "processing", "completed"]

    def test_initial_unknown_is_published(self):
        """The first detection (last is None) may legitimately be UNKNOWN —
        e.g. a freshly created terminal before any marker renders."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.UNKNOWN)
        assert m.status() == TerminalStatus.UNKNOWN
        assert m.published == ["unknown"]


class TestStatusGenerations:
    def test_processing_edge_then_ready_stamps_input_generation(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.sm.notify_input_sent("t1")
        gen = m.sm.get_input_gen("t1")
        m.feed(TerminalStatus.COMPLETED)  # accepted no-change redraw, still stale
        assert m.sm.get_status_gen("t1") < gen
        m.feed(TerminalStatus.PROCESSING)
        assert m.sm._processing_gen["t1"] == gen
        assert m.sm.get_status_gen("t1") < gen
        m.feed(TerminalStatus.COMPLETED)
        assert m.sm.get_status_gen("t1") == gen

    def test_notify_invalidates_detection_computed_at_live_sequence(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        detection_seq = m.sm._chunk_seq["t1"]
        m.sm.notify_input_sent("t1")
        gen = m.sm.get_input_gen("t1")
        assert m.sm._chunk_seq["t1"] > detection_seq

        # This detection was computed before the send but only reached the
        # apply boundary afterward. The send-time sequence bump must reject it.
        m.sm._apply_detection(
            "t1", TerminalStatus.PROCESSING, expected_seq=detection_seq
        )
        assert m.sm._processing_gen.get("t1", 0) < gen

    def test_unknown_rejection_stamps_nothing(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.sm.notify_input_sent("t1")
        gen = m.sm.get_input_gen("t1")
        m.feed(TerminalStatus.UNKNOWN)
        assert m.sm.get_status_gen("t1") < gen

    def test_generation_lifecycle(self):
        sm = StatusMonitor()
        sm.notify_input_sent("t1")
        sm._processing_gen["t1"] = 1
        sm._status_gen["t1"] = 1
        sm.clear_rolling_buffer("t1")
        assert (sm.get_input_gen("t1"), sm._processing_gen["t1"], sm._status_gen["t1"]) == (1, 1, 1)
        sm.reset_buffer("t1")
        assert sm.get_input_gen("t1") == 0
        assert "t1" not in sm._processing_gen and "t1" not in sm._status_gen
        sm.notify_input_sent("t1")
        sm.clear_terminal("t1")
        assert sm.get_input_gen("t1") == 0

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_event_inbox_has_no_status_generation(self, mock_get_backend):
        mock_get_backend.return_value = _backend(event_inbox=True)
        sm = StatusMonitor()
        sm.notify_input_sent("t1")
        assert sm.get_status_gen("t1") is None


class TestQuiescenceTimerCancel:
    """The pyte quiescence timer is an asyncio.TimerHandle owned by the
    StatusMonitor's loop. clear_terminal/reset_buffer can run off that loop
    thread (cleanup_old_data is dispatched via asyncio.to_thread), and
    TimerHandle.cancel() is not thread-safe, so the cancel must be marshaled
    onto the owning loop, never called directly cross-thread."""

    def test_cancel_marshaled_when_off_loop_thread(self):
        sm = StatusMonitor()
        loop = MagicMock()
        sm._loop = loop
        handle = MagicMock()
        sm._quiesce_handle["t1"] = handle

        # clear_terminal from a worker thread (which has no running loop).
        t = threading.Thread(target=sm.clear_terminal, args=("t1",))
        t.start()
        t.join()

        handle.cancel.assert_not_called()
        loop.call_soon_threadsafe.assert_called_once_with(handle.cancel)

    def test_reset_buffer_cancel_marshaled_when_off_loop_thread(self):
        sm = StatusMonitor()
        loop = MagicMock()
        sm._loop = loop
        handle = MagicMock()
        sm._quiesce_handle["t1"] = handle

        t = threading.Thread(target=sm.reset_buffer, args=("t1",))
        t.start()
        t.join()

        handle.cancel.assert_not_called()
        loop.call_soon_threadsafe.assert_called_once_with(handle.cancel)

    def test_cancel_direct_when_no_loop_captured(self):
        """Offline/unit path (no loop ever scheduled a timer): a direct cancel
        is correct because there is no foreign loop to race."""
        sm = StatusMonitor()
        handle = MagicMock()
        sm._quiesce_handle["t1"] = handle
        sm.clear_terminal("t1")  # sm._loop is None
        handle.cancel.assert_called_once()

    def test_no_handle_is_a_noop(self):
        sm = StatusMonitor()
        sm._loop = MagicMock()
        # No timer scheduled for this terminal — must not blow up.
        sm.clear_terminal("missing")
        sm._loop.call_soon_threadsafe.assert_not_called()


class TestQuiescenceGeneration:
    @pytest.mark.asyncio
    async def test_screen_stale_result_in_generation_gap_cannot_publish(self):
        sm = StatusMonitor()
        sm._loop = asyncio.get_running_loop()
        sm._chunk_seq["t1"] = 1
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._screens["t1"] = (MagicMock(display=["› "]), MagicMock())
        started = threading.Event()
        release = threading.Event()
        provider = MagicMock()

        def detect(_lines):
            started.set()
            assert release.wait(timeout=1)
            return TerminalStatus.IDLE

        provider.get_status_from_screen.side_effect = detect
        bus = MagicMock()

        with patch("cli_agent_orchestrator.services.status_monitor.bus", bus):
            sm._on_screen_quiescent("t1", provider, expected_seq=1)
            assert await asyncio.to_thread(started.wait, 1)
            with sm._lock:
                sm._bump_chunk_seq_locked("t1")
            release.set()
            await asyncio.sleep(0.05)

        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_raw_stale_result_in_generation_gap_cannot_publish(self):
        sm = StatusMonitor()
        sm._loop = asyncio.get_running_loop()
        sm._chunk_seq["t1"] = 1
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = "ready"
        started = threading.Event()
        release = threading.Event()

        def detect(_terminal_id, _buffer):
            started.set()
            assert release.wait(timeout=1)
            return TerminalStatus.IDLE

        bus = MagicMock()

        with (
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
            patch.object(sm, "_detect_status", side_effect=detect),
        ):
            sm._on_raw_quiescent("t1", expected_seq=1)
            assert await asyncio.to_thread(started.wait, 1)
            with sm._lock:
                sm._bump_chunk_seq_locked("t1")
            release.set()
            await asyncio.sleep(0.05)

        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_buffer_advances_generation_so_stale_task_cannot_publish(self):
        sm = StatusMonitor()
        sm._loop = asyncio.get_running_loop()
        sm._chunk_seq["t1"] = 1
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = "old ready"
        started = threading.Event()
        release = threading.Event()

        def detect(_terminal_id, _buffer):
            started.set()
            assert release.wait(timeout=1)
            return TerminalStatus.IDLE

        bus = MagicMock()

        with (
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
            patch.object(sm, "_detect_status", side_effect=detect),
        ):
            sm._on_raw_quiescent("t1", expected_seq=1)
            assert await asyncio.to_thread(started.wait, 1)
            sm.reset_buffer("t1")
            assert sm._chunk_seq["t1"] == 2
            release.set()
            await asyncio.sleep(0.05)

        assert "t1" not in sm._last_status
        bus.publish.assert_not_called()


class TestRawDebounceArmedDetection:
    """Regression: raw debounce must detect PROCESSING on later chunks while armed."""

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_armed_ready_detects_processing_on_second_chunk(self, mock_get_backend, mock_pm):
        """When terminal is IDLE (armed), chunk 1 is UNKNOWN, chunk 2 has PROCESSING
        marker — PROCESSING must be detected immediately, not deferred to quiescence."""
        mock_get_backend.return_value = _backend(event_inbox=False)
        provider = MagicMock()
        provider.supports_screen_detection = False
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        # Simulate terminal already at IDLE (armed state)
        sm._last_status["t1"] = TerminalStatus.IDLE
        sm._allow_processing_revert["t1"] = True

        # Mock _detect_status: first call returns UNKNOWN, second returns PROCESSING
        detect_results = iter([TerminalStatus.UNKNOWN, TerminalStatus.PROCESSING])
        sm._detect_status = lambda tid, buf: next(detect_results)

        # Chunk 1: UNKNOWN — should still attempt detection (terminal is ready)
        sm._process_chunk("t1", "neutral output")
        # Chunk 2: PROCESSING — must detect immediately, not wait for quiescence
        sm._process_chunk("t1", "● Working on task...")

        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
