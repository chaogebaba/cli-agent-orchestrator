import ast
import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends.base import NativeIdentityResult
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import InboxModel, TerminalModel
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
from cli_agent_orchestrator.providers.cursor_cli import CursorCliProvider
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.providers.hermes import HermesProvider
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.providers.opencode_cli import OpenCodeCliProvider
from cli_agent_orchestrator.services import mailbox_service, terminal_service
from cli_agent_orchestrator.services.herdr_inbox_service import (
    HerdrInboxService,
    IdentityMarker,
    ReconcileOutcome,
    _IdentityRecord,
)
from cli_agent_orchestrator.services.inbox_service import (
    InboxService,
    InjectSafetyResult,
    get_delivery_lock,
)
from cli_agent_orchestrator.services.provider_rebind_service import DeliveryGuard
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation, StatusMonitor


def _probe_meta(status: str = "idle", **extra):
    value = {
        "probed_at": "2026-07-17T00:00:00Z",
        "geometry": {"columns": 80, "rows": 24},
        "frame_rows_hash": "0" * 64,
        "frame_source": "fresh_capture",
        "result_status": status,
        "law_signal": {"class": "chrome", "provider_signal": None, "row_index": None},
    }
    value.update(extra)
    return value


@pytest.fixture
def wpq8_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wpq8.sqlite'}")
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=True)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def test_wpq8_closed_result_invariants():
    assert InjectSafetyResult("safe").reason is None
    with pytest.raises(ValueError):
        InjectSafetyResult("safe", "waiting_status")
    with pytest.raises(ValueError):
        InjectSafetyResult("veto")
    with pytest.raises(ValueError):
        InjectSafetyResult("veto", "dialog_hazard", "unknown_dialog:-")


def test_wpq8_m1_waiting_gate_is_consulted_and_episode_is_closed():
    service = InboxService()
    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        return_value="unknown_dialog",
    ) as gate:
        result = service._inject_safe("worker", object(), _probe_meta())
    gate.assert_called_once_with("worker")
    assert result == InjectSafetyResult("veto", "waiting_gate", "unknown_dialog:-")


def test_wpq8_m3_waiting_status_is_unconditional():
    result = InboxService()._inject_safe(
        "worker",
        SimpleNamespace(accepts_input_while_processing=True),
        _probe_meta("waiting_user_answer"),
    )
    assert result == InjectSafetyResult("veto", "waiting_status")


@pytest.mark.parametrize(
    "failure",
    ["empty_capture", "malformed_meta", "provider_hook_exception"],
)
def test_wpq8_m15_probe_failures_are_safety_unverified(failure):
    result = InboxService()._inject_safe("worker", object(), _probe_meta(probe_failure=failure))
    assert result == InjectSafetyResult("veto", "safety_unverified")


def test_wpq8_m15_waiting_gate_exception_is_safety_unverified():
    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        side_effect=RuntimeError("unavailable"),
    ):
        result = InboxService()._inject_safe("worker", object(), _probe_meta())
    assert result == InjectSafetyResult("veto", "safety_unverified")


def test_wpq8_m6_m8_native_no_proof_vetoes_before_attempt():
    result = InboxService()._inject_safe(
        "worker",
        object(),
        _probe_meta(identity_proof_failure="native_identity_unavailable"),
    )
    assert result == InjectSafetyResult("veto", "identity_unverified")


def _runtime_monitor(final_status: TerminalStatus):
    observation = BoundaryObservation("epoch", TerminalStatus.PROCESSING, 1, 1, 1, None, 1)
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = observation
    monitor.get_status.return_value = TerminalStatus.PROCESSING
    monitor.get_input_gen.return_value = 1
    monitor.get_status_gen.return_value = 1
    monitor.probe_screen_status.return_value = (
        final_status,
        _probe_meta(final_status.value),
    )
    return monitor, observation


def _run_eager_delivery(wpq8_db, final_status):
    database.create_terminal("worker", "session", "window", "grok_cli")
    message = database.create_inbox_message("sender", "worker", "payload")
    monitor, observation = _runtime_monitor(final_status)
    provider = MagicMock()
    provider.accepts_input_while_processing = True
    send = MagicMock()

    def submitted(_terminal_id, _wire, **kwargs):
        kwargs["on_submitted"](observation)
        return observation

    send.side_effect = submitted
    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            side_effect=lambda _terminal, value, _kind: value,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            send,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "send_returned_unverified"}),
        ),
        patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True),
    ):
        InboxService().deliver_pending("worker")
    return message, send


