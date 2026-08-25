"""F435 send-seam wiring + verification-failure transition (BLOCKERS B2, B3).

These tests guard the terminal_service side of the F435 fix, which the
provider-level tests cannot:

* B3 — WIRING: both send seams (``send_input`` and ``send_prepared_input``)
  must actually invoke ``provider.verify_submission_after_send`` exactly once.
  Disconnecting either call must turn a shipped test RED.
* B2 — FAILURE TRANSITION: when verification raises ``CodexSubmitStuckError``
  the dispatch must be ROLLED BACK (``abort_dispatch``), never committed, and
  the seam must surface a retry-safe ``DeliveryDeferredError`` — not a raw
  crash and not a pretend-success. On the prepared seam, a stuck send must NOT
  publish the submission boundary (``mark_injection_completed`` /
  ``on_submitted``).

Ordering is asserted against a single recording list so the "verify runs
BEFORE commit" invariant is locked: a mutant that verifies after commit would
record ``commit`` before ``verify`` and fail.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexSubmitStuckError
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation

METADATA = {
    "tmux_session": "session",
    "tmux_window": "window",
    "provider": "codex",
}


def _install_recording_dispatch(monkeypatch, events: list[str]):
    """Patch status_monitor dispatch txn methods to record ordering.

    begin_dispatch returns an opaque token; commit/abort record their names so
    tests can assert exactly one of them fires and in what order relative to
    the verify hook.
    """
    token = SimpleNamespace(terminal_id="term1234", dispatch_gen=1, begun=0.0)
    monkeypatch.setattr(terminal_service.status_monitor, "begin_dispatch", lambda _tid: token)
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "commit_dispatch",
        lambda _txn: events.append("commit"),
    )
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "abort_dispatch",
        lambda _txn: events.append("abort"),
    )
    monkeypatch.setattr(
        terminal_service.status_monitor, "bind_dispatch_provider", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        terminal_service.status_monitor, "notify_input_sent", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "clear_rolling_buffer",
        lambda *_a, **_k: None,
    )
    return token


def _codex_like_provider(events: list[str], *, stuck: bool) -> MagicMock:
    """A provider whose verify hook records 'verify' and optionally raises."""
    provider = MagicMock()
    provider.composer_stash_keys = None  # take the preserve_draft path (returns None)
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0.0
    provider.assume_processing_on_dispatch = False

    def _verify(metadata, backend, message=None, baseline=None):
        events.append("verify")
        if stuck:
            raise CodexSubmitStuckError(
                "Codex terminal term1234 did not submit the pasted task after 3 "
                "re-Enter attempts"
            )

    provider.verify_submission_after_send.side_effect = _verify
    return provider


# ---------------------------------------------------------------------------
# send_input seam
# ---------------------------------------------------------------------------


def _wire_send_input(monkeypatch, events, provider):
    backend = MagicMock()
    backend.send_keys.side_effect = lambda *_a, **_k: events.append("send_keys")
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _tid: dict(METADATA))
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda _tid: provider)
    monkeypatch.setattr(
        terminal_service.status_monitor, "get_status", lambda _tid: TerminalStatus.IDLE
    )
    monkeypatch.setattr(terminal_service, "preserve_draft_before_send", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_service, "update_last_active", lambda _tid: None)
    monkeypatch.setattr(terminal_service, "inject_memory_context", lambda msg, _tid: msg)
    monkeypatch.setattr(terminal_service, "_append_message_contract", lambda msg, *_a, **_k: msg)
    return backend


def test_send_input_invokes_verify_hook_once(monkeypatch):
    """B3: send_input wires the verification hook (disconnect ⇒ RED)."""
    events: list[str] = []
    _install_recording_dispatch(monkeypatch, events)
    provider = _codex_like_provider(events, stuck=False)
    _wire_send_input(monkeypatch, events, provider)

    assert terminal_service.send_input("term1234", "task") is True

    provider.verify_submission_after_send.assert_called_once()
    # r4: the pasted task text is threaded through so the hook can anchor on
    # the scrollback-content submission boundary. A regression that drops the
    # message argument turns this RED.
    assert provider.verify_submission_after_send.call_args.kwargs.get("message") == "task"
    # verify runs AFTER the paste and BEFORE the commit.
    assert events == ["send_keys", "verify", "commit"]


def test_send_input_stuck_aborts_and_defers(monkeypatch):
    """B2: a stuck verdict rolls the dispatch back and defers (no commit)."""
    events: list[str] = []
    _install_recording_dispatch(monkeypatch, events)
    provider = _codex_like_provider(events, stuck=True)
    _wire_send_input(monkeypatch, events, provider)

    with pytest.raises(DeliveryDeferredError):
        terminal_service.send_input("term1234", "task")

    # Rolled back, not committed; verify ran before the abort.
    assert events == ["send_keys", "verify", "abort"]
    assert "commit" not in events


# ---------------------------------------------------------------------------
# send_prepared_input seam
# ---------------------------------------------------------------------------


def _wire_send_prepared(monkeypatch, events, provider):
    backend = MagicMock(supports_identity_readback=False)
    backend.read_native_identity.return_value = SimpleNamespace(verdict="match")
    backend.send_keys.side_effect = lambda *_a, **_k: events.append("send_keys")
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _tid: dict(METADATA))
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda _tid: provider)
    monkeypatch.setattr(
        terminal_service.status_monitor, "get_status", lambda _tid: TerminalStatus.IDLE
    )
    monkeypatch.setattr(terminal_service, "preserve_draft_before_send", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_service, "update_last_active", lambda _tid: None)
    observation = BoundaryObservation("f435", TerminalStatus.IDLE, 1, 1, 1, 1, 1)
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "mark_injection_completed",
        lambda _tid: events.append("mark_injection_completed") or observation,
    )
    terminal_service._memory_injected_terminals.discard("term1234")
    return backend


def test_send_prepared_input_invokes_verify_hook_once(monkeypatch):
    """B3: send_prepared_input wires the verification hook (disconnect ⇒ RED)."""
    events: list[str] = []
    _install_recording_dispatch(monkeypatch, events)
    provider = _codex_like_provider(events, stuck=False)
    _wire_send_prepared(monkeypatch, events, provider)

    submitted_calls: list[object] = []
    terminal_service.send_prepared_input("term1234", "payload", on_submitted=submitted_calls.append)

    provider.verify_submission_after_send.assert_called_once()
    # r4: the prepared payload text is threaded through as the message anchor.
    assert provider.verify_submission_after_send.call_args.kwargs.get("message") == "payload"
    # verify BEFORE commit; boundary published only after commit.
    assert events == ["send_keys", "verify", "commit", "mark_injection_completed"]
    assert len(submitted_calls) == 1
    terminal_service._memory_injected_terminals.discard("term1234")


def test_send_prepared_input_stuck_aborts_defers_and_publishes_no_boundary(monkeypatch):
    """B2: stuck verdict aborts, defers, and never publishes the submission boundary."""
    events: list[str] = []
    _install_recording_dispatch(monkeypatch, events)
    provider = _codex_like_provider(events, stuck=True)
    _wire_send_prepared(monkeypatch, events, provider)

    submitted_calls: list[object] = []
    with pytest.raises(DeliveryDeferredError):
        terminal_service.send_prepared_input(
            "term1234", "payload", on_submitted=submitted_calls.append
        )

    assert events == ["send_keys", "verify", "abort"]
    assert "commit" not in events
    # No pretend-success: the submission boundary and on_submitted never fire.
    assert "mark_injection_completed" not in events
    assert submitted_calls == []
    terminal_service._memory_injected_terminals.discard("term1234")
