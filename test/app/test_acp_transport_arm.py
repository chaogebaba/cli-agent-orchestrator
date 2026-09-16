"""Phase B1 — the ACP seat transport, its switch, and the nudge.

AC-S1.1 (native unchanged), AC-S1.2 / AC-S1.3 (against the mock agent),
AC-S1.7 (no keystroke path reachable for an ACP seat) and AC-S1.14 (the nudge
AND the floor).

The arms that matter are the negative ones. A transport that delivered correctly
and also, somewhere, kept a keystroke path alive would pass every positive
assertion here; so would one whose nudge worked and whose floor had quietly
stopped. Both are checked by their absence.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS, FakeClock

import pytest

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.acp.client import AcpClient, AcpFrameLog
from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
from cli_agent_orchestrator.core.delivery import (
    AttemptOutcome,
    DeliveryAttempt,
    EnqueueDraft,
    MsgKind,
    QueueMode,
)
from cli_agent_orchestrator.core.timing import DELIVERY_BACKOFF_S, DELIVERY_LEASE_S
from cli_agent_orchestrator.services.queue_carrier import AcpTransport, NativeSeatCarrier

_MOCK = str(Path(__file__).resolve().parents[1] / "helpers" / "acp_mock_agent.py")
T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
RECEIVER = "term-acp"


# ============================================================== AC-S1.1


def test_with_the_switch_native_the_carrier_is_the_same_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native is not "the dispatch chooses native every time" — no dispatch exists.

    Asserted by IDENTITY. A wrapper that always delegated would satisfy every
    behavioural assertion about the native path while changing the object graph
    the rollback depends on, and D18's back-out is revert-only precisely because
    the switch-off path has to be the old path rather than a new path that
    resembles it.
    """
    monkeypatch.delenv("CAO_SEAT_TRANSPORT", raising=False)
    native = NativeSeatCarrier()
    assert bootstrap._build_seat_carrier(native, AcpTransport) is native


