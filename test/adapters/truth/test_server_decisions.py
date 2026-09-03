"""AC4c — server decision rows (WP-ARCH F725 #581, lane B).

The exception classes below are LOCAL definitions that mirror the legacy ones by
name.  That is not a shortcut: the classifier itself matches by class name,
because ``adapters`` may not import the legacy tree, and a test that imported the
real ``DeliveryDeferredError`` would be testing a coupling the design forbids.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.truth import codex_rollout, legacy_egress, server_decisions
from cli_agent_orchestrator.core.events import DecisionKind

from .conftest import FakeEventStore


class DeliveryDeferredError(Exception):
    """Mirrors ``services/draft_guard.py``'s retry-safe deferral."""


class DialogOpenError(DeliveryDeferredError):
    """Mirrors the dialog subclass — deferred, but NOT composer-unreadable."""


class CodexSubmitStuckError(Exception):
    """Mirrors the codex submit-verification failure."""


class TerminalInputBlockedError(Exception):
    """Mirrors the WAITING_USER_ANSWER refusal."""


# -- classification ---------------------------------------------------------


def test_no_exception_is_confirmed() -> None:
    assert server_decisions.classify_outcome(None) == "confirmed"


def test_a_stuck_submit_survives_the_conversion_to_deferred() -> None:
    """``send_input`` re-raises the stuck error as a deferral; the cause still says stuck.

    Collapsing the two would erase the #555 distinction between "the submit key
    was lost" and "we chose not to deliver yet".
    """
    stuck = CodexSubmitStuckError("submit never landed")
    deferred = DeliveryDeferredError(str(stuck))
    deferred.__cause__ = stuck
    assert server_decisions.classify_outcome(deferred) == "stuck"
    assert server_decisions.classify_outcome(stuck) == "stuck"


def test_a_draft_guard_message_is_composer_unreadable() -> None:
    exc = DeliveryDeferredError("Composer state is unreadable for terminal t1")
    assert server_decisions.classify_outcome(exc) == "composer_unreadable"


def test_a_dialog_is_deferred_not_composer_unreadable() -> None:
    """The composer was read perfectly well; it is simply not free."""
    assert server_decisions.classify_outcome(DialogOpenError("Claude dialog is active")) == (
        "deferred"
    )


def test_a_plain_deferral_is_deferred() -> None:
    assert server_decisions.classify_outcome(DeliveryDeferredError("identity_unverified")) == (
        "deferred"
    )
    assert server_decisions.classify_outcome(TerminalInputBlockedError("waiting")) == "deferred"


def test_anything_else_is_error() -> None:
    assert server_decisions.classify_outcome(ValueError("Terminal 't1' not found")) == "error"


def test_every_outcome_is_in_the_closed_vocabulary() -> None:
    cases: list[BaseException] = [
        DeliveryDeferredError("Composer state is unreadable"),
        DialogOpenError("dialog"),
        CodexSubmitStuckError("stuck"),
        ValueError("boom"),
    ]
    outcomes = {server_decisions.classify_outcome(exc) for exc in cases} | {"confirmed"}
    assert outcomes <= server_decisions.DELIVERY_OUTCOMES


def test_a_cyclic_cause_chain_terminates() -> None:
    first = DeliveryDeferredError("a")
    second = DeliveryDeferredError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert server_decisions.classify_outcome(first) == "deferred"


# -- delivery.attempt rows --------------------------------------------------


def test_off_writes_nothing(store: FakeEventStore) -> None:
    server_decisions.record_delivery_attempt("t1", carrier="send_input")
    server_decisions.record_teardown_decided("t1", "deferred_init_internal")
    server_decisions.record_teardown_intended(
        "t1", scope_kind="terminal", scope_key="t1", ttl_s=300.0
    )
    assert store.rows == []


def test_a_delivery_attempt_cites_the_observation_it_acted_on(
    ingest_on: FakeEventStore,
) -> None:
    class _Monitor:
        _status_fusion_reason: dict[str, str] = {}

        def get_condition(self, terminal_id: str) -> None:
            return None

    legacy_egress.record_legacy_publish(
        _Monitor(), "t1", "idle", "incremental", "incremental", "ok", None
    )
    published = ingest_on.rows[0]
    server_decisions.record_delivery_attempt("t1", carrier="send_input")
    row = ingest_on.of_kind(DecisionKind.DELIVERY_ATTEMPT, "t1")[0]
    assert row.evidence == published.event_id
    assert row.msg_id
    assert row.payload["carrier"] == "send_input"
    assert row.payload["outcome"] == "confirmed"


def test_a_confirmed_attempt_names_the_rollout_record_that_confirmed_it(
    ingest_on: FakeEventStore, tmp_path: Path
) -> None:
    """The join is by ``source_ref``, never by a timestamp window (r9 retired N3)."""
    rollout = tmp_path / "rollout-x.jsonl"
    codex_rollout.attach("t1", rollout)
    with rollout.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user"}})
            + "\n"
        )
    server_decisions.record_delivery_attempt("t1", carrier="send_input")
    attempt = ingest_on.of_kind(DecisionKind.DELIVERY_ATTEMPT, "t1")[0]
    confirmed = ingest_on.rows[0]
    assert confirmed.kind.value == "submission.confirmed"
    assert attempt.source_ref == confirmed.source_ref
    assert attempt.source_ref == f"rollout:{rollout}#0"


