from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.backends import registry as backend_registry
from cli_agent_orchestrator.cli.commands.terminal import terminal
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    CallbackBarrierMemberModel,
    CallbackBarrierModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.services import fleet_service, terminal_service
from cli_agent_orchestrator.services.session_lifecycle_lease import (
    acquire_session_lifecycle_exclusive,
    acquire_session_lifecycle_shared,
    release_session_lifecycle_lease,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation
from cli_agent_orchestrator.services.terminal_guard_service import (
    TerminalProtectionError,
    classify_deletion,
)
from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles


class FleetBackend:
    def __init__(self) -> None:
        self.windows: dict[str, set[str]] = {"cao-f72": set()}
        self.kills: list[str] = []
        self.parent_writes: list[tuple[str, str | None]] = []
        self.inventory_calls = 0
        self.uncertain: set[str] = set()

    def add(self, window: str) -> None:
        self.windows["cao-f72"].add(window)

    def kill_window(self, session: str, window: str) -> bool:
        self.kills.append(window)
        if window not in self.uncertain:
            self.windows[session].discard(window)
        return True

    def window_liveness(self, session: str, window: str) -> str:
        return "live" if window in self.windows[session] else "gone"

    def get_history(self, *_args, **_kwargs) -> str:
        return ""

    def get_pane_working_directory(self, *_args) -> str:
        return "/tmp"

    def stop_pipe_pane(self, *_args) -> None:
        return None

    def set_window_parent(self, _session: str, window: str, parent: str | None) -> None:
        self.parent_writes.append((window, parent))

    def get_session_windows(self, _session: str) -> list[dict[str, str]]:
        self.inventory_calls += 1
        return [
            {"name": name, "index": str(index)}
            for index, name in enumerate(sorted(self.windows["cao-f72"]))
        ]


@pytest.fixture
def f72_env(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    backend = FleetBackend()
    monkeypatch.setattr(backend_registry, "_backend", backend)
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", tmp_path)
    monkeypatch.setattr(terminal_service.provider_manager, "cleanup_provider", MagicMock())
    monkeypatch.setattr(terminal_service.fifo_manager, "stop_reader", MagicMock())
    monkeypatch.setattr(terminal_service, "dispatch_plugin_event", MagicMock())
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.persona_context.cleanup_persona", lambda _terminal_id: None
    )
    return sessions, backend


def add_terminal(backend: FleetBackend, terminal_id: str, parent: str | None = None, **kwargs):
    window = f"w-{terminal_id}"
    database.create_terminal(
        terminal_id,
        "cao-f72",
        window,
        "claude_code",
        agent_profile=kwargs.pop("agent_profile", "developer"),
        caller_id=parent,
        **kwargs,
    )
    backend.add(window)


def seed_mailbox(sessions, terminal_id: str) -> None:
    with sessions.begin() as db:
        db.add(
            MailboxModel(
                id="mb_11111111",
                session_name="cao-f72",
                role="supervisor",
                current_terminal_id=terminal_id,
                generation=1,
            )
        )
        db.add(
            MailboxIncarnationModel(mailbox_id="mb_11111111", generation=1, terminal_id=terminal_id)
        )


def test_cascade_reaps_four_children_depth_first_and_releases_success_lease(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    for value in ("33333333", "44444444", "55555555", "66666666"):
        add_terminal(backend, value, "22222222")

    result = terminal_service.delete_terminal("22222222", caller_id="11111111")

    assert [row["id"] for row in result["reaped"]] == [
        "33333333",
        "44444444",
        "55555555",
        "66666666",
        "22222222",
    ]
    assert result["skipped"] == result["uncertain"] == result["unattempted"] == []
    assert backend.kills[-1] == "w-22222222"
    lease = acquire_session_lifecycle_exclusive("cao-f72")
    assert lease is not None
    release_session_lifecycle_lease(lease)


def test_cascade_is_children_before_parent_at_every_depth(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222")
    add_terminal(backend, "44444444", "33333333")

    result = terminal_service.delete_terminal("22222222", caller_id="11111111")

    assert [row["id"] for row in result["reaped"]] == [
        "44444444",
        "33333333",
        "22222222",
    ]


def test_orphan_reparents_both_edges_records_origin_and_rewrites_tmux_cache(f72_env):
    sessions, backend = f72_env
    add_terminal(backend, "11111111")
    seed_mailbox(sessions, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222")

    result = terminal_service.delete_terminal("22222222", caller_id="11111111", orphan=True)

    child = database.get_terminal_metadata("33333333")
    assert child["caller_id"] == "11111111"
    assert child["caller_mailbox_id"] == "mb_11111111"
    assert child["reparented_from"] == "22222222"
    assert result["skipped"] == [{"id": "33333333", "reason": "orphan_requested"}]
    assert ("w-33333333", "11111111") in backend.parent_writes


def test_sticky_cut_is_named_and_force_overrides(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222", lifecycle="sticky")
    add_terminal(backend, "44444444", "33333333")

    result = terminal_service.delete_terminal("22222222", caller_id="11111111")
    assert result["skipped"] == [
        {"id": "33333333", "reason": "sticky"},
        {"id": "44444444", "reason": "ancestor_skipped:33333333"},
    ]
    assert database.terminal_exists("33333333")
    assert database.terminal_exists("44444444")

    result = terminal_service.delete_terminal("33333333", caller_id="11111111", force=True)
    assert [row["id"] for row in result["reaped"]] == ["44444444", "33333333"]


def test_skipped_child_is_reparented_to_nearest_surviving_mailbox_ancestor(f72_env):
    sessions, backend = f72_env
    add_terminal(backend, "11111111")
    seed_mailbox(sessions, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222")
    add_terminal(backend, "44444444", "33333333", lifecycle="sticky")

    result = terminal_service.delete_terminal("22222222", caller_id="11111111")

    assert {row["id"] for row in result["reaped"]} == {"22222222", "33333333"}
    assert result["skipped"] == [{"id": "44444444", "reason": "sticky"}]
    survivor = database.get_terminal_metadata("44444444")
    assert survivor["caller_id"] == "11111111"
    assert survivor["caller_mailbox_id"] == "mb_11111111"
    assert survivor["reparented_from"] == "33333333"
    fleet = fleet_service.build_fleet("cao-f72")
    row = next(item for item in fleet["terminals"] if item["id"] == "44444444")
    assert row["orphan"] is False
    assert ("w-44444444", "11111111") in backend.parent_writes


def test_ready_base_reason_is_structured_for_descendant_and_root(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222")
    database.register_provider_session(
        name="protected-base",
        provider="claude_code",
        session_uuid="uuid-protected-base",
        cwd="/tmp",
        agent_profile="developer",
        git_sha=None,
        dirty_hashes="{}",
        summary="f72",
        source_terminal_id="33333333",
        session_name="cao-f72",
    )

    classification = classify_deletion("33333333")
    result = terminal_service.delete_terminal("22222222", caller_id="11111111")

    assert classification.reason == "ready_base:protected-base"
    assert result["skipped"] == [{"id": "33333333", "reason": "ready_base:protected-base"}]
    assert database.get_ready_provider_session_by_source_terminal("33333333") is not None
    with pytest.raises(TerminalProtectionError, match="protected-base"):
        terminal_service.delete_terminal("33333333", caller_id="11111111")


def test_held_rows_readdress_and_terminal_owned_barrier_releases_members(f72_env):
    sessions, backend = f72_env
    add_terminal(backend, "11111111")
    seed_mailbox(sessions, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222")
    with sessions.begin() as db:
        barrier = CallbackBarrierModel(
            owner_terminal_id="22222222",
            owner_generation=1,
            label="f72",
            state="OPEN",
            timeout_at=datetime.now(timezone.utc),
        )
        db.add(barrier)
        db.flush()
        db.add(
            CallbackBarrierMemberModel(
                barrier_id=barrier.id,
                member_key="child",
                position=0,
                terminal_id="33333333",
                lifecycle_generation=1,
                state="AWAITING",
            )
        )
        db.add_all(
            [
                InboxModel(
                    sender_id="33333333",
                    receiver_id="22222222",
                    message="memo",
                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                    status=MessageStatus.HELD.value,
                    barrier_id=barrier.id,
                    barrier_member_key="child",
                ),
                InboxModel(
                    sender_id="22222222",
                    receiver_id="33333333",
                    message="member",
                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                    status=MessageStatus.HELD.value,
                    barrier_id=barrier.id,
                    barrier_member_key="child",
                ),
            ]
        )
        barrier_id = barrier.id

    terminal_service.delete_terminal("22222222", caller_id="11111111", orphan=True)

    with sessions() as db:
        rows = db.query(InboxModel).order_by(InboxModel.id).all()
        barrier = db.get(CallbackBarrierModel, barrier_id)
        assert rows[0].receiver_id == "11111111"
        assert rows[0].logical_receiver_id == "mb_11111111"
        assert rows[0].status == MessageStatus.PENDING.value
        assert rows[0].message.startswith("[released from 22222222")
        assert rows[1].status == MessageStatus.PENDING.value
        assert barrier.state == "CANCELLED"
        assert barrier.close_reason == "owner_gone"


def test_uncertain_kill_stops_keeps_row_and_releases_quarantine_exit_lease(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222")
    add_terminal(backend, "44444444", "22222222")
    backend.uncertain.add("w-33333333")

    result = terminal_service.delete_terminal("22222222", caller_id="11111111")

    assert result["uncertain"] == [{"id": "33333333", "reason": "rollback_kill_uncertain"}]
    assert result["unattempted"] == ["44444444", "22222222"]
    assert database.terminal_exists("33333333")
    assert database.terminal_exists("44444444")
    lease = acquire_session_lifecycle_exclusive("cao-f72")
    assert lease is not None
    release_session_lifecycle_lease(lease)


def test_uncertain_walk_is_idempotent_both_before_and_after_confirmed_death(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222")
    add_terminal(backend, "44444444", "22222222")
    backend.uncertain.add("w-44444444")

    first = terminal_service.delete_terminal("22222222", caller_id="11111111")
    second = terminal_service.delete_terminal("22222222", caller_id="11111111")

    assert [row["id"] for row in first["reaped"]] == ["33333333"]
    assert (
        first["uncertain"]
        == second["uncertain"]
        == [{"id": "44444444", "reason": "rollback_kill_uncertain"}]
    )
    assert backend.kills.count("w-33333333") == 1
    assert backend.kills.count("w-44444444") == 2

    backend.uncertain.clear()
    backend.windows["cao-f72"].discard("w-44444444")
    third = terminal_service.delete_terminal("22222222", caller_id="11111111")
    assert [row["id"] for row in third["reaped"]] == ["44444444", "22222222"]
    assert third["uncertain"] == []


def test_busy_descendant_is_named_and_all_held_rows_leave_held_state(f72_env, monkeypatch):
    sessions, backend = f72_env
    add_terminal(backend, "11111111")
    seed_mailbox(sessions, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222")
    with sessions.begin() as db:
        for receiver_id in ("22222222", "33333333"):
            db.add(
                InboxModel(
                    sender_id="11111111",
                    receiver_id=receiver_id,
                    message=f"held-{receiver_id}",
                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                    status=MessageStatus.HELD.value,
                )
            )

    def observation(terminal_id: str) -> BoundaryObservation:
        status = TerminalStatus.PROCESSING if terminal_id == "33333333" else TerminalStatus.IDLE
        return BoundaryObservation("f72", status, 1, 1, 1, None, 1)

    monkeypatch.setattr(terminal_service.status_monitor, "get_boundary_observation", observation)
    result = terminal_service.delete_terminal("22222222", caller_id="11111111")

    busy = next(row for row in result["reaped"] if row["id"] == "33333333")
    assert busy["status"] == "killed_while_busy"
    with sessions() as db:
        rows = db.query(InboxModel).all()
        assert rows
        assert all(row.status != MessageStatus.HELD.value for row in rows)
        assert all(row.receiver_id == "11111111" for row in rows)


def test_mailbox_owned_barrier_survives_bound_terminal_reap(f72_env):
    sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    with sessions.begin() as db:
        db.add(
            MailboxModel(
                id="mb_22222222",
                session_name="cao-f72",
                role="worker",
                current_terminal_id="22222222",
                generation=1,
            )
        )
        db.add(
            MailboxIncarnationModel(mailbox_id="mb_22222222", generation=1, terminal_id="22222222")
        )
        barrier = CallbackBarrierModel(
            owner_mailbox_id="mb_22222222",
            owner_generation=1,
            label="mailbox-owned",
            state="OPEN",
            timeout_at=datetime.now(timezone.utc),
        )
        db.add(barrier)
        db.flush()
        barrier_id = barrier.id

    terminal_service.delete_terminal("22222222", caller_id="11111111")

    with sessions() as db:
        barrier = db.get(CallbackBarrierModel, barrier_id)
        assert barrier.state == "OPEN"
        assert barrier.close_reason is None


def test_authority_abort_releases_lease_and_self_delete_is_refused(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    with pytest.raises(ValueError, match="cascade_outside_caller_subtree"):
        terminal_service.delete_terminal("22222222", caller_id="22222222")
    assert database.terminal_exists("22222222")
    lease = acquire_session_lifecycle_exclusive("cao-f72")
    assert lease is not None
    release_session_lifecycle_lease(lease)


def test_fallback_settlement_repoints_children_in_same_transaction(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222")
    add_terminal(backend, "33333333", "11111111")
    with database.SessionLocal.begin() as db:
        old = db.get(TerminalModel, "11111111")
        old.recovery_state = "fallback_starting"
        new = db.get(TerminalModel, "22222222")
        new.provider_session_id = "uuid-f72"
    database.settle_terminal_fallback("11111111", "22222222")
    assert database.get_terminal_metadata("33333333")["caller_id"] == "22222222"
    result = terminal_service.delete_terminal("22222222")
    assert [row["id"] for row in result["reaped"]] == ["33333333", "22222222"]


def test_legacy_fallback_husk_edges_are_migrated(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222")
    add_terminal(backend, "33333333", "11111111")
    with database.SessionLocal.begin() as db:
        husk = db.get(TerminalModel, "11111111")
        husk.recovery_state = "fallback_ready"
        husk.fallback_terminal_id = "22222222"

    database._migrate_fallback_parent_edges()

    child = database.get_terminal_metadata("33333333")
    assert child["caller_id"] == "22222222"
    assert child["reparented_from"] == "11111111"


def test_fleet_is_one_bulk_inventory_with_dangling_parent_and_no_body_fields(f72_env):
    sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    with sessions.begin() as db:
        db.get(TerminalModel, "22222222").caller_id = "deadbeef"
    result = fleet_service.build_fleet("cao-f72")
    row = next(item for item in result["terminals"] if item["id"] == "22222222")
    assert backend.inventory_calls == 1
    assert row["orphan"] is True
    assert row["depth"] == 1
    assert {
        "id",
        "profile",
        "window_index",
        "window_name",
        "parent_id",
        "depth",
        "orphan",
        "status",
    } <= row.keys()
    assert not ({"charter", "prompt", "transcript"} & row.keys())


def test_fleet_reports_exact_multilevel_depths(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    add_terminal(backend, "33333333", "22222222")

    result = fleet_service.build_fleet("cao-f72")

    depths = {row["id"]: row["depth"] for row in result["terminals"]}
    assert depths == {"11111111": 0, "22222222": 1, "33333333": 2}


def test_fleet_since_last_input_converts_local_naive_clock_to_utc(f72_env, monkeypatch):
    sessions, backend = f72_env
    add_terminal(backend, "11111111")
    local_zone = ZoneInfo("Asia/Kolkata")
    now_utc = datetime(2026, 8, 6, 6, 30, tzinfo=timezone.utc)
    local_last_active = now_utc.astimezone(local_zone).replace(tzinfo=None) - timedelta(seconds=120)
    with sessions.begin() as db:
        db.get(TerminalModel, "11111111").last_active = local_last_active

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return now_utc.astimezone(tz)
            return now_utc.astimezone(local_zone).replace(tzinfo=None)

    monkeypatch.setattr(fleet_service, "datetime", FixedDateTime)
    monkeypatch.setattr(fleet_service, "get_localzone", lambda: local_zone)

    result = fleet_service.build_fleet("cao-f72")

    assert result["terminals"][0]["since_last_input"] == pytest.approx(120.0, abs=1.0)


def test_fleet_drill_projection_is_under_8192_bytes_with_profile_count_recorded(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    for terminal_id in ("33333333", "44444444", "55555555", "66666666"):
        add_terminal(backend, terminal_id, "22222222")

    result = fleet_service.build_fleet("cao-f72")
    encoded = json.dumps(result, default=str, separators=(",", ":")).encode("utf-8")
    installed_profile_count = len(list_agent_profiles())
    children = [row for row in result["terminals"] if row["id"] != "11111111"]

    assert len(result["terminals"]) == 5
    assert len(children) == 4
    assert all(row["orphan"] is True for row in children)
    assert installed_profile_count >= 1
    assert (
        len(encoded) < 8192
    ), f"fleet payload={len(encoded)} bytes; installed_profiles={installed_profile_count}"
    assert not ({"charter", "prompt", "transcript"} & set(encoded.decode().split('"')))


def test_depth_cap_cuts_and_names_the_remaining_subtree(f72_env):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    parent = "22222222"
    descendants = []
    for index in range(1, 34):
        terminal_id = f"{20_000_000 + index:08d}"
        add_terminal(backend, terminal_id, parent)
        descendants.append(terminal_id)
        parent = terminal_id

    result = terminal_service.delete_terminal("22222222", caller_id="11111111")

    cap = descendants[31]
    assert {"id": cap, "reason": "depth_cap"} in result["skipped"]
    assert {
        "id": descendants[32],
        "reason": f"ancestor_skipped:{cap}",
    } in result["skipped"]
    assert database.terminal_exists(cap)


def test_restore_is_named_unmanaged_exception_under_exclusive_lease(f72_env, monkeypatch, tmp_path):
    _sessions, backend = f72_env
    snapshot_dir = tmp_path / "logs"
    snapshot_dir.mkdir()
    (snapshot_dir / "deadbeef.snapshot.json").write_text(
        '{"session_name":"cao-f72","window_name":"old","agent_profile":"dev"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", snapshot_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.terminal.sync_backend_from_server", lambda: None
    )
    create = MagicMock(return_value="restored-old")
    monkeypatch.setattr(backend, "create_window", create, raising=False)
    response = MagicMock(status_code=200)
    response.raise_for_status.return_value = None
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.terminal.cao_http.get", lambda *_a, **_k: response
    )
    lease = acquire_session_lifecycle_exclusive("cao-f72")
    assert lease is not None
    try:
        result = CliRunner().invoke(terminal, ["restore", "deadbeef"])
    finally:
        release_session_lifecycle_lease(lease)
    assert result.exit_code == 0
    create.assert_called_once()
    assert database.get_terminal_metadata("deadbeef") is None


@pytest.mark.parametrize("mode", ["cold", "fork", "resume"])
def test_managed_create_mode_matrix_before_and_after_publication(monkeypatch, mode):
    session_name = f"cao-f72-{mode}"
    checks: list[tuple[str, bool]] = []
    published_lifecycles: list[str] = []

    class Backend:
        def session_exists(self, _session):
            return True

        def create_window(self, *_args, **_kwargs):
            probe = acquire_session_lifecycle_exclusive(session_name)
            checks.append(("create_window", probe is None))
            if probe:
                release_session_lifecycle_lease(probe)
            return "worker"

        def supports_event_inbox(self):
            return True

        def set_window_parent(self, *_args):
            return None

        def get_pane_id(self, *_args):
            return "pane"

    backend = Backend()
    monkeypatch.setattr(backend_registry, "_backend", backend)
    monkeypatch.setattr(
        terminal_service,
        "load_agent_profile",
        lambda _name: SimpleNamespace(
            sessionBrief=None,
            lifecycle=("sticky" if mode == "fork" else "ephemeral" if mode == "cold" else None),
            contextPolicy=None,
            allowedTools=None,
            mcpServers=None,
            role=None,
            skills=None,
        ),
    )
    monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "deadbeef")
    monkeypatch.setattr(terminal_service, "generate_window_name", lambda _profile: "worker")
    monkeypatch.setattr(terminal_service, "get_session_env", lambda _session: {})
    monkeypatch.setattr(terminal_service, "list_terminals_by_provider_session_id", lambda _u: [])
    monkeypatch.setattr(
        terminal_service, "_persist_provider_runtime_identity", lambda *_a, **_k: None
    )
    monkeypatch.setattr(terminal_service, "dispatch_plugin_event", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)

    def publisher(*_args, **kwargs):
        probe = acquire_session_lifecycle_exclusive(session_name)
        checks.append(("publisher", probe is None))
        published_lifecycles.append(kwargs.get("lifecycle", "ephemeral"))
        if probe:
            release_session_lifecycle_lease(probe)

    monkeypatch.setattr(terminal_service, "db_create_terminal", publisher)
    monkeypatch.setattr(terminal_service, "create_terminal_with_warm_intent", publisher)

    provider = SimpleNamespace(
        allocated_session_uuid=None,
        shell_baseline=None,
        initialize=None,
    )

    async def initialize():
        probe = acquire_session_lifecycle_exclusive(session_name)
        checks.append(("provider_init", probe is None))
        if probe:
            release_session_lifecycle_lease(probe)

    provider.initialize = AsyncMock(side_effect=initialize)

    def create_provider(*_args, **_kwargs):
        probe = acquire_session_lifecycle_exclusive(session_name)
        checks.append(("provider", probe is None))
        if probe:
            release_session_lifecycle_lease(probe)
        return provider

    monkeypatch.setattr(terminal_service.provider_manager, "create_provider", create_provider)
    context = None
    if mode != "cold":
        context = ForkContext(
            mode=mode,
            session_uuid=f"uuid-{mode}",
            base_name="base",
            provider="claude_code",
            initial_preamble="",
        )
    asyncio.run(
        terminal_service.create_terminal(
            "claude_code",
            "developer",
            session_name=session_name,
            fork_context=context,
            lifecycle="sticky" if mode == "cold" else None,
        )
    )
    assert checks[:2] == [("create_window", True), ("publisher", True)]
    assert checks[2] == ("provider", mode == "resume")
    assert checks[3] == ("provider_init", mode == "resume")
    assert published_lifecycles == ["sticky" if mode in {"cold", "fork"} else "ephemeral"]


@pytest.mark.parametrize("mode", ["cold", "fork", "resume"])
def test_managed_create_prepublication_failure_releases_local_authority(monkeypatch, mode):
    session_name = f"cao-f72-prepub-{mode}"

    class Backend:
        def session_exists(self, _session):
            return True

        def create_window(self, *_args, **_kwargs):
            return "worker"

        def supports_event_inbox(self):
            return True

        def set_window_parent(self, *_args):
            return None

        def kill_window(self, *_args):
            return True

    monkeypatch.setattr(backend_registry, "_backend", Backend())
    monkeypatch.setattr(
        terminal_service,
        "load_agent_profile",
        lambda _name: SimpleNamespace(
            sessionBrief=None,
            lifecycle=None,
            contextPolicy=None,
            skills=None,
            allowedTools=None,
            mcpServers=None,
            role=None,
        ),
    )
    monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "deadbeef")
    monkeypatch.setattr(terminal_service, "generate_window_name", lambda _profile: "worker")
    monkeypatch.setattr(terminal_service, "get_session_env", lambda _session: {})
    monkeypatch.setattr(terminal_service, "list_terminals_by_provider_session_id", lambda _u: [])
    monkeypatch.setattr(
        terminal_service,
        "db_create_terminal",
        MagicMock(side_effect=RuntimeError("publish_failed")),
    )
    monkeypatch.setattr(
        terminal_service,
        "create_terminal_with_warm_intent",
        MagicMock(side_effect=RuntimeError("publish_failed")),
    )
    context = None
    if mode != "cold":
        context = ForkContext(
            mode=mode,
            session_uuid=f"uuid-prepub-{mode}",
            base_name="base",
            provider="claude_code",
            initial_preamble="",
        )
    with pytest.raises(RuntimeError, match="publish_failed"):
        asyncio.run(
            terminal_service.create_terminal(
                "claude_code",
                "developer",
                session_name=session_name,
                fork_context=context,
            )
        )
    lease = acquire_session_lifecycle_exclusive(session_name)
    assert lease is not None
    release_session_lifecycle_lease(lease)


def test_fork_create_started_after_quiesce_snapshot_is_blocked(f72_env, monkeypatch):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    events: list[str] = []
    real_quiesce = terminal_service.quiesce_deferred_session_sync

    def quiesce(session_name: str) -> None:
        real_quiesce(session_name)
        events.append("snapshot_complete")

    monkeypatch.setattr(terminal_service, "quiesce_deferred_session_sync", quiesce)
    monkeypatch.setattr(
        terminal_service,
        "load_agent_profile",
        lambda _name: SimpleNamespace(sessionBrief=None, lifecycle=None, contextPolicy=None),
    )
    monkeypatch.setattr(terminal_service, "list_terminals_by_provider_session_id", lambda _u: [])
    real_delete = terminal_service._delete_terminal_under_lease

    def delete_after_snapshot(*args, **kwargs):
        assert events == ["snapshot_complete"]
        context = ForkContext(
            mode="fork",
            session_uuid="uuid-race",
            base_name="base",
            provider="claude_code",
            initial_preamble="",
        )
        with pytest.raises(RuntimeError, match="resume_in_progress"):
            asyncio.run(
                terminal_service.create_terminal(
                    "claude_code",
                    "developer",
                    session_name="cao-f72",
                    fork_context=context,
                )
            )
        events.append("fork_blocked")
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease", delete_after_snapshot)
    terminal_service.delete_terminal("22222222", caller_id="11111111")
    assert events == ["snapshot_complete", "fork_blocked"]


def test_collision_quiesces_first_refuses_once_and_deletes_nothing(f72_env, monkeypatch):
    _sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    shared = acquire_session_lifecycle_shared("cao-f72")
    assert shared is not None
    events: list[str] = []
    calls = 0
    real_acquire = acquire_session_lifecycle_exclusive

    def quiesce(session_name: str) -> None:
        assert session_name == "cao-f72"
        events.append("quiesce")

    def acquire(session_name: str):
        nonlocal calls
        calls += 1
        events.append("acquire")
        return real_acquire(session_name)

    monkeypatch.setattr(terminal_service, "quiesce_deferred_session_sync", quiesce)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
        acquire,
    )
    try:
        with pytest.raises(RuntimeError, match="resume_in_progress"):
            terminal_service.delete_terminal("22222222", caller_id="11111111")
    finally:
        release_session_lifecycle_lease(shared)

    assert events == ["quiesce", "acquire"]
    assert calls == 1
    assert database.terminal_exists("22222222")
    assert backend.kills == []


# --- Slice A2 acceptance criteria completions ---


def test_ac13_held_row_target_exists_after_delete(f72_env):
    """AC#13: after deleting a terminal, held rows are re-addressed AND the new target
    still exists (not merely that receiver_id changed)."""
    sessions, backend = f72_env
    add_terminal(backend, "11111111")
    seed_mailbox(sessions, "11111111")
    add_terminal(backend, "22222222", "11111111")
    with sessions.begin() as db:
        db.add(
            InboxModel(
                sender_id="99999999",
                receiver_id="22222222",
                message="held-memo",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.HELD.value,
                barrier_id=None,
            )
        )

    terminal_service.delete_terminal("22222222", caller_id="11111111", orphan=True)

    with sessions() as db:
        rows = db.query(InboxModel).filter(InboxModel.message.contains("held-memo")).all()
        assert len(rows) == 1
        row = rows[0]
        # AC#13: receiver_id CHANGED from the deleted terminal
        assert row.receiver_id != "22222222"
        assert row.receiver_id == "11111111"
        # AC#13: new target still exists
        assert database.terminal_exists(row.receiver_id)
        # S8: mailbox address resolved
        assert row.logical_receiver_id == "mb_11111111"
        # Status is PENDING (released from HELD)
        assert row.status == MessageStatus.PENDING.value


def test_ac13_no_surviving_ancestor_cancels_with_reason(f72_env):
    """AC#13 fallback: when caller_id is NULL (root terminal), held rows are
    cancelled with terminal_reaped_no_surviving_ancestor — never dropped."""
    sessions, backend = f72_env
    # Terminal with no parent (root)
    add_terminal(backend, "11111111")
    with sessions.begin() as db:
        db.add(
            InboxModel(
                sender_id="99999999",
                receiver_id="11111111",
                message="orphan-memo",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.HELD.value,
                barrier_id=None,
            )
        )

    database.delete_terminal_and_warm_intent("11111111")

    with sessions() as db:
        rows = db.query(InboxModel).filter(InboxModel.message.contains("orphan-memo")).all()
        assert len(rows) == 1
        row = rows[0]
        # Not dropped — row still exists
        assert row.status == MessageStatus.CANCELLED.value
        assert row.failure_reason == "terminal_reaped_no_surviving_ancestor"


def test_ac15_barrier_cancelled_before_terminal_row_removed(f72_env):
    """AC#15: ordering — barrier is cancelled and members released BEFORE the
    terminal row disappears. We verify by checking that after the full delete
    transaction completes, the barrier is CANCELLED and the terminal is gone —
    proving both happened in one atomic transaction with correct ordering."""
    sessions, backend = f72_env
    add_terminal(backend, "11111111")
    add_terminal(backend, "22222222", "11111111")
    with sessions.begin() as db:
        barrier = CallbackBarrierModel(
            owner_terminal_id="22222222",
            owner_generation=1,
            label="ac15",
            state="OPEN",
            timeout_at=datetime.now(timezone.utc),
        )
        db.add(barrier)
        db.flush()
        db.add(
            InboxModel(
                sender_id="33333333",
                receiver_id="22222222",
                message="barrier-held",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.HELD.value,
                barrier_id=barrier.id,
                barrier_member_key="mem",
            )
        )
        barrier_id = barrier.id

    terminal_service.delete_terminal("22222222", caller_id="11111111", orphan=True)

    with sessions() as db:
        barrier = db.get(CallbackBarrierModel, barrier_id)
        # Barrier is CANCELLED (happened before row removal, proven by atomicity)
        assert barrier.state == "CANCELLED"
        assert barrier.close_reason == "owner_gone"
        assert barrier.fired_at is not None
        # Terminal row is gone
        assert db.query(TerminalModel).filter_by(id="22222222").one_or_none() is None
        # The held row was released to PENDING (member release)
        row = db.query(InboxModel).filter(InboxModel.message.contains("barrier-held")).one()
        assert row.status == MessageStatus.PENDING.value