def test_wpq8_m14_safe_eager_processing_reaches_paste(wpq8_db):
    message, send = _run_eager_delivery(wpq8_db, TerminalStatus.PROCESSING)
    send.assert_called_once()
    trace = database.get_message_trace(message.id)
    assert trace["message"]["status"] == "delivered"
    assert len(trace["attempts"]) == 1


def test_wpq8_m2_eager_waiting_is_vetoed_before_attempt(wpq8_db):
    message, send = _run_eager_delivery(wpq8_db, TerminalStatus.WAITING_USER_ANSWER)
    send.assert_not_called()
    trace = database.get_message_trace(message.id)
    assert trace["message"]["status"] == "pending"
    assert trace["attempts"] == []


def test_wpq8_m9_all_admission_kinds_share_one_preopen_safety_call():
    source = (
        Path(__file__).parents[2]
        / "src"
        / "cli_agent_orchestrator"
        / "services"
        / "inbox_service.py"
    ).read_text(encoding="utf-8")
    assert source.count("safety = self._inject_safe(terminal_id, provider, probe_meta)") == 1
    safety_seat = source.index("safety = self._inject_safe")
    opener_seat = source.index("opened = begin_delivery_attempt_if_no_other_delivering")
    assert safety_seat < opener_seat


def test_wpq8_m7_m10_hazard_comes_from_one_fresh_probe_frame(monkeypatch):
    monitor = StatusMonitor()
    monitor._screens["worker"] = (
        SimpleNamespace(display=["› "], columns=80, lines=24),
        object(),
    )
    backend = MagicMock()
    backend.capture_viewport.return_value = "Approve command? yes/no\n› "
    backend.get_pane_size.return_value = (80, 24)
    provider = CodexProvider("worker", "session", "window")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _terminal: provider,
    )
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _terminal: {
            "tmux_session": "session",
            "tmux_window": "window",
            "provider": "codex",
        },
    )

    status, meta = monitor.probe_screen_status("worker")

    assert status == TerminalStatus.WAITING_USER_ANSWER
    assert meta["frame_source"] == "fresh_capture"
    assert meta["injection_hazard"] == "interactive_dialog"
    backend.capture_viewport.assert_called_once_with("session", "window")


@pytest.mark.parametrize(
    ("provider_class", "rows"),
    [
        (AntigravityCliProvider, ["Do you want to allow this action?", "Allow once"]),
        (ClaudeCodeProvider, ["❯ 1. Yes", "  2. No", "↑/↓ to navigate"]),
        (CodexProvider, ["Approve command? yes/no"]),
        (CopilotCliProvider, ["Do you trust the contents of this directory?"]),
        (CursorCliProvider, ["↑/↓ to navigate"]),
        (GrokCliProvider, ["Run Grok Build in a project directory?"]),
        (HermesProvider, ["Approve action? y/n"]),
        (KiroCliProvider, ["Yes No Always allow", "Ask a question or describe a task"]),
        (OpenCodeCliProvider, ["△ Permission required"]),
    ],
)
def test_wpq8_all_midturn_dialog_providers_expose_hazard(provider_class, rows):
    provider = (
        provider_class("worker", "session", "window", "developer")
        if provider_class is KiroCliProvider
        else provider_class("worker", "session", "window")
    )
    assert provider.classify_injection_hazard(rows) == "interactive_dialog"


def test_wpq8_kimi_startup_only_dialog_is_not_midturn_hazard():
    provider = KimiCliProvider("worker", "session", "window")
    assert provider.classify_injection_hazard(["A new version is available"]) is None
    source = Path(provider.__class__.__module__.replace(".", "/") + ".py")
    source = Path(__file__).parents[2] / "src" / source
    tree = ast.parse(source.read_text(encoding="utf-8"))
    callers = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_handle_startup_dialog"
            for call in ast.walk(node)
        ):
            callers.append(node.name)
    assert callers == ["initialize"]


@pytest.mark.parametrize("orchestration_type", [OrchestrationType.SEND_MESSAGE, None])
def test_wpq8_m11_prepared_send_waiting_guard_is_unconditional(orchestration_type, monkeypatch):
    backend = MagicMock()
    monkeypatch.setattr(
        terminal_service,
        "get_terminal_metadata",
        lambda _terminal: {
            "tmux_session": "session",
            "tmux_window": "window",
            "provider": "codex",
        },
    )
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "get_status",
        lambda _terminal: TerminalStatus.WAITING_USER_ANSWER,
    )
    with pytest.raises(terminal_service.TerminalInputBlockedError):
        terminal_service.send_prepared_input(
            "worker", "payload", orchestration_type=orchestration_type
        )
    backend.send_keys.assert_not_called()


