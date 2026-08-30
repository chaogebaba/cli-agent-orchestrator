"""F582 D21 (#386/#530) — auto-responder polling LIFETIME (AC8, arming gap).

Attribution (`/data/cao-scratch/9064394e/f582-d21-attribution.md`, supervisor
correction mid-2629): on the #386 recurrence the whitelisted rule MATCHED and
FIRED on the byte-exact pane — the matcher is correct. The failing stage is an
ARMING/TIMING gap: ``_wait_for_auto_responder_dialog_clear`` evaluated only
during a fixed ~12s window, the dialog rendered AFTER that window closed, and a
static pane never re-arms quiescence detection — so once the loop abandoned at
``timeout`` the responder never re-evaluated and ``send_input`` raced into an
unanswered dialog (the #386 stall).

The empirical F530 matcher leg (that the trust-dir pane FIRES once) lives in
``test/auto_answers/test_f530_corpus.py`` (fixture
``05-trust-dir-startup-card-15a6fa21``, a PASS regression case): the trust
prompt renders above the OpenAI Codex startup card, the 18 source rows fit
inside ``DIALOG_REGION_LINES=20``, so the card does NOT push the dialog out of
the tail — there is no tail-composition defect on the byte-exact pane. This
module tests the BUILT fix: the polling lifetime keeps evaluating past the base
``timeout`` for as long as a whitelisted dialog is provably on screen (bounded
by the hard cap), which is what catches a dialog that renders after the base
window.

Kiro's F589 "connection interrupted" leg is DEFERRED — the F589 screen was not
captured in the corpus (INDEX.md gap: kiro DIALOG_BLOCKED has no live screen).
"""

import asyncio

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus


