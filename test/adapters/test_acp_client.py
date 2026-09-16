"""D3's client against a real mock agent over a real pipe.

Every arm here spawns a subprocess and frames JSON over stdio, because the
properties are about the WIRE: a second prompt never leaving CAO, a cancel
settling with the right stop reason, an option chosen by kind rather than by id,
a process group that dies even when SIGTERM is ignored.  An in-process double
would satisfy all of these assertions while the shipped client wrote nothing.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.acp.client import (
    ACP_PROTOCOL_VERSION,
    DENIAL_KINDS,
    PERMISSION_KINDS,
    AcpClient,
    AcpFrameLog,
    PromptRefused,
    select_permission_option,
)

_MOCK = str(Path(__file__).resolve().parents[1] / "helpers" / "acp_mock_agent.py")


def _spawn(tmp_path: Path, **overrides: str) -> AcpClient:
    env = dict(os.environ)
    env.update(overrides)
    env.setdefault("MOCK_ACP_TURN_SECONDS", "3")
    client = AcpClient(
        [sys.executable, _MOCK],
        frame_log=AcpFrameLog(tmp_path / "frames.jsonl"),
        env=env,
        stderr_path=tmp_path / "agent.err",
    ).start()
    client.initialize(timeout=20)
    client.session_new(cwd=str(tmp_path), timeout=20)
    return client


@pytest.fixture
def client(tmp_path: Path) -> Iterator[AcpClient]:
    started = _spawn(tmp_path)
    yield started
    started.terminate_process_group(grace_s=2)
    started.close()


# ------------------------------------------------------------- handshake


def test_initialize_and_session_new_round_trip(client: AcpClient) -> None:
    state = client.session_state()
    assert state.session_id == "mock-session-1"
    assert state.turn_open is False


def test_the_frame_log_records_both_directions(client: AcpClient, tmp_path: Path) -> None:
    """AC-S1.2's second clause, and the substrate AC-S1.19/S1.21 assert over."""
    frames = AcpFrameLog(tmp_path / "frames.jsonl").frames()
    directions = {frame["dir"] for frame in frames}
    assert ">>" in directions and "<<" in directions
    methods = [f["frame"].get("method") for f in frames if f["dir"] == ">>"]
    assert "initialize" in methods and "session/new" in methods


def test_v1_is_what_is_negotiated(tmp_path: Path) -> None:
    """D4: v1 is frozen.  AC-S0.6 confirmed both SDK releases pin protocol 1,
    so there is no v2 to negotiate and no flag to hide one behind."""
    assert ACP_PROTOCOL_VERSION == 1
    client = _spawn(tmp_path)
    try:
        sent = [
            f["frame"]
            for f in AcpFrameLog(tmp_path / "frames.jsonl").frames()
            if f["dir"] == ">>" and f["frame"].get("method") == "initialize"
        ]
        assert sent[0]["params"]["protocolVersion"] == 1
    finally:
        client.terminate_process_group(grace_s=2)
        client.close()


def test_steering_capability_is_discovered_not_assumed(tmp_path: Path) -> None:
    """F4b: the WHOLE ``_meta`` is inspected, because two of the three wire
    shapes are vendor-namespaced and one is off-protocol entirely."""
    quiet = _spawn(tmp_path / "quiet")
    try:
        assert quiet.session_state().steering_advertised is False
    finally:
        quiet.terminate_process_group(grace_s=2)
        quiet.close()

    loud = _spawn(tmp_path / "loud", MOCK_ACP_STEERING="1")
    try:
        assert loud.session_state().steering_advertised is True
    finally:
        loud.terminate_process_group(grace_s=2)
        loud.close()


# ------------------------------------------------- the client-side busy model


def test_a_second_prompt_is_refused_and_never_written(client: AcpClient, tmp_path: Path) -> None:
    """AC-S1.3 / AC-S1.19: no second ``session/prompt`` leaves CAO mid-turn.

    Asserted from the FRAME LOG, not from the absence of an error — which is the
    AC's own wording, and the distinction that matters: S0 proved the fleet
    reports no busy class, so an assertion that waited for an error would pass
    against an agent that silently accepted the second prompt and interleaved it.
    """
    client.prompt("first")
    assert client.session_state().turn_open is True
    with pytest.raises(PromptRefused):
        client.prompt("second")

    prompts = [
        f
        for f in AcpFrameLog(tmp_path / "frames.jsonl").frames()
        if f["dir"] == ">>" and f["frame"].get("method") == "session/prompt"
    ]
    assert len(prompts) == 1, "exactly one prompt may reach the wire"