def test_a_failed_attempt_carries_no_source_ref_and_a_typed_detail(
    ingest_on: FakeEventStore,
) -> None:
    exc = DeliveryDeferredError("Composer state is unreadable for terminal t1")
    server_decisions.record_delivery_attempt("t1", carrier="send_input", exc=exc)
    row = ingest_on.of_kind(DecisionKind.DELIVERY_ATTEMPT, "t1")[0]
    assert row.source_ref is None
    assert row.payload["outcome"] == "composer_unreadable"
    assert row.payload["detail"].startswith("DeliveryDeferredError: Composer state is unreadable")


# -- the decorator ----------------------------------------------------------


def test_the_decorator_is_a_passthrough_when_ingestion_is_off(
    store: FakeEventStore,
) -> None:
    calls: list[str] = []

    @server_decisions.dispatch_attempt("send_input")
    def send(terminal_id: str) -> bool:
        calls.append(terminal_id)
        return True

    assert send("t1") is True
    assert calls == ["t1"]
    assert store.rows == []


def test_the_decorator_writes_one_row_per_exit(ingest_on: FakeEventStore) -> None:
    @server_decisions.dispatch_attempt("send_input")
    def send(terminal_id: str) -> bool:
        return True

    send("t1")
    send("t1")
    assert len(ingest_on.of_kind(DecisionKind.DELIVERY_ATTEMPT, "t1")) == 2


def test_the_decorator_records_a_raise_and_re_raises_it_unchanged(
    ingest_on: FakeEventStore,
) -> None:
    """The #555 shape: the guard raises ABOVE the try block, before any paste."""
    original = DeliveryDeferredError("Composer state is unreadable for terminal t1")

    @server_decisions.dispatch_attempt("send_input")
    def send(terminal_id: str) -> bool:
        raise original

    with pytest.raises(DeliveryDeferredError) as caught:
        send("t1")
    assert caught.value is original
    row = ingest_on.of_kind(DecisionKind.DELIVERY_ATTEMPT, "t1")[0]
    assert row.payload["outcome"] == "composer_unreadable"


def test_the_decorator_covers_an_early_return_too(ingest_on: FakeEventStore) -> None:
    """A hand-placed statement at the else exit would miss the fixture path."""

    @server_decisions.dispatch_attempt("send_input")
    def send(terminal_id: str) -> bool:
        return True  # the ``_fixture_send_input_override`` shape

    send("t1")
    assert len(ingest_on.of_kind(DecisionKind.DELIVERY_ATTEMPT, "t1")) == 1


def test_a_base_exception_is_re_raised_without_a_row(ingest_on: FakeEventStore) -> None:
    """A ``KeyboardInterrupt`` belongs to the caller's control flow, not to us."""

    @server_decisions.dispatch_attempt("send_input")
    def send(terminal_id: str) -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        send("t1")
    assert ingest_on.rows == []


def test_the_decorator_preserves_the_wrapped_identity(ingest_on: FakeEventStore) -> None:
    @server_decisions.dispatch_attempt("send_input")
    def send_input(terminal_id: str, message: str = "") -> bool:
        """A docstring the fork's own tooling reads."""
        return True

    assert send_input.__name__ == "send_input"
    assert send_input.__doc__ == "A docstring the fork's own tooling reads."
    assert send_input(terminal_id="t1") is True


# -- teardown rows ----------------------------------------------------------


def test_teardown_decided_carries_the_settlement_code(ingest_on: FakeEventStore) -> None:
    server_decisions.record_teardown_decided("t1", "deferred_init_internal", "never started")
    row = ingest_on.of_kind(DecisionKind.TEARDOWN_DECIDED, "t1")[0]
    assert row.payload["code"] == "deferred_init_internal"
    assert row.payload["detail"] == "never started"
    assert row.decision is DecisionKind.TEARDOWN_DECIDED


def test_teardown_intended_carries_the_scope_and_ttl(ingest_on: FakeEventStore) -> None:
    server_decisions.record_teardown_intended(
        "t1", scope_kind="terminal", scope_key="t1", ttl_s=300.0, requested_by="supervisor"
    )
    row = ingest_on.of_kind(DecisionKind.TEARDOWN_INTENDED, "t1")[0]
    assert row.payload == {
        "scope_kind": "terminal",
        "scope_key": "t1",
        "ttl_s": 300.0,
        "requested_by": "supervisor",
    }


def test_a_draft_guard_frame_is_composer_unreadable_without_the_message(
    tmp_path: Path,
) -> None:
    """The second signal: the raise came out of ``draft_guard.py``.

    The message check alone would miss a future draft-guard failure worded
    differently, and the frame check alone would miss a test stub that patches
    ``preserve_draft_before_send``.  Both are cheap; either is sufficient.
    """
    module = tmp_path / "draft_guard.py"
    module.write_text("def guard(exc):\n    raise exc\n", encoding="utf-8")
    namespace: dict[str, object] = {}
    exec(compile(module.read_text(), str(module), "exec"), namespace)

    exc = DeliveryDeferredError("could not confirm composer clear")
    assert server_decisions.classify_outcome(exc) == "deferred"
    try:
        namespace["guard"](exc)  # type: ignore[operator]
    except DeliveryDeferredError as caught:
        assert server_decisions.classify_outcome(caught) == "composer_unreadable"
    else:  # pragma: no cover
        pytest.fail("the stub did not raise")
