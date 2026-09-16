"""B1 — the ACP plane's composition root, as a PERMANENT arm.

The S1 review's `probes/liveness_probe.py` asked five questions of the production
expressions and got five answers that meant the plane could not deliver anything.
This is that probe kept in the tree, with each fact asserted in its flipped form
so it cannot silently un-flip.

Two of the reviewer's printed facts are asserted here in a DIFFERENT shape than
he printed them, and the difference is the point rather than a dodge:

* ARM 1 printed `acp_session_unbound` against an EMPTY registry, which is the
  correct answer to "deliver to a terminal that has no seat". The defect was that
  there was no way to bind one. So the arm here binds a real mock ACP seat over a
  real pipe and asserts the wake is DELIVERED — and keeps the unbound case as its
  control, because the typed reason must still be reachable.
* ARM 3 printed `DeliveryTick.receiver_tasks default: None`, which AC-S1.1
  REQUIRES: a native deployment must construct no ACP object at all. The defect
  was that nothing ever passed one. So the arm asserts the default stays `None`
  AND that the composition root passes a real registry when the switch is armed.
"""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.acp.client import AcpClient, AcpFrameLog
from cli_agent_orchestrator.adapters.acp.registry import AcpSessionRegistry, acp_sessions
from cli_agent_orchestrator.adapters.acp.session import AcpAgentSession, AcpMessageTransport
from cli_agent_orchestrator.core.ports import AgentSession, MessageTransport
from cli_agent_orchestrator.services.queue_carrier import AcpTransport, NativeSeatCarrier

_MOCK = str(Path(__file__).resolve().parents[1] / "helpers" / "acp_mock_agent.py")


@pytest.fixture
def seat(tmp_path: Path) -> Iterator[AcpClient]:
    """A real ACP subprocess over a real pipe, bound into a private registry."""
    env = dict(os.environ)
    env["MOCK_ACP_TURN_SECONDS"] = "2"
    client = AcpClient(
        [sys.executable, _MOCK],
        frame_log=AcpFrameLog(tmp_path / "frames.jsonl"),
        env=env,
        stderr_path=tmp_path / "agent.err",
    ).start()
    client.initialize(timeout=20)
    client.session_new(cwd=str(tmp_path), timeout=20)
    yield client
    client.terminate_process_group(grace_s=2)
    client.close()


# ============================================================ ARM 1


