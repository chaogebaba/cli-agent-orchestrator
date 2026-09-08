"""F810 (#667): drain digest envelope golden strings + D3 ejection condition.

* the drain envelope text equals the root hook's format for one pending row and
  one suppressed BUSY ping (golden strings) — the header/per-row shape must match
  ``.claude/hooks/supervisor-inbox-drain.sh`` verbatim so nothing that reads the
  digest changes;
* transport_ejection emits exactly one ``native_unreachable`` condition per
  ejection episode (mutant: emits every attempt).
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

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


# ── BLOCKER 3: in-process-subagent safety gate on the drain ─────────────────────


def _run_drain(stdin: str, monkeypatch):
    """Run supervisor_drain.main() with mocked transport; return (rc, post, get)."""
    from cli_agent_orchestrator.hooks import supervisor_drain

    post = MagicMock()
    get = MagicMock()
    with (
        patch("sys.stdin", io.StringIO(stdin)),
        patch.object(supervisor_drain, "get_local_bearer", return_value=None),
        patch.object(supervisor_drain.cao_http, "post", post),
        patch.object(supervisor_drain.cao_http, "get", get),
    ):
        rc = supervisor_drain.main()
    return rc, post, get


def test_drain_subagent_gate_agent_id_in_stdin_no_transport(monkeypatch):
    """A child event carrying `agent_id` in stdin claims NOTHING: no POST (drain
    trigger + ack), no GET (claim list)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.delenv("CLAUDE_AGENT_ID", raising=False)
    rc, post, get = _run_drain(json.dumps({"agent_id": "child-1"}), monkeypatch)
    assert rc == 0
    post.assert_not_called()
    get.assert_not_called()


def test_drain_subagent_gate_env_discriminator_no_transport(monkeypatch):
    """The CLAUDE_AGENT_ID env discriminator alone also fully gates the drain."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CLAUDE_AGENT_ID", "child-1")
    rc, post, get = _run_drain(json.dumps({"hook_event_name": "Stop"}), monkeypatch)
    assert rc == 0
    post.assert_not_called()
    get.assert_not_called()


def test_drain_no_terminal_id_is_full_noop(monkeypatch):
    """Non-CAO / worker context (no CAO_TERMINAL_ID): zero side effects."""
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    rc, post, get = _run_drain(json.dumps({"hook_event_name": "PostToolUse"}), monkeypatch)
    assert rc == 0
    post.assert_not_called()
    get.assert_not_called()


# ── BLOCKER 5: emitted hookEventName names the event that fired ─────────────────


def _run_drain_capture_envelope(hook_event_name: str, monkeypatch, capsys):
    """Drive main() with one pending row and the given hook_event_name; return the
    parsed stdout envelope."""
    from cli_agent_orchestrator.hooks import supervisor_drain

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.delenv("CLAUDE_AGENT_ID", raising=False)
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")

    drain_resp = MagicMock()
    drain_resp.raise_for_status = MagicMock()
    list_resp = MagicMock()
    list_resp.raise_for_status = MagicMock()
    list_resp.json.return_value = {
        "items": [{"id": 11, "sender_id": "wrk", "message": "real callback"}]
    }

    def fake_get(path, **kw):
        return list_resp

    def fake_post(path, **kw):
        return drain_resp

    stdin = json.dumps({"hook_event_name": hook_event_name})
    with (
        patch("sys.stdin", io.StringIO(stdin)),
        patch.object(supervisor_drain, "get_local_bearer", return_value=None),
        patch.object(supervisor_drain.cao_http, "get", side_effect=fake_get),
        patch.object(supervisor_drain.cao_http, "post", side_effect=fake_post),
    ):
        rc = supervisor_drain.main()
    out = capsys.readouterr().out.strip()
    assert rc == 0
    return json.loads(out)


def test_drain_envelope_hook_event_name_session_start(monkeypatch, capsys):
    env = _run_drain_capture_envelope("SessionStart", monkeypatch, capsys)
    assert env["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "real callback" in env["hookSpecificOutput"]["additionalContext"]


def test_drain_envelope_hook_event_name_post_tool_use(monkeypatch, capsys):
    env = _run_drain_capture_envelope("PostToolUse", monkeypatch, capsys)
    assert env["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_drain_envelope_hook_event_name_stop(monkeypatch, capsys):
    env = _run_drain_capture_envelope("Stop", monkeypatch, capsys)
    assert env["hookSpecificOutput"]["hookEventName"] == "Stop"


def test_drain_envelope_defaults_to_post_tool_use_when_event_absent(monkeypatch, capsys):
    # No hook_event_name in stdin → falls back to PostToolUse (the safe default).
    env = _run_drain_capture_envelope("", monkeypatch, capsys)
    assert env["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