@pytest.mark.parametrize("value", ["native", "", "1", "ACP", "acp ", "true"])
def test_only_the_exact_token_arms_the_plane(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Strictly ``acp``. A switch that also accepted ``1`` or ``ACP`` would be one
    nobody could state the position of from a process listing."""
    monkeypatch.setenv("CAO_SEAT_TRANSPORT", value)
    native = NativeSeatCarrier()
    assert bootstrap._build_seat_carrier(native, AcpTransport) is native


def test_the_armed_switch_builds_a_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_SEAT_TRANSPORT", "acp")
    carrier = bootstrap._build_seat_carrier(NativeSeatCarrier(), AcpTransport)
    assert isinstance(carrier, bootstrap._TransportSeatCarrierDispatch)


def test_the_acp_transport_is_never_constructed_while_the_switch_is_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not merely unused — not built. With the switch native no ACP object exists
    in the process, which is AC-S1.1 stated as an object graph."""
    monkeypatch.delenv("CAO_SEAT_TRANSPORT", raising=False)
    built: list[str] = []

    def _factory() -> AcpTransport:
        built.append("acp")
        return AcpTransport()

    carrier = bootstrap._build_seat_carrier(NativeSeatCarrier(), _factory)
    carrier.emit(terminal_id="term-pane", line="x", sender_key="s", sender_name="s", msg_id="m")
    assert built == []


def test_a_pane_terminal_takes_the_native_path_under_the_armed_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch's default arm. An armed switch changes the PANE seat not at all."""
    monkeypatch.setenv("CAO_SEAT_TRANSPORT", "acp")
    seen: list[str] = []

    class _Native:
        def emit(self, **kwargs: object) -> object:
            seen.append("native")
            return _EMISSION

        pass

    def _factory() -> object:
        raise AssertionError("the ACP transport must not be built for a pane seat")

    carrier = bootstrap._TransportSeatCarrierDispatch(
        _Native(), _factory, lambda _tid: False  # type: ignore[arg-type]
    )
    carrier.emit(terminal_id="t", line="x", sender_key="s", sender_name="s", msg_id="m")
    assert seen == ["native"]


def test_the_dispatch_selects_on_transport_not_on_a_null_coordinate() -> None:
    """AC-S1.10's rule, at the one new consumer B1 adds.

    The predicate is given a row with NULL coordinates AND ``transport='pane'``:
    a NULL-reading dispatch would send it to the ACP arm and deliver nothing.
    """
    from cli_agent_orchestrator.core.transport import is_acp_terminal

    assert is_acp_terminal({"transport": "pane", "tmux_window": None}) is False
    assert is_acp_terminal({"transport": "acp", "tmux_window": None}) is True


def test_the_transport_probe_fails_toward_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row that cannot be read is NOT an ACP seat.

    The directions are not symmetric: an ACP wake to a pane terminal delivers
    nothing, while a native wake to an ACP terminal takes a path that reports its
    own typed refusals.
    """
    import cli_agent_orchestrator.bootstrap as boot

    def _explode(_tid: str) -> dict[str, object]:
        raise RuntimeError("database gone")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata", _explode
    )
    assert boot._acp_seat_selected("anything") is False


# ============================================================== AC-S1.7


def test_no_keystroke_path_is_reachable_from_the_acp_transport() -> None:
    """AC-S1.7's fails-if is "any ``send_keys`` path is REACHABLE for an ACP seat"
    (NG9), so the check is over reachability, not over intent.

    ``AcpTransport`` satisfies ``SeatCarrier`` and NOT ``PaneInjector``, which is
    why D7 made them separate ports: the seat branch holds no reference to the
    pane seam at all, so no conditional inside an injector can be got wrong later.
    """
    import ast
    import inspect

    from cli_agent_orchestrator.core import ports

    assert isinstance(AcpTransport(), ports.SeatCarrier)
    assert not hasattr(AcpTransport, "inject"), "an ACP seat has no pane seam to reach"

    reached = _code_mentions(AcpTransport, _KEYSTROKE_WORDS)
    assert not reached, f"a keystroke path is reachable from the ACP transport: {reached}"


def test_the_acp_transport_never_reads_a_tmux_coordinate() -> None:
    """The native carrier refuses ``no_tmux_coordinates``; for an ACP row those
    columns are NULL BY DESIGN, which is why the two are separate carriers rather
    than one that learned to branch."""
    assert not _code_mentions(AcpTransport, {"tmux_session", "tmux_window"})


# ============================================== AC-S1.2 / AC-S1.3 (mock agent)


@pytest.fixture
def agent(tmp_path: Path) -> Iterator[AcpClient]:
    env = dict(os.environ)
    env["MOCK_ACP_TURN_SECONDS"] = "30"
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


def test_a_wake_reaches_the_agent_and_both_directions_are_in_the_frame_log(
    agent: AcpClient, tmp_path: Path
) -> None:
    """AC-S1.2: the callback is presented over the plane; the frame log has both
    directions."""
    transport = AcpTransport(lambda _tid: agent)
    emission = transport.emit(
        terminal_id=RECEIVER,
        line="[cb-1] the supervisor asks for a status line",
        sender_key="worker",
        sender_name="worker",
        msg_id="m-1",
    )
    assert emission.reason is None
    assert emission.verified is True

    frames = AcpFrameLog(tmp_path / "frames.jsonl").frames()
    assert {f["dir"] for f in frames} >= {">>", "<<"}
    prompts = [
        f for f in frames if f["dir"] == ">>" and f["frame"].get("method") == "session/prompt"
    ]
    assert len(prompts) == 1
    assert "status line" in str(prompts[0]["frame"]["params"]["prompt"])


def test_a_busy_receiver_is_never_submitted_and_no_second_prompt_leaves_cao(
    agent: AcpClient, tmp_path: Path
) -> None:
    """AC-S1.3, the whole of it.

    The mock is mid-turn BY CAO'S OWN TRACKING — a prompt was sent and no
    ``stopReason`` has been seen — and the row is NEVER SUBMITTED. Asserted from
    the FRAME LOG, because S0 proved the fleet reports no busy class: an
    assertion that waited for an error would pass against an agent that silently
    accepted the second prompt and interleaved it.
    """
    transport = AcpTransport(lambda _tid: agent)
    first = transport.emit(
        terminal_id=RECEIVER, line="first", sender_key="w", sender_name="w", msg_id="m-1"
    )
    assert first.verified is True
    assert agent.session_state().turn_open is True

    second = transport.emit(
        terminal_id=RECEIVER, line="second", sender_key="w", sender_name="w", msg_id="m-2"
    )
    assert second.reason == AcpTransport.BUSY_REASON
    assert second.verified is False

    prompts = [
        f
        for f in AcpFrameLog(tmp_path / "frames.jsonl").frames()
        if f["dir"] == ">>" and f["frame"].get("method") == "session/prompt"
    ]
    assert len(prompts) == 1, "exactly one prompt may reach the wire"


def test_the_busy_reason_is_the_outcome_that_spends_no_attempt() -> None:
    """The accounting half of AC-S1.3: "the attempt budget is burned" is its
    fails-if, and the carrier's reason is what the classification reads."""
    from cli_agent_orchestrator.core.delivery import (
        ATTEMPT_BUDGET_OUTCOMES,
        NON_DELIVERY_OUTCOMES,
    )

    assert AcpTransport.BUSY_REASON == AttemptOutcome.ACP_BUSY_RETRY.value
    assert AttemptOutcome.ACP_BUSY_RETRY in NON_DELIVERY_OUTCOMES
    assert AttemptOutcome.ACP_BUSY_RETRY not in ATTEMPT_BUDGET_OUTCOMES


def test_the_row_is_delivered_on_the_first_stop_reason(agent: AcpClient) -> None:
    """The other half of AC-S1.3: parked, then delivered when the turn ends."""
    transport = AcpTransport(lambda _tid: agent)
    transport.emit(terminal_id=RECEIVER, line="first", sender_key="w", sender_name="w", msg_id="1")
    assert (
        transport.emit(
            terminal_id=RECEIVER, line="second", sender_key="w", sender_name="w", msg_id="2"
        ).reason
        == AcpTransport.BUSY_REASON
    )

    agent.cancel()
    assert agent.await_stop_reason(timeout=20) == "cancelled"

    delivered = transport.emit(
        terminal_id=RECEIVER, line="second", sender_key="w", sender_name="w", msg_id="2"
    )
    assert delivered.reason is None
    assert delivered.verified is True


def test_an_unbound_seat_is_a_deadline_bound_reason_not_a_refusal() -> None:
    """No session yet. It returns when the seat's first call does, so it is
    bounded by the row's own deadline rather than by the attempt budget — the
    same reading ``wake_unreachable`` gets."""
    assert (
        AcpTransport(lambda _tid: None)
        .emit(terminal_id=RECEIVER, line="x", sender_key="w", sender_name="w", msg_id="1")
        .reason
        == AcpTransport.UNBOUND_REASON
    )


# ============================================================== AC-S1.14


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[ConnectionPool]:
    result, pool = migrate(tmp_path / "nudge.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    yield pool
    pool.close_all()


def _park_busy(store: SqliteQueueStore, clock: FakeClock) -> str:
    """One row, claimed and then parked with an ``ACP_BUSY_RETRY`` attempt."""
    message = store.enqueue(
        EnqueueDraft(
            idempotency_key="k-busy",
            receiver_id=RECEIVER,
            sender_id="sender",
            kind=MsgKind.CALLBACK,
            payload="body",
            mode=QueueMode.LIVE,
        )
    )
    claimed = store.claim(lease_owner="tick", now=clock.now(), limit=1)
    assert claimed
    store.record_attempt(
        DeliveryAttempt(
            msg_id=message.msg_id,
            claim_id=claimed[0].claim_id,
            carrier="acp",
            started_at=clock.now(),
            outcome=AttemptOutcome.ACP_BUSY_RETRY,
            detail="turn_open",
        )
    )
    return message.msg_id


def _state(pool: ConnectionPool, msg_id: str) -> tuple[str, int]:
    row = (
        pool.connection()
        .execute("SELECT state, attempts FROM delivery_msg WHERE msg_id = ?", (msg_id,))
        .fetchone()
    )
    return row["state"], int(row["attempts"])


def test_the_nudge_re_offers_a_busy_row_immediately(pool: ConnectionPool) -> None:
    """Well under the 65-second floor: the row is claimable at the SAME instant."""
    clock = FakeClock(T0)
    store = SqliteQueueStore(pool, clock=clock)
    msg_id = _park_busy(store, clock)
    assert _state(pool, msg_id)[0] == "leased"

    released = store.release_busy(RECEIVER, now=clock.now())
    assert released == 1
    state, attempts = _state(pool, msg_id)
    assert state == "ready"
    assert attempts == 0, "a nudge spends no attempt"
    assert [m.msg_id for m in store.claim(lease_owner="tick", now=clock.now(), limit=5)] == [msg_id]


def test_the_nudge_leaves_a_genuinely_in_flight_row_alone(pool: ConnectionPool) -> None:
    """The negative that makes the positive safe.

    A nudge that re-offered every leased row would cut short the lease of a
    delivery in flight, which is the double-send the fencing token exists to
    prevent.
    """
    clock = FakeClock(T0)
    store = SqliteQueueStore(pool, clock=clock)
    message = store.enqueue(
        EnqueueDraft(
            idempotency_key="k-inflight",
            receiver_id=RECEIVER,
            sender_id="sender",
            kind=MsgKind.CALLBACK,
            payload="body",
            mode=QueueMode.LIVE,
        )
    )
    assert store.claim(lease_owner="tick", now=clock.now(), limit=1)
    assert store.release_busy(RECEIVER, now=clock.now()) == 0
    assert _state(pool, message.msg_id)[0] == "leased"


def test_the_nudge_does_not_reach_another_receiver(pool: ConnectionPool) -> None:
    clock = FakeClock(T0)
    store = SqliteQueueStore(pool, clock=clock)
    msg_id = _park_busy(store, clock)
    assert store.release_busy("some-other-terminal", now=clock.now()) == 0
    assert _state(pool, msg_id)[0] == "leased"


def test_a_nudge_that_finds_nothing_is_normal(pool: ConnectionPool) -> None:
    """A driver nudging a terminal whose rows another path already served."""
    clock = FakeClock(T0)
    store = SqliteQueueStore(pool, clock=clock)
    assert store.release_busy(RECEIVER, now=clock.now()) == 0


def test_the_floor_still_delivers_with_no_nudge_at_all(pool: ConnectionPool) -> None:
    """AC-S1.14's second half, and the one a nudge implementation tends to break.

    No nudge is ever issued. The lease expires and ``reclaim`` re-offers on the
    ordinary floor — slower, and still correct. The fails-if is explicit that a
    build where the nudge is the ONLY path fails.
    """
    clock = FakeClock(T0)
    store = SqliteQueueStore(pool, clock=clock)
    msg_id = _park_busy(store, clock)

    past_the_floor = T0 + timedelta(seconds=DELIVERY_LEASE_S + 5)
    result = store.reclaim(now=past_the_floor)
    assert result.reoffered >= 1
    state, attempts = _state(pool, msg_id)
    assert state == "ready"
    assert attempts == 0, "the floor spends no attempt on a busy row either"
    # ``reclaim`` re-offers with the flat backoff, which is the difference
    # between the two paths and is exactly what the nudge exists to skip: the
    # floor is the lease PLUS a backoff, the nudge is now.
    after_backoff = past_the_floor + timedelta(seconds=DELIVERY_BACKOFF_S + 1)
    assert [m.msg_id for m in store.claim(lease_owner="tick", now=after_backoff, limit=5)] == [
        msg_id
    ]


def test_the_nudge_is_reachable_on_the_tick_and_returns_a_count() -> None:
    """D7: the tick stays the single retry/queue authority, so the driver's
    event-driven path is a method ON it rather than a second queue beside it."""
    import inspect

    from cli_agent_orchestrator.app.delivery.tick import DeliveryTick

    assert hasattr(DeliveryTick, "nudge")
    signature = inspect.signature(DeliveryTick.nudge)
    assert list(signature.parameters)[:2] == ["self", "terminal_id"]


_KEYSTROKE_WORDS = frozenset(
    {"send_keys", "send_special_key", "paste", "pipe_pane", "tmux", "type_text"}
)


def _code_mentions(target: type, words: frozenset[str] | set[str]) -> list[str]:
    """Words reachable from ``target``'s CODE — identifiers and runtime strings.

    Docstrings are excluded, and the reason is the one AC-S1.9 gives: this class
    has to be able to SAY what it is not allowed to reach, and a check that
    forbade the explanation would push the reasoning out of the file and leave
    only the rule. What is checked is what the code NAMES.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(target)))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and getattr(node, "body", None)
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in words:
            found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in words:
            found.append(node.id)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.extend(word for word in words if word in node.value)
    return sorted(set(found))


class _Emission:
    reason = None
    verified = True
    annotations: tuple[str, ...] = ()
    detail = "native"


_EMISSION = _Emission()


# ============================== S7 / M1 — the transport's OWN busy guard


class _CompliantClient:
    """A client that does NOT refuse a mid-turn prompt.

    The review's M1 deleted ``if state.turn_open:`` from ``AcpTransport.emit``
    and all 25 transport arms stayed green, because ``AcpClient.prompt`` refuses
    independently and ``emit`` maps that refusal back to the same reason. Two
    guards, one of them unexercised — defence in depth with a half nobody tests
    is a half that can be deleted.

    This double is the missing half's test harness: it accepts every prompt, so
    the transport's own guard is the ONLY thing standing between a busy receiver
    and a second prompt on the wire.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._turn_open = False

    def session_state(self) -> object:
        client = self

        class _State:
            session_id = "sess-1"
            turn_open = client._turn_open
            open_request_id = 1
            last_stop_reason = None
            steering_advertised = False

        return _State()

    def prompt(self, text: str, *, callback_id: str | None = None) -> int:
        del callback_id
        self.prompts.append(text)
        self._turn_open = True
        return len(self.prompts)

    def settle(self) -> None:
        self._turn_open = False


def test_the_transports_own_guard_refuses_a_busy_receiver() -> None:
    """S7: exercised against a client that would happily take the second prompt.

    With ``AcpClient``'s guard removed from the picture, the transport's
    ``turn_open`` branch is load-bearing on its own — and AC-S1.3's invariant is
    that NO second prompt reaches the wire, not that one of two guards happens to
    catch it.
    """
    client = _CompliantClient()
    transport = AcpTransport(lambda _tid: client)

    first = transport.emit(
        terminal_id=RECEIVER, line="first", sender_key="w", sender_name="w", msg_id="m-1"
    )
    assert first.verified is True
    assert client.prompts == ["first"]

    second = transport.emit(
        terminal_id=RECEIVER, line="second", sender_key="w", sender_name="w", msg_id="m-2"
    )
    assert second.reason == AcpTransport.BUSY_REASON
    assert client.prompts == ["first"], "the transport's own guard let a second prompt through"


def test_the_transport_delivers_again_once_the_turn_settles() -> None:
    """The control: the guard must be a GUARD, not a permanent refusal."""
    client = _CompliantClient()
    transport = AcpTransport(lambda _tid: client)
    transport.emit(terminal_id=RECEIVER, line="first", sender_key="w", sender_name="w", msg_id="1")
    client.settle()
    again = transport.emit(
        terminal_id=RECEIVER, line="second", sender_key="w", sender_name="w", msg_id="2"
    )
    assert again.reason is None
    assert client.prompts == ["first", "second"]