def test_the_turn_reopens_after_a_stop_reason(client: AcpClient) -> None:
    client.prompt("first")
    assert client.await_stop_reason(timeout=20) == "end_turn"
    assert client.session_state().turn_open is False
    client.prompt("second")  # must not raise


def test_no_busy_leak_is_observed_against_a_conforming_agent(client: AcpClient) -> None:
    """AC-S1.19's negative arm: ``DIAG-ACP-BUSY-LEAK`` fires only if a
    busy-shaped error is ever RECEIVED.  Against a conforming agent, never."""
    client.prompt("first")
    client.await_stop_reason(timeout=20)
    assert client.busy_leaks == []


def test_a_closed_transport_ends_the_turn(tmp_path: Path) -> None:
    """A dead agent must not look permanently busy.

    Leaving ``turn_open`` set when the stream ends would stall every row
    addressed to that terminal behind a stop reason that can never arrive — a
    strictly worse failure than delivering to a terminal that is gone, because
    the second is visible and the first is not.
    """
    client = _spawn(tmp_path, MOCK_ACP_TURN_SECONDS="30")
    client.prompt("long")
    assert client.session_state().turn_open is True
    client.terminate_process_group(grace_s=2)
    assert client.await_stop_reason(timeout=10) == "transport_closed"
    assert client.session_state().turn_open is False
    client.close()


# ----------------------------------------------------------------- cancel


def test_cancel_settles_the_open_turn_with_the_cancelled_stop_reason(
    tmp_path: Path,
) -> None:
    client = _spawn(tmp_path, MOCK_ACP_TURN_SECONDS="30")
    try:
        client.prompt("long")
        client.cancel()
        assert client.await_stop_reason(timeout=20) == "cancelled"
        assert client.session_state().turn_open is False
    finally:
        client.terminate_process_group(grace_s=2)
        client.close()


def test_an_agent_that_never_settles_a_cancel_times_out_honestly(tmp_path: Path) -> None:
    """The FAIL cell four adapters showed, reproduced.

    ``None`` is the answer, and the caller quarantines.  Returning ``"cancelled"``
    on a timeout would make an agent that never answered indistinguishable from
    one that did, which is exactly what the certification row exists to
    distinguish.
    """
    client = _spawn(tmp_path, MOCK_ACP_TURN_SECONDS="30", MOCK_ACP_CANCEL="ignore")
    try:
        client.prompt("long")
        client.cancel()
        assert client.await_stop_reason(timeout=1.5) is None
    finally:
        client.terminate_process_group(grace_s=2)
        client.close()


def test_an_agent_that_answers_end_turn_is_not_read_as_cancelled(tmp_path: Path) -> None:
    """cline's measured queued-arm behaviour: it settles, with the WRONG reason.

    The client reports what arrived.  The typed certification reason
    ``CANCEL_STOPREASON_UNRELIABLE`` is what that becomes upstream; down here the
    only job is not to launder it into ``cancelled``.
    """
    client = _spawn(tmp_path, MOCK_ACP_TURN_SECONDS="30", MOCK_ACP_CANCEL="end_turn")
    try:
        client.prompt("long")
        client.cancel()
        assert client.await_stop_reason(timeout=20) == "end_turn"
    finally:
        client.terminate_process_group(grace_s=2)
        client.close()


def test_the_frame_log_names_the_cancelled_tool_call(tmp_path: Path) -> None:
    """AC-S1.21's fails-if includes "the cancelled tool call cannot be named from
    the frames", which would make D6b's cost unauditable."""
    client = _spawn(tmp_path, MOCK_ACP_TURN_SECONDS="30")
    try:
        client.prompt("long")
        client.cancel()
        client.await_stop_reason(timeout=20)
        ids = {
            (f["frame"].get("params") or {}).get("update", {}).get("toolCallId")
            for f in AcpFrameLog(tmp_path / "frames.jsonl").frames()
            if f["dir"] == "<<"
        }
        assert any(i for i in ids if i)
    finally:
        client.terminate_process_group(grace_s=2)
        client.close()


# ------------------------------------------------------------ permissions


