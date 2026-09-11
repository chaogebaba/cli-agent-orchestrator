"""F810 (#667) D3: one ``native_unreachable`` condition per ejection episode.

The transport-ejection service must emit its condition ONCE per episode, not on
every refusal (the named mutant: emits every attempt).

WP-ARCH 3c K1: this file also carried the ``supervisor_drain`` digest-envelope
golden strings and the hook's in-process-subagent gate. That hook is deleted —
the server-side delivery tick is the seat's single carrier — so only the
ejection half survives.
"""

from __future__ import annotations

from cli_agent_orchestrator.services.transport_ejection import TransportEjectionService

# ── D3: one native_unreachable condition per ejection episode ────────────────────


def _eject(svc: TransportEjectionService, tid: str = "t1", rung: str = "fallback") -> None:
    for _ in range(svc.EJECTION_THRESHOLD):
        svc.record_refusal(tid, rung, "not_registered_fallback")


def test_emit_only_after_ejected():
    svc = TransportEjectionService()
    calls: list = []
    sink = lambda tid, label: calls.append((tid, label))  # noqa: E731
    # Not yet ejected → no emit.
    svc.record_refusal("t1", "fallback", "not_registered_fallback")
    assert svc.emit_native_unreachable("t1", "fallback", fleet_sink=sink) is False
    assert calls == []


def test_emit_exactly_once_per_episode():
    svc = TransportEjectionService()
    calls: list = []
    sink = lambda tid, label: calls.append((tid, label))  # noqa: E731
    _eject(svc)
    assert svc.emit_native_unreachable("t1", "fallback", fleet_sink=sink) is True
    # Any number of later attempts in the SAME episode emit nothing more.
    for _ in range(5):
        assert svc.emit_native_unreachable("t1", "fallback", fleet_sink=sink) is False
    assert len(calls) == 1
    assert calls[0][0] == "t1"
    assert calls[0][1].startswith("native_unreachable retry_after=")


def test_emit_carries_retry_after_seconds():
    svc = TransportEjectionService()
    calls: list = []
    sink = lambda tid, label: calls.append((tid, label))  # noqa: E731
    _eject(svc)
    svc.emit_native_unreachable("t1", "fallback", fleet_sink=sink)
    # First episode duration = base_ejection_s * 1 (default 30s).
    assert "retry_after=30s" in calls[0][1]


def test_readmit_allows_next_episode_to_emit_again():
    svc = TransportEjectionService()
    calls: list = []
    sink = lambda tid, label: calls.append((tid, label))  # noqa: E731
    _eject(svc)
    svc.emit_native_unreachable("t1", "fallback", fleet_sink=sink)
    svc.readmit("t1", "fallback")
    _eject(svc)
    assert svc.emit_native_unreachable("t1", "fallback", fleet_sink=sink) is True
    assert len(calls) == 2


def test_sink_failure_leaves_flag_unset_for_retry():
    svc = TransportEjectionService()
    _eject(svc)

    def boom(tid, label):
        raise RuntimeError("sink down")

    assert svc.emit_native_unreachable("t1", "fallback", fleet_sink=boom) is False
    # A later attempt with a working sink still emits (flag was not set on error).
    calls: list = []
    assert (
        svc.emit_native_unreachable(
            "t1", "fallback", fleet_sink=lambda tid, label: calls.append((tid, label))
        )
        is True
    )
    assert len(calls) == 1
