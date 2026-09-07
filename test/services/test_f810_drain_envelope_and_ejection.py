"""F810 (#667): drain digest envelope golden strings + D3 ejection condition.

* the drain envelope text equals the root hook's format for one pending row and
  one suppressed BUSY ping (golden strings) — the header/per-row shape must match
  ``.claude/hooks/supervisor-inbox-drain.sh`` verbatim so nothing that reads the
  digest changes;
* transport_ejection emits exactly one ``native_unreachable`` condition per
  ejection episode (mutant: emits every attempt).
"""

from __future__ import annotations

from cli_agent_orchestrator.hooks.supervisor_drain import _build_digest, _is_busy_suppressible
from cli_agent_orchestrator.services.transport_ejection import TransportEjectionService

# ── drain envelope golden ───────────────────────────────────────────────────────


def test_envelope_header_and_row_shape_one_pending():
    # created_at omitted → no age suffix (age is best-effort in the .sh too).
    items = [{"id": 5, "sender_id": "wrk1", "message": "do the thing"}]
    digest, max_id = _build_digest(items)
    assert max_id == 5
    assert digest is not None
    # Golden header (matches supervisor-inbox-drain.sh).
    assert digest.startswith("[CAO INBOX] 1 message(s) auto-surfaced and acked (ids 5-5):\n")
    # Golden per-row shape.
    assert "\n--- From wrk1 (id 5) ---\ndo the thing\n---" in digest


def test_envelope_multiple_rows_min_max_and_order():
    items = [
        {"id": 9, "sender_id": "b", "message": "second"},
        {"id": 4, "sender_id": "a", "message": "first"},
    ]
    digest, max_id = _build_digest(items)
    assert max_id == 9
    assert digest.startswith("[CAO INBOX] 2 message(s) auto-surfaced and acked (ids 4-9):\n")
    # sorted ascending by id: 'first' (id 4) appears before 'second' (id 9)
    assert digest.index("(id 4)") < digest.index("(id 9)")


def test_busy_ping_is_suppressed_but_still_acked():
    items = [{"id": 8, "sender_id": "x", "message": "[CONDITION] kind=BUSY subtype=spinner"}]
    digest, max_id = _build_digest(items)
    # Suppressed → no digest to inject, but the row still acks (max_id set).
    assert digest is None
    assert max_id == 8


def test_proc_exited_command_exit_code_suppressed():
    body = "[CONDITION] kind=PROC_EXITED subtype=command_exit_code code=1"
    assert _is_busy_suppressible(body) is True


def test_capped_condition_not_suppressed():
    body = "[CONDITION] kind=CAPPED subtype=hard"
    assert _is_busy_suppressible(body) is False
    items = [{"id": 3, "sender_id": "x", "message": body}]
    digest, max_id = _build_digest(items)
    assert digest is not None and "kind=CAPPED" in digest


def test_mixed_busy_and_real_surfaces_only_real():
    items = [
        {"id": 1, "sender_id": "x", "message": "[CONDITION] kind=BUSY"},
        {"id": 2, "sender_id": "y", "message": "real callback"},
    ]
    digest, max_id = _build_digest(items)
    assert max_id == 2
    assert digest is not None
    assert "real callback" in digest
    assert "kind=BUSY" not in digest
    # header count is the FULL item count (both rows acked), matching the .sh
    assert digest.startswith("[CAO INBOX] 2 message(s) auto-surfaced and acked (ids 1-2):\n")


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
