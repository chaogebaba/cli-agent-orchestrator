"""F935 (#787): launch health must fail a seat the backend never recognises.

`_confirm_launch_health` proves a PROCESS is alive. A wrapper, launcher or
runtime that starts and stays up satisfies that — its foreground process is not
the baseline shell — while the agent inside it never comes up. The seat then
passes launch health carrying no native status: delivery waits on a lifecycle
that never arrives, and the only trace is a DIAG-HERDR-STATUS-UNKNOWN row whose
count climbs quietly.

Measured on herdr 0.9.0, polling from the moment the command was sent:

    live pane   no agent (0s) -> agent named (1s) -> classified idle (4s)
    crashed     agent NEVER named, indefinitely
    bare shell  agent NEVER named, indefinitely
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

METADATA = {"tmux_session": "cao", "tmux_window": "w1-pi"}


def _run(coro):
    return asyncio.run(coro)


# --- the backend probe -----------------------------------------------------


def _backend(payload: str | None, rc: int = 0):
    b = HerdrBackend.__new__(HerdrBackend)
    b._resolve_pane_id_from_window = MagicMock(return_value="w1:p1")  # type: ignore[method-assign]
    b._run_herdr = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(returncode=rc, stdout=payload or "", stderr="")
    )
    return b


class TestTheHerdrProbe:
    def test_an_agent_named_is_true(self):
        b = _backend(json.dumps({"result": {"pane": {"pane_id": "w1:p1", "agent": "pi"}}}))
        assert b.probe_agent_detected("cao", "w1-pi") is True

    def test_no_agent_field_is_false(self):
        b = _backend(json.dumps({"result": {"pane": {"pane_id": "w1:p1"}}}))
        assert b.probe_agent_detected("cao", "w1-pi") is False

    def test_an_empty_agent_string_is_false(self):
        b = _backend(json.dumps({"result": {"pane": {"pane_id": "w1:p1", "agent": ""}}}))
        assert b.probe_agent_detected("cao", "w1-pi") is False

    @pytest.mark.parametrize(
        "payload,rc",
        [(None, 1), ("not json at all", 0), (json.dumps({"result": "scalar"}), 0)],
    )
    def test_a_backend_that_cannot_answer_says_none_not_false(self, payload, rc):
        """A transport fault during startup must never read as a dead seat."""
        b = _backend(payload, rc=rc)
        assert b.probe_agent_detected("cao", "w1-pi") is None

    def test_an_unresolvable_pane_says_none(self):
        from cli_agent_orchestrator.backends.base import TerminalBackendError

        b = _backend(json.dumps({"result": {}}))
        b._resolve_pane_id_from_window = MagicMock(side_effect=TerminalBackendError("gone"))  # type: ignore[method-assign]
        assert b.probe_agent_detected("cao", "w1-pi") is None


class TestTheBaseBackendHasNoOpinion:
    def test_the_port_default_is_none(self):
        """tmux and every other agent-unaware backend must be untouched."""
        from cli_agent_orchestrator.backends.base import TerminalBackend

        assert TerminalBackend.probe_agent_detected(MagicMock(), "s", "w") is None


# --- the gate --------------------------------------------------------------


def _gate(answers, *, has_child=True, wrapped=False, side_effect=None):
    """Drive `_confirm_agent_detected` over a scripted answer sequence.

    EVERY gate test goes through this helper. Hand-rolling the provider is how
    r2's `test_a_probe_that_raises_is_not_a_dead_seat` went vacuous: it set only
    `has_child`, so `launch_hides_agent_from_backend` stayed a bare MagicMock
    attribute — TRUTHY — the gate stood down at the wrapped-launch check, and the
    probe was never called at all (mutant MB survived).

    `side_effect` replaces the scripted answers with a callable, for the cases
    where the probe must RAISE rather than answer.
    """
    from cli_agent_orchestrator.services import terminal_service as ts

    provider = MagicMock()
    provider.has_process_child = has_child
    # Must be set explicitly: a bare MagicMock attribute is TRUTHY, which would
    # look like a wrapped launch and make the gate stand down for every test.
    provider.launch_hides_agent_from_backend = wrapped
    backend = MagicMock(spec=["probe_agent_detected"])
    if side_effect is not None:
        backend.probe_agent_detected = MagicMock(side_effect=side_effect)
    else:
        seq = list(answers)
        backend.probe_agent_detected = MagicMock(
            side_effect=lambda *a, **k: seq.pop(0) if seq else answers[-1]
        )

    with (
        patch.object(ts, "get_terminal_metadata", return_value=METADATA),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.core.timing.HERDR_AGENT_DETECT_S", 0.3),
        patch.object(ts, "CONFIRM_LAUNCH_HEALTH_POLL_INTERVAL", 0.05),
    ):
        _run(ts._confirm_agent_detected("t1", provider))
    return backend


class TestTheGate:
    def test_an_agent_that_appears_late_passes(self):
        backend = _gate([False, False, True])
        assert backend.probe_agent_detected.call_count >= 3

    def test_an_agent_that_never_appears_fails_the_launch(self):
        from cli_agent_orchestrator.services.terminal_service import ProviderLaunchFailed

        with pytest.raises(ProviderLaunchFailed) as exc:
            _gate([False])
        # r2 N1: its OWN typed code, so the API body and the supervisor notice
        # can tell this apart from a dead-process failure.
        assert str(exc.value) == "agent_never_detected"
        assert "never detected an agent" in exc.value.detail

    def test_a_backend_with_no_opinion_skips_the_gate_entirely(self):
        """The base-class default. tmux must not be able to fail this way."""
        backend = _gate([None])
        assert backend.probe_agent_detected.call_count == 1

    def test_a_process_less_provider_is_not_gated(self):
        backend = _gate([False], has_child=False)
        backend.probe_agent_detected.assert_not_called()

    def test_a_probe_that_raises_in_the_loop_is_not_a_dead_seat(self):
        """A backend that throws has NO OPINION; it has not proven a dead seat.

        r2's version of this test hand-rolled its provider and left
        `launch_hides_agent_from_backend` a truthy MagicMock, so the gate stood
        down before probing and the assertion proved nothing — mutant MB (in-loop
        handler re-raises) survived it. Going through `_gate` fixes that.
        """
        backend = _gate(None, side_effect=RuntimeError("socket gone"))
        assert (
            backend.probe_agent_detected.call_count >= 1
        ), "the gate must actually reach the probe for this test to mean anything"

    def test_a_probe_that_raises_on_the_FINAL_ask_is_not_a_dead_seat(self):
        """r2 review B4: the confirming ask after the deadline.

        The guard used to wrap only the in-loop call, so a probe that answered
        during the window and then threw on the confirming ask escaped as a raw
        exception into deferred init — a teardown and an HTTP 500 for a seat that
        may be alive. This scripts exactly that: definite answers, then a throw.
        """
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] <= 3:
                return False
            raise RuntimeError("socket died before the confirming ask")

        backend = _gate(None, side_effect=flaky)
        assert calls["n"] > 3, "the final ask must have been reached"

    def test_an_early_no_then_lost_contact_does_NOT_kill_the_seat(self):
        """r2 B1, the defect this replaces.

        At t=0 a healthy pane answers "no agent" — that is the measured normal
        state, not a verdict. r1 latched on that first sample, so a backend that
        went quiet one poll later tore a LIVE seat down, and did it exactly in
        the population the 30s window exists to protect. The decision is now the
        last definite answer, so an early no followed by silence returns.
        """
        from cli_agent_orchestrator.services import terminal_service as ts

        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_hides_agent_from_backend = False
        backend = MagicMock(spec=["probe_agent_detected"])
        seq = [False, None, None, None, None, None, None, None, None, None]
        backend.probe_agent_detected = MagicMock(
            side_effect=lambda *a, **k: seq.pop(0) if seq else None
        )
        with (
            patch.object(ts, "get_terminal_metadata", return_value=METADATA),
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.core.timing.HERDR_AGENT_DETECT_S", 0.3),
            patch.object(ts, "CONFIRM_LAUNCH_HEALTH_POLL_INTERVAL", 0.05),
        ):
            _run(ts._confirm_agent_detected("t1", provider))  # must not raise

    def test_a_definite_no_at_the_deadline_does_fail(self):
        """The other half: sustained, confirmed absence still fails the seat."""
        from cli_agent_orchestrator.services.terminal_service import AgentNeverDetected

        with pytest.raises(AgentNeverDetected):
            _gate([False])

    def test_an_early_no_that_becomes_yes_passes(self):
        """The measured healthy timeline: no agent at 0s, named at 1s."""
        _gate([False, False, True])


class TestAWrappedLaunchIsNotCondemned:
    """r2 B2: a wrapped launch hides the agent from the backend BY DESIGN.

    `herdr_backend.fetch_native_status` and `providers/base._resolve_native_status`
    both document that condition as healthy, resolved by buffer analysis. The
    gate must stand down rather than declare those seats dead 30s in.
    """

    def test_the_gate_stands_down_for_a_wrapped_launch(self):
        backend = _gate([False], wrapped=True)
        backend.probe_agent_detected.assert_not_called()

    def test_it_says_so(self, caplog):
        with caplog.at_level("INFO", logger="cli_agent_orchestrator.services.terminal_service"):
            _gate([False], wrapped=True)
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "f935_gate_skipped" in m and "launch_hides_agent_from_backend" in m for m in msgs
        ), msgs

    def test_the_default_is_gated_so_a_new_provider_is_protected(self):
        from cli_agent_orchestrator.providers.base import BaseProvider

        assert BaseProvider.launch_hides_agent_from_backend is False


class TestTheFailureReusesTheExistingShape:
    def test_it_is_a_provider_launch_failed(self):
        from cli_agent_orchestrator.services.terminal_service import ProviderLaunchFailed

        with pytest.raises(ProviderLaunchFailed):
            _gate([False])

    def test_the_message_explains_the_distinction(self):
        from cli_agent_orchestrator.services.terminal_service import ProviderLaunchFailed

        with pytest.raises(ProviderLaunchFailed) as exc:
            _gate([False])
        msg = exc.value.detail
        assert "a process is running" in msg
        assert "no native status" in msg


class TestTheTypedCodeReachesTheOperator:
    """r2 N1: `str(exc)` is what the API body and the supervisor notice render."""

    def test_it_has_its_own_code_not_the_dead_process_one(self):
        from cli_agent_orchestrator.services.terminal_service import (
            AgentNeverDetected,
            ProviderLaunchFailed,
        )

        exc = AgentNeverDetected("no agent for t1")
        assert str(exc) == "agent_never_detected"
        assert str(ProviderLaunchFailed("x")) == "provider_launch_failed"

    def test_the_existing_handlers_still_catch_it(self):
        """A subclass, so every `except ProviderLaunchFailed` keeps working."""
        from cli_agent_orchestrator.services.terminal_service import (
            AgentNeverDetected,
            ProviderLaunchFailed,
            _DeferredInitFailure,
        )

        exc = AgentNeverDetected("d")
        assert isinstance(exc, ProviderLaunchFailed)
        assert isinstance(exc, _DeferredInitFailure)

    def test_the_code_is_registered_where_teardown_reads_it(self):
        from cli_agent_orchestrator.services.terminal_service import (
            _PERSIST_FAILURE_CODES,
            _PRE_DELIVERY_CODES,
            AgentNeverDetected,
            _failure_code,
        )

        assert _failure_code(AgentNeverDetected("d")) == "agent_never_detected"
        assert "agent_never_detected" in _PERSIST_FAILURE_CODES
        assert "agent_never_detected" in _PRE_DELIVERY_CODES

    def test_the_deferred_notice_carries_the_prose(self):
        """The deferred path settles with `reason=repr(e)`."""
        from cli_agent_orchestrator.services.terminal_service import AgentNeverDetected

        assert "no agent was recognised" in repr(AgentNeverDetected("no agent was recognised"))


class TestThePaneIsResolvedOncePerGate:
    """r2 N4: the resolver never caches, so a resolve per poll is ~4 subprocesses."""

    def test_the_resolver_runs_once_across_many_polls(self):
        from cli_agent_orchestrator.services import terminal_service as ts

        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_hides_agent_from_backend = False
        backend = MagicMock(spec=["probe_agent_detected", "_resolve_pane_id_from_window"])
        backend._resolve_pane_id_from_window = MagicMock(return_value="w1:p1")
        seq = [False] * 20
        backend.probe_agent_detected = MagicMock(
            side_effect=lambda *a, **k: seq.pop(0) if seq else False
        )

        with (
            patch.object(ts, "get_terminal_metadata", return_value=METADATA),
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.core.timing.HERDR_AGENT_DETECT_S", 0.25),
            patch.object(ts, "CONFIRM_LAUNCH_HEALTH_POLL_INTERVAL", 0.02),
            pytest.raises(Exception),
        ):
            _run(ts._confirm_agent_detected("t1", provider))

        assert backend._resolve_pane_id_from_window.call_count == 1
        assert backend.probe_agent_detected.call_count > 1
        # and the hint was actually passed through
        assert backend.probe_agent_detected.call_args.kwargs.get("pane_id") == "w1:p1"


class TestTheGateIsWiredIntoBothLaunchPaths:
    """r2 B3: deleting BOTH call sites left the whole suite green.

    Nothing proved the gate is reachable in production, so a refactor could
    silently disconnect it. These read the source of the two launch paths,
    because that is what a deletion mutant changes and what a mock of the
    function itself cannot see.
    """

    def _source_of(self, fn):
        import inspect

        return inspect.getsource(fn)

    def test_the_deferred_path_awaits_the_gate(self):
        from cli_agent_orchestrator.services import terminal_service as ts

        src = self._source_of(ts._schedule_deferred_init)
        assert "_confirm_launch_health(" in src, "precondition: liveness check present"
        assert (
            "await _confirm_agent_detected(" in src
        ), "the deferred launch path no longer runs the F935 gate"

    def test_the_sync_create_path_awaits_the_gate(self):
        from cli_agent_orchestrator.services import terminal_service as ts

        src = self._source_of(ts.create_terminal)
        assert (
            "await _confirm_agent_detected(" in src
        ), "the synchronous create path no longer runs the F935 gate"

    def test_the_sync_gate_sits_behind_the_pinned_incarnation_boundary(self):
        """A real behavioural rule: a sync create with no pinned incarnation
        skips BOTH checks. Recorded so a move is a deliberate decision."""
        from cli_agent_orchestrator.services import terminal_service as ts

        src = self._source_of(ts.create_terminal)
        guard = "if _f138_incarnation_id is not None:"
        assert guard in src
        after = src.split(guard, 1)[1]
        gate_at = after.index("await _confirm_agent_detected(")
        health_at = after.index("await _confirm_launch_health(")
        assert health_at < gate_at, "the gate must follow the liveness check"
        # both inside the same guarded block, at the same depth
        assert after[:gate_at].count("\n                await _confirm_launch_health(") == 1