class _FakeClock:
    """Deterministic monotonic clock; advances only when the loop sleeps."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _install_clock_and_sleep(monkeypatch, clock: _FakeClock):
    import cli_agent_orchestrator.services.terminal_service as ts

    monkeypatch.setattr(ts.time, "monotonic", clock.monotonic)

    async def fake_sleep(s):
        # The loop's poll sleep and the initial grace both advance the clock.
        clock.advance(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


class TestF582D21DialogClearLifetime:
    """AC8 arming leg: the loop keeps evaluating past the base ``timeout``."""

    @pytest.mark.asyncio
    async def test_late_render_after_base_timeout_still_clears(self, monkeypatch):
        """A dialog that renders AFTER the base ``timeout`` is still dismissed.

        The seat sits WAITING with a whitelisted dialog pending well past the
        base ``timeout`` (5s here); the fix keeps re-arming ``on_screen`` until
        the responder's fire clears it (status flips to IDLE) at t≈8s — inside
        the hard lifetime cap. Without the lifetime extension the loop would
        abandon at 5s and never dismiss it (see the mutant test below).
        """
        import cli_agent_orchestrator.services.terminal_service as ts

        clock = _FakeClock()
        _install_clock_and_sleep(monkeypatch, clock)

        on_screen_fires: list[float] = []

        class FakeStatusMonitor:
            def get_status(self, tid):
                # Dialog present (WAITING) until the fire takes at t≈8s.
                if clock.t < 8.0:
                    return TerminalStatus.WAITING_USER_ANSWER
                return TerminalStatus.IDLE

            def get_rendered_screen(self, tid):
                return ["Do you trust the contents of this directory?"]

        class FakeAutoResponder:
            def waiting_gate(self, tid):
                return None

            def match_verdict(self, provider_name, lines, terminal_id=None):
                # A whitelisted dialog is pending until the fire takes.
                return object() if clock.t < 8.0 else None

            def on_screen(self, tid, provider, lines):
                on_screen_fires.append(clock.t)
                return None

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor",
            FakeStatusMonitor(),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.auto_responder.auto_responder",
            FakeAutoResponder(),
        )
        monkeypatch.setattr(ts, "get_terminal_metadata", lambda tid: {"provider": "codex"})

        await ts._wait_for_auto_responder_dialog_clear("term1", "gen1", None, timeout=5.0)

        # The loop kept re-arming on_screen PAST the 5s base timeout (the #386
        # class), and at least one re-arm landed after t=5s.
        assert any(t >= 5.0 for t in on_screen_fires), (
            f"lifetime did not extend past base timeout; fires at {on_screen_fires}"
        )
        # It stopped once the dialog cleared (well under the hard cap).
        assert clock.t < ts._F491_DIALOG_CLEAR_MAX_LIFETIME

    @pytest.mark.asyncio
    async def test_static_stuck_dialog_bounded_by_lifetime_cap(self, monkeypatch):
        """Two fires without clearing → the loop is bounded by the hard cap.

        A genuinely stuck dialog (never clears, no waiting_gate) must not block
        forever: the polling lifetime is capped at
        ``_F491_DIALOG_CLEAR_MAX_LIFETIME`` and the loop then proceeds (the
        caller's retry / the human escalation path takes over).
        """
        import cli_agent_orchestrator.services.terminal_service as ts

        clock = _FakeClock()
        _install_clock_and_sleep(monkeypatch, clock)

        class FakeStatusMonitor:
            def get_status(self, tid):
                return TerminalStatus.WAITING_USER_ANSWER  # never clears

            def get_rendered_screen(self, tid):
                return ["Do you trust the contents of this directory?"]

        class FakeAutoResponder:
            def waiting_gate(self, tid):
                return None

            def match_verdict(self, provider_name, lines, terminal_id=None):
                return object()  # always pending

            def on_screen(self, tid, provider, lines):
                return None

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor",
            FakeStatusMonitor(),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.auto_responder.auto_responder",
            FakeAutoResponder(),
        )
        monkeypatch.setattr(ts, "get_terminal_metadata", lambda tid: {"provider": "codex"})

        await ts._wait_for_auto_responder_dialog_clear("term1", "gen1", None, timeout=5.0)

        # Bounded: the loop proceeded at the hard cap, not the base timeout, and
        # not unbounded.
        assert clock.t >= ts._F491_DIALOG_CLEAR_MAX_LIFETIME
        # Sanity: the cap is a bounded, finite value greater than the base window.
        assert 5.0 < ts._F491_DIALOG_CLEAR_MAX_LIFETIME < 600.0

    @pytest.mark.asyncio
    async def test_no_dialog_returns_at_base_timeout_not_cap(self, monkeypatch):
        """Regression: with NO dialog pending, the loop still bounds at the base
        ``timeout`` — the lifetime extension only applies while a dialog is
        provably on screen, so the no-dialog common case is unchanged."""
        import cli_agent_orchestrator.services.terminal_service as ts

        clock = _FakeClock()
        _install_clock_and_sleep(monkeypatch, clock)

        class FakeStatusMonitor:
            # Stuck WAITING per raw status, but the responder sees NO whitelisted
            # dialog (e.g. a false-WAITING parse) — so lifetime must NOT extend.
            def get_status(self, tid):
                return TerminalStatus.WAITING_USER_ANSWER

            def get_rendered_screen(self, tid):
                return ["some unrelated agent output"]

        class FakeAutoResponder:
            def waiting_gate(self, tid):
                return None

            def match_verdict(self, provider_name, lines, terminal_id=None):
                return None  # nothing whitelisted pending

            def on_screen(self, tid, provider, lines):
                return None

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor",
            FakeStatusMonitor(),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.auto_responder.auto_responder",
            FakeAutoResponder(),
        )
        monkeypatch.setattr(ts, "get_terminal_metadata", lambda tid: {"provider": "codex"})

        await ts._wait_for_auto_responder_dialog_clear("term1", "gen1", None, timeout=5.0)

        # Bounded at the base window (± one poll interval), NOT extended to the cap.
        assert clock.t < ts._F491_DIALOG_CLEAR_MAX_LIFETIME
        assert clock.t < 5.0 + 2 * ts._F491_DIALOG_CLEAR_POLL_INTERVAL + 1.0