def _seed_identity(service: HerdrInboxService, agent: str = "codex") -> tuple[str, str, int]:
    key = ("worker", "pane-1", 1)
    with service._identity_guard:
        service._terminal_to_pane["worker"] = "pane-1"
        service._pane_to_terminal["pane-1"] = "worker"
        service._native_event_gen[("worker", "pane-1")] = 1
        service._identity_records[key] = _IdentityRecord(
            IdentityMarker(agent, "pane-1", 1),
            received_monotonic=1.0,
        )
    return key


def test_wpq8_m13_m17_foreground_process_never_proves_identity(monkeypatch):
    backend = object.__new__(HerdrBackend)
    backend._resolve_pane_id_from_window = MagicMock(return_value="pane-1")
    backend._run_herdr = MagicMock(
        return_value=subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"result": {"pane": {"foreground_process": "node"}}})
        )
    )
    service = MagicMock()
    service.read_identity_marker.return_value = None
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.herdr_inbox_registry.get_herdr_inbox_service",
        lambda: service,
    )
    assert backend.read_native_identity("worker", "session", "window", "codex") == (
        NativeIdentityResult(None, "node", "unavailable")
    )
    service.read_identity_marker.return_value = IdentityMarker("codex", "pane-1", 1)
    assert backend.read_native_identity(
        "worker", "session", "window", "claude_code"
    ) == NativeIdentityResult("codex", "node", "mismatch")


def test_wpq8_identity_age_is_not_a_validity_boundary(monkeypatch):
    service = HerdrInboxService(socket_path="/tmp/wpq8.sock")
    _seed_identity(service)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.herdr_inbox_service.time.monotonic",
        lambda: 10000.0,
    )
    assert service.read_identity_marker("worker") == IdentityMarker("codex", "pane-1", 1)


def test_wpq8_m21_m22_m25_reconnect_preserves_then_grace_promotes(monkeypatch):
    service = HerdrInboxService(socket_path="/tmp/wpq8.sock")
    key = _seed_identity(service)
    clock = [100.0]
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.herdr_inbox_service.time.monotonic",
        lambda: clock[0],
    )
    service._quarantine_identity_markers()
    assert service.read_identity_marker("worker") is None
    service._apply_reconcile_outcome(ReconcileOutcome("ok", frozenset({key})))
    clock[0] = 129.9
    assert service.read_identity_marker("worker") is None
    clock[0] = 130.1
    assert service.read_identity_marker("worker") == IdentityMarker("codex", "pane-1", 1)


def test_wpq8_m23_failed_reconcile_never_confirms_marker(monkeypatch):
    service = HerdrInboxService(socket_path="/tmp/wpq8.sock")
    key = _seed_identity(service)
    clock = [100.0]
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.herdr_inbox_service.time.monotonic",
        lambda: clock[0],
    )
    service._apply_reconcile_outcome(ReconcileOutcome("failed"))
    clock[0] = 1000.0
    assert service.read_identity_marker("worker") is None
    service._apply_reconcile_outcome(ReconcileOutcome("ok", frozenset({key})))
    clock[0] = 1030.1
    assert service.read_identity_marker("worker") == IdentityMarker("codex", "pane-1", 1)


def test_wpq8_m26_grace_is_measured_from_last_reconnect(monkeypatch):
    service = HerdrInboxService(socket_path="/tmp/wpq8.sock")
    key = _seed_identity(service)
    clock = [100.0]
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.herdr_inbox_service.time.monotonic",
        lambda: clock[0],
    )
    service._apply_reconcile_outcome(ReconcileOutcome("ok", frozenset({key})))
    clock[0] = 120.0
    service._apply_reconcile_outcome(ReconcileOutcome("ok", frozenset({key})))
    clock[0] = 145.0
    assert service.read_identity_marker("worker") is None
    clock[0] = 150.1
    assert service.read_identity_marker("worker") is not None


def test_wpq8_m20_remap_waits_for_delivery_lock():
    service = HerdrInboxService(socket_path="/tmp/wpq8.sock")
    _seed_identity(service)
    lock = get_delivery_lock("worker")
    lock.acquire()
    thread = threading.Thread(
        target=service._remap_terminal_identity,
        args=("worker", "pane-1", "pane-2"),
    )
    thread.start()
    time.sleep(0.05)
    assert thread.is_alive()
    assert service._terminal_to_pane["worker"] == "pane-1"
    lock.release()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert service._terminal_to_pane["worker"] == "pane-2"
    assert service.read_identity_marker("worker") is None