def test_a_bound_seat_actually_receives_a_wake(
    seat: AcpClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fact the whole blocker rested on: a wake reaches a live subprocess.

    Driven through the PRODUCTION carrier expression — the same
    ``_build_seat_carrier`` call ``_build_delivery_tick`` makes — with the switch
    armed and the terminal selected, exactly as the reviewer's ARM 1 drove it.
    """
    # The switch is STRUCTURAL: ``_build_seat_carrier`` reads it once and returns
    # the bare native carrier when it is off, so an armed switch is part of the
    # production expression rather than a detail of the test.
    monkeypatch.setenv("CAO_SEAT_TRANSPORT", "acp")
    registry = AcpSessionRegistry()
    registry.bind("term-acp", seat)
    carrier = bootstrap._build_seat_carrier(
        NativeSeatCarrier(),
        lambda: AcpTransport(registry.get),
        predicate=lambda _t: True,
    )
    emission = carrier.emit(
        terminal_id="term-acp",
        line="[cb-1] the supervisor asks for a status line",
        sender_key="w",
        sender_name="w",
        msg_id="cb-1",
    )
    assert emission.reason is None, f"the wake was refused: {emission.reason}"
    assert emission.verified is True

    prompts = [
        f
        for f in AcpFrameLog(tmp_path / "frames.jsonl").frames()
        if f["dir"] == ">>" and f["frame"].get("method") == "session/prompt"
    ]
    assert len(prompts) == 1
    assert "status line" in str(prompts[0]["frame"]["params"]["prompt"])


def test_an_unbound_terminal_still_reports_the_typed_reason() -> None:
    """The control. Binding a seat must not remove the answer for a terminal
    that has none — that reason is bounded by the row's own deadline and heals
    when the seat's session binds."""
    carrier = AcpTransport(AcpSessionRegistry().get)
    emission = carrier.emit(
        terminal_id="nobody", line="x", sender_key="w", sender_name="w", msg_id="m"
    )
    assert emission.reason == AcpTransport.UNBOUND_REASON


def test_the_default_resolver_is_the_real_registry_not_none() -> None:
    """The review's B1.1 in one line: a default that cannot work is not neutral."""
    assert AcpTransport()._sessions is not None
    assert AcpTransport()._sessions == acp_sessions.get


# ============================================================ ARM 2


def test_a_public_path_can_create_a_transport_acp_terminal() -> None:
    """``terminal_service.create_terminal`` is the seam every spawn funnels
    through (the cell-guard seam), so a ``transport`` parameter anywhere else
    cannot produce an ACP row."""
    from cli_agent_orchestrator.clients import database as db
    from cli_agent_orchestrator.services import terminal_service

    assert "transport" in inspect.signature(terminal_service.create_terminal).parameters
    assert "transport" in inspect.signature(db.create_terminal).parameters


def test_the_acp_branch_creates_no_pane_resource() -> None:
    """D2: a headless seat takes none of the pane path.

    Asserted over the source because the alternative is spawning a real backend
    window in a unit test. The three pane steps are named individually so a
    future edit that re-enables one of them for ACP fails here.
    """
    from cli_agent_orchestrator.services import terminal_service

    source = inspect.getsource(terminal_service.create_terminal)
    assert 'if transport == "acp":' in source, "no ACP branch in the create path"
    assert (
        'transport != "acp" and not get_backend().supports_event_inbox()' in source
    ), "the FIFO/pipe-pane block is not gated on the transport"


# ============================================================ ARM 3


def test_the_tick_default_stays_none_and_the_root_passes_a_registry() -> None:
    """Both halves. The default is AC-S1.1's requirement; the wiring is B1.3's."""
    from cli_agent_orchestrator.app.delivery.tick import DeliveryTick

    assert inspect.signature(DeliveryTick.__init__).parameters["receiver_tasks"].default is None

    source = inspect.getsource(bootstrap._build_delivery_tick)
    assert "receiver_tasks=" in source
    assert "ReceiverTaskRegistry" in source


def test_the_registry_is_none_when_the_switch_is_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-S1.1 as an object graph: native constructs no ACP object at all."""
    monkeypatch.delenv("CAO_SEAT_TRANSPORT", raising=False)
    assert bootstrap._build_acp_receiver_registry(object(), [None]) is None  # type: ignore[arg-type]


def test_the_registry_is_built_when_the_switch_is_armed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS

    from cli_agent_orchestrator.adapters.store.migrator import migrate
    from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
    from cli_agent_orchestrator.app.acp.receiver_task import ReceiverTaskRegistry

    monkeypatch.setenv("CAO_SEAT_TRANSPORT", "acp")
    result, pool = migrate(tmp_path / "root.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    try:
        registry = bootstrap._build_acp_receiver_registry(SqliteQueueStore(pool), [None])
        assert isinstance(registry, ReceiverTaskRegistry)
    finally:
        pool.close_all()


def test_the_interrupt_store_shares_the_queues_own_pool(tmp_path: Path) -> None:
    """D6b(3)'s build-stop condition, checked rather than arranged.

    Every transition writes the state row and I's queue row in one
    ``BEGIN IMMEDIATE``, and SQLite has no cross-file transaction. The aggregate
    takes the pool OFF the queue store, so the two cannot be pointed at
    different files by any later configuration change.
    """
    from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS

    from cli_agent_orchestrator.adapters.store.migrator import migrate
    from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore

    result, pool = migrate(tmp_path / "shared.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    try:
        queue = SqliteQueueStore(pool)
        store = bootstrap._interrupt_store(queue)
        assert store._pool is queue._pool  # type: ignore[attr-defined]
    finally:
        pool.close_all()


# ============================================================ ARM 4


def test_the_two_wire_ports_have_production_implementations() -> None:
    """The review's B1.4: the only implementations were test fakes, so the
    registry had nothing to build a task from."""
    assert isinstance(AcpMessageTransport("t", object()), MessageTransport)
    assert isinstance(AcpAgentSession("t", object()), AgentSession)
    assert AcpMessageTransport.__module__.startswith("cli_agent_orchestrator.adapters.acp")
    assert AcpAgentSession.__module__.startswith("cli_agent_orchestrator.adapters.acp")


def test_the_transport_probe_reads_the_clients_own_state(seat: AcpClient) -> None:
    """``prepare_interrupt`` touches no wire: this client is the only reader of
    its own stream, so the answer is exact rather than probed."""
    from cli_agent_orchestrator.core.interrupt import PreparationKind

    transport = AcpMessageTransport("term-acp", seat)
    assert transport.prepare_interrupt(terminal_id="term-acp").kind is PreparationKind.IDLE

    seat.prompt("a long turn", callback_id="cb-N")
    preparation = transport.prepare_interrupt(terminal_id="term-acp")
    assert preparation.kind is PreparationKind.CANCEL_REQUIRED
    assert preparation.active_turn is not None
    assert preparation.active_turn.callback_id == "cb-N", "the cut is nameable"
    assert preparation.active_turn.session_id == seat.session_state().session_id


# ============================================================ ARM 5


def test_the_composition_root_reaches_the_client() -> None:
    source = inspect.getsource(bootstrap)
    assert "AcpClient" in source
    assert "acp_sessions" in source


def test_a_seat_spawn_exists_and_refuses_an_uncertified_provider() -> None:
    """D2 has a code path now. Its refusal is the interesting half: a provider
    with no certified adapter is not a failure, it is one that must take the
    native path."""
    from cli_agent_orchestrator.adapters.acp.spawn import (
        SeatSpawnFailed,
        acp_adapter_for_provider,
        spawn_acp_seat,
    )

    assert acp_adapter_for_provider("claude_code") == "claude-acp"
    assert acp_adapter_for_provider("kiro_cli") is None
    with pytest.raises(SeatSpawnFailed, match="no certified ACP adapter"):
        spawn_acp_seat(terminal_id="t", provider="kiro_cli", cwd="/tmp")