def test_an_option_is_selected_by_kind_never_by_id(tmp_path: Path) -> None:
    """AC-S0.9: the kinds are portable across all five adapters that ask; the ids
    are not — codex answers ``accept_execpolicy_amendment`` where others answer
    ``allow``.  The mock uses vendor-shaped ids so an id-based selector fails."""
    client = _spawn(tmp_path, MOCK_ACP_PERMISSION="full", MOCK_ACP_TURN_SECONDS="2")
    try:
        client.prompt("do a thing")
        client.await_stop_reason(timeout=20)
        answers = [
            f["frame"]
            for f in AcpFrameLog(tmp_path / "frames.jsonl").frames()
            if f["dir"] == ">>" and "result" in f["frame"] and "outcome" in str(f["frame"])
        ]
        assert answers, "the client must answer a permission request"
        outcome = answers[0]["result"]["outcome"]
        assert outcome["outcome"] == "selected"
        assert outcome["optionId"] == "vendor-allow-once"
    finally:
        client.terminate_process_group(grace_s=2)
        client.close()


def test_reject_always_is_not_expressible_anywhere_in_the_fleet() -> None:
    """AC-S0.9's measured finding, recorded as a test rather than as prose.

    All five adapters that ask supply exactly ``allow_once`` / ``allow_always`` /
    ``reject_once``.  "Deny and remember" has no wire representation, so the
    preference list must not contain one — a policy that could ask for it would
    silently degrade to a different answer on every adapter.
    """
    assert PERMISSION_KINDS == ("allow_once", "allow_always", "reject_once")
    assert "reject_always" not in PERMISSION_KINDS
    assert "reject_always" in DENIAL_KINDS, "it is a DENIAL intent even though nobody offers it"


def test_a_denial_with_no_denial_option_cancels_first_then_answers_cancelled(
    tmp_path: Path,
) -> None:
    """D11's timeout path, exactly: cancel, THEN ``cancelled``.

    ``cancelled`` is the only representable no-answer outcome, and answering it
    without cancelling would be a lie about the agent's state.  Cancelling first
    is what makes it conformant.
    """
    assert (
        select_permission_option([{"kind": "allow_once", "optionId": "a"}], prefer="reject_once")
        is None
    )

    client = AcpClient(
        [sys.executable, _MOCK],
        frame_log=AcpFrameLog(tmp_path / "frames.jsonl"),
        env={**os.environ, "MOCK_ACP_PERMISSION": "no_denial", "MOCK_ACP_TURN_SECONDS": "2"},
        permission_policy=lambda _params: "reject_once",
        stderr_path=tmp_path / "agent.err",
    ).start()
    try:
        client.initialize(timeout=20)
        client.session_new(cwd=str(tmp_path), timeout=20)
        client.prompt("do a thing")
        client.await_stop_reason(timeout=20)
        outgoing = [
            f["frame"] for f in AcpFrameLog(tmp_path / "frames.jsonl").frames() if f["dir"] == ">>"
        ]
        cancel_index = next(
            i for i, f in enumerate(outgoing) if f.get("method") == "session/cancel"
        )
        cancelled_index = next(
            i
            for i, f in enumerate(outgoing)
            if (f.get("result") or {}).get("outcome", {}).get("outcome") == "cancelled"
        )
        assert cancel_index < cancelled_index, "cancel FIRST, then answer cancelled"
    finally:
        client.terminate_process_group(grace_s=2)
        client.close()


# -------------------------------------------------------------- teardown


def test_the_process_group_dies_even_when_sigterm_is_ignored(tmp_path: Path) -> None:
    """The SIGKILL leg is MANDATORY: S0 measured an adapter that never exits on
    SIGTERM, and ``expire_recovery`` requires proven absence, not a request."""
    client = _spawn(tmp_path, MOCK_ACP_IGNORE_SIGTERM="1", MOCK_ACP_TURN_SECONDS="60")
    try:
        assert client.pid is not None
        assert client.terminate_process_group(grace_s=0.5) is True
    finally:
        client.close()


def test_teardown_on_an_unstarted_client_is_a_no_op(tmp_path: Path) -> None:
    client = AcpClient([sys.executable, _MOCK], frame_log=AcpFrameLog(tmp_path / "f.jsonl"))
    assert client.terminate_process_group(grace_s=0.1) is True
    client.close()