@pytest.mark.asyncio
async def test_wpq8_m24_guard_capability_registration_and_states():
    service = HerdrInboxService(socket_path="/tmp/wpq8.sock")
    guard = DeliveryGuard("worker", asyncio.get_running_loop())
    assert guard.active is False
    with pytest.raises(RuntimeError):
        service._register_terminal_under_guard("worker", "pane-1", False, guard)
    await guard.acquire()
    assert guard.active is True
    service._register_terminal_under_guard("worker", "pane-1", False, guard)
    with pytest.raises(RuntimeError):
        service._register_terminal_under_guard("other", "pane-2", False, guard)
    await guard.close()
    assert guard.active is False

    cancelled = DeliveryGuard("cancelled", asyncio.get_running_loop())
    cancelled.cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled.acquire()
    assert cancelled.active is False


def test_wpq8_m4_m5_null_and_mismatched_direct_rows_are_digested(wpq8_db):
    with wpq8_db.begin() as db:
        terminal = TerminalModel(
            id="worker",
            tmux_session="session",
            tmux_window="window",
            provider="codex",
            lifecycle_generation=3,
        )
        db.add(terminal)
        db.add_all(
            [
                InboxModel(
                    sender_id="sender",
                    receiver_id="worker",
                    enqueue_generation=None,
                    message="null",
                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                    status=MessageStatus.PENDING.value,
                ),
                InboxModel(
                    sender_id="sender",
                    receiver_id="worker",
                    enqueue_generation=2,
                    message="old",
                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                    status=MessageStatus.PENDING.value,
                ),
            ]
        )
    assert mailbox_service.digest_stale_pending_for_terminal("worker") == 2
    with wpq8_db() as db:
        rows = db.query(InboxModel).order_by(InboxModel.id).all()
        assert [row.status for row in rows[:2]] == ["digested", "digested"]
        assert rows[2].status == "pending"
        assert rows[2].enqueue_generation == 3


def _constructor_owners(path: Path) -> tuple[set[str], dict[str, bool]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owners: set[str] = set()
    stamped: dict[str, bool] = {}

    def walk(node, stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = [*stack, node.name]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "InboxModel"
        ):
            owner = ".".join(stack)
            owners.add(owner)
            functions = [
                candidate
                for candidate in ast.walk(tree)
                if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
                and candidate.name == stack[0]
            ]
            stamped[owner] = any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_stamp_enqueue_generation"
                for function in functions
                for call in ast.walk(function)
            )
        for child in ast.iter_child_nodes(node):
            walk(child, stack)

    walk(tree, [])
    return owners, stamped


def test_wpq8_m16_all_twelve_inbox_writers_use_the_stamp_helper():
    root = Path(__file__).parents[2] / "src" / "cli_agent_orchestrator"
    db_owners, db_stamped = _constructor_owners(root / "clients" / "database.py")
    mailbox_owners, mailbox_stamped = _constructor_owners(root / "services" / "mailbox_service.py")
    owners = db_owners | mailbox_owners
    expected = {
        "claim_deferred_init_failure",
        "_fire_open_barrier_in_db",
        "_insert_routed_inbox_row",
        "insert_barrier_escalation_message",
        "insert_watchdog_auto_resume_message",
        "insert_identity_authority_notice",
        "_record_p5_orphan_notices",
        "record_wpm1_stalled_notice.operation",
        "settle_wpm1_terminal_batch.operation",
        "publish_supervisor_incarnation",
        "digest_stale_pending_for_terminal",
        "delete_mailbox",
    }
    assert owners == expected
    unstamped = {
        owner for owner, stamped in {**db_stamped, **mailbox_stamped}.items() if not stamped
    }
    assert unstamped == set()


def test_wpq8_lifecycle_increments_run_under_delivery_lock():
    root = Path(__file__).parents[2] / "src" / "cli_agent_orchestrator" / "services"
    terminal_source = (root / "terminal_service.py").read_text(encoding="utf-8")
    mailbox_source = (root / "mailbox_service.py").read_text(encoding="utf-8")
    rebind_source = (root / "provider_rebind_service.py").read_text(encoding="utf-8")
    assert "with delivery_authority:" in terminal_source
    assert terminal_source.count("create_terminal_with_warm_intent") >= 2
    assert terminal_source.count("db_create_terminal") >= 2
    assert "delivery_lock = get_delivery_lock(terminal_id)" in mailbox_source
    assert "await guard.acquire()" in rebind_source
    assert "settle_terminal_rebound(terminal_id" in rebind_source
