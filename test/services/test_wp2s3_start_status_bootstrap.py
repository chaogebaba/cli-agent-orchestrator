"""Frozen WP2S3 pins: start/status/seed and UUID ownership law."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base, TerminalModel
from cli_agent_orchestrator.models.terminal import ForkContext
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services import session_status_service, terminal_service
from cli_agent_orchestrator.services.provider_session_lease import (
    acquire_provider_session_lease,
    provider_session_lease_held,
    release_provider_session_lease,
)
from cli_agent_orchestrator.services.rebind_lease import (
    acquire_rebind_lease, release_rebind_lease,
)
from cli_agent_orchestrator.services.session_lifecycle_lease import (
    acquire_session_lifecycle_exclusive, acquire_session_lifecycle_shared,
    release_session_lifecycle_lease,
)


@pytest.fixture
def real_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    return sessions


def test_uuid_lease_is_non_reentrant_generation_bound():
    first = acquire_provider_session_lease("u-lease")
    assert first is not None and provider_session_lease_held("u-lease")
    assert acquire_provider_session_lease("u-lease") is None
    release_provider_session_lease(first)
    second = acquire_provider_session_lease("u-lease")
    assert second is not None and second != first
    with pytest.raises(RuntimeError, match="invalid_provider_session_lease_token"):
        release_provider_session_lease(first)
    release_provider_session_lease(second)


def test_session_lifecycle_shared_intent_and_exclusive_are_nonblocking():
    shared1 = acquire_session_lifecycle_shared("cao-lease")
    shared2 = acquire_session_lifecycle_shared("cao-lease")
    assert shared1 is not None and shared2 is not None
    assert acquire_session_lifecycle_exclusive("cao-lease") is None
    release_session_lifecycle_lease(shared2)
    release_session_lifecycle_lease(shared1)
    exclusive = acquire_session_lifecycle_exclusive("cao-lease")
    assert exclusive is not None
    assert acquire_session_lifecycle_shared("cao-lease") is None
    release_session_lifecycle_lease(exclusive)


@pytest.mark.parametrize("failure", ["owner_query", "profile_load", "profile_validation"])
def test_resume_prepublication_failures_release_all_locally_owned_authority(
    monkeypatch, failure,
):
    session_name = f"cao-prepub-{failure}"
    session_uuid = f"uuid-prepub-{failure}"
    context = ForkContext(
        mode="resume", session_uuid=session_uuid, base_name="b",
        provider="grok_cli", initial_preamble="",
    )
    monkeypatch.setattr(
        terminal_service, "list_terminals_by_provider_session_id", lambda _u: []
    )
    if failure == "owner_query":
        monkeypatch.setattr(
            terminal_service, "list_terminals_by_provider_session_id",
            MagicMock(side_effect=RuntimeError("owner_query_boom")),
        )
        expected = "owner_query_boom"
    elif failure == "profile_load":
        monkeypatch.setattr(
            terminal_service, "load_agent_profile",
            MagicMock(side_effect=RuntimeError("profile_parse_boom")),
        )
        expected = "profile_parse_boom"
    else:
        monkeypatch.setattr(
            terminal_service, "load_agent_profile",
            lambda _p: AgentProfile(
                name="dev", description="", sessionBrief="required"
            ),
        )
        expected = "sessionBrief requires"
    with pytest.raises((RuntimeError, ValueError), match=expected):
        asyncio.run(terminal_service.create_terminal(
            "kiro_cli" if failure == "profile_validation" else "grok_cli",
            "dev", session_name=session_name, fork_context=context,
        ))
    assert not provider_session_lease_held(session_uuid)
    exclusive = acquire_session_lifecycle_exclusive(session_name)
    assert exclusive is not None
    release_session_lifecycle_lease(exclusive)


def test_invalid_supplied_uuid_token_releases_locally_owned_lifecycle(monkeypatch):
    wrong = acquire_provider_session_lease("wrong-prepub-uuid")
    assert wrong is not None
    context = ForkContext(
        mode="resume", session_uuid="wanted-prepub-uuid", base_name="b",
        provider="grok_cli", initial_preamble="",
    )
    with pytest.raises(RuntimeError, match="invalid_provider_session_lease_token"):
        asyncio.run(terminal_service.create_terminal(
            "grok_cli", "dev", session_name="cao-invalid-prepub",
            fork_context=context, uuid_lease_token=wrong,
        ))
    exclusive = acquire_session_lifecycle_exclusive("cao-invalid-prepub")
    assert exclusive is not None
    release_session_lifecycle_lease(exclusive)
    release_provider_session_lease(wrong)


@pytest.mark.parametrize("flow", ["fallback", "epoch"])
def test_teardown_vs_resume_flow_is_nonblocking_without_deadlock(flow):
    exclusive = acquire_session_lifecycle_exclusive(f"cao-{flow}")
    assert exclusive is not None
    started = time.monotonic()
    assert acquire_session_lifecycle_shared(f"cao-{flow}") is None
    assert time.monotonic() - started < 0.1
    release_session_lifecycle_lease(exclusive)


def test_runtime_confirmation_write_first_supersedes_current_read_set(real_db):
    database.create_terminal("old1", "s", "w1", "codex", provider_session_id="uuid")
    database.create_terminal("old2", "s", "w2", "codex", provider_session_id="uuid")
    database.create_terminal("new", "s", "w3", "codex", provider_session_id="uuid")
    assert database.update_terminal_runtime_identity(
        "new", "uuid", "bash", supersede_other_claims=True
    )
    assert database.get_terminal_metadata("new")["provider_session_id"] == "uuid"
    assert database.get_terminal_metadata("old1")["provider_session_id"] is None
    assert database.get_terminal_metadata("old2")["provider_session_id"] is None


def test_quarantine_cas_never_attaches_second_owner(real_db):
    database.create_terminal("winner", "s", "w1", "codex", provider_session_id="uuid")
    database.create_terminal("loser", "s", "w2", "codex")
    assert database.quarantine_terminal_owner("loser", "uuid", "boom") == "skipped_existing_owner"
    assert database.get_terminal_metadata("loser")["provider_session_id"] is None
    assert database.get_terminal_metadata("loser")["recovery_state"] == "rebind_failed"


def test_quarantine_missing_row_is_a_failure_token(real_db):
    assert database.quarantine_terminal_owner("missing", "uuid", "boom") == ""


@pytest.mark.parametrize("first", ["quarantine", "confirm"])
def test_real_sqlite_two_thread_commit_orders_converge(tmp_path, monkeypatch, first):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.create_terminal("old", "s", "w1", "codex")
    database.create_terminal("new", "s", "w2", "codex", provider_session_id="uuid")
    go_second = threading.Event()
    errors = []

    def quarantine():
        try:
            if first != "quarantine":
                go_second.wait(2)
            database.quarantine_terminal_owner("old", "uuid", "uncertain")
            if first == "quarantine":
                go_second.set()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def confirm():
        try:
            if first != "confirm":
                go_second.wait(2)
            database.update_terminal_runtime_identity(
                "new", "uuid", "bash", supersede_other_claims=True
            )
            if first == "confirm":
                go_second.set()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=quarantine), threading.Thread(target=confirm)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert not errors
    assert database.get_terminal_metadata("new")["provider_session_id"] == "uuid"
    assert database.get_terminal_metadata("old")["provider_session_id"] is None


def test_attach_between_preflight_and_publication_is_cleared(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'attach.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    database.create_terminal("late", "s", "w1", "codex")
    assert database.quarantine_terminal_owner("late", "uuid", "uncertain") == "associated"
    database.create_terminal("new", "s", "w2", "codex", provider_session_id="uuid")
    assert database.update_terminal_runtime_identity(
        "new", "uuid", "bash", supersede_other_claims=True
    )
    assert database.get_terminal_metadata("late")["provider_session_id"] is None


def test_fallback_settlement_transfers_full_uuid_read_set(real_db):
    database.create_terminal("source", "s", "w1", "codex", provider_session_id="uuid")
    database.create_terminal("gone", "s", "w2", "codex", provider_session_id="uuid")
    database.create_terminal("replacement", "s", "w3", "codex", provider_session_id="uuid")
    database.set_terminal_recovery_state("source", "fallback_starting")
    database.settle_terminal_fallback("source", "replacement")
    assert database.get_terminal_metadata("replacement")["provider_session_id"] == "uuid"
    assert database.get_terminal_metadata("source")["provider_session_id"] is None
    assert database.get_terminal_metadata("gone")["provider_session_id"] is None


def test_status_durable_only_and_ledger_honesty(monkeypatch):
    monkeypatch.setattr(session_status_service.get_backend(), "session_exists", lambda _s: False)
    monkeypatch.setattr(session_status_service, "list_terminals_by_session", lambda _s: [])
    monkeypatch.setattr(session_status_service, "list_ready_provider_sessions_for_session", lambda _s: [{
        "name": "base", "agent_profile": "dev", "provider": "codex", "session_uuid": "u"
    }])
    monkeypatch.setattr(session_status_service, "list_warm_intents", lambda _s: [])
    monkeypatch.setattr(session_status_service, "get_session_epoch", lambda _s: None)
    result = session_status_service.build_session_status("cao-s")
    assert result["manifest"] is None and result["manifest_error"] == "no_terminals"
    assert result["ledger"] == {"available": False, "count": None}
    assert result["ready_bases"][0]["base_name"] == "base"


def test_seed_required_is_before_every_side_effect(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal_service, "get_provider_class", lambda _p: SimpleNamespace(
        supports_seed_resume_identity=True
    ))
    monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: calls.append("id"))
    monkeypatch.setattr(terminal_service, "get_backend", lambda: calls.append("backend"))
    with pytest.raises(RuntimeError, match="seed_required"):
        asyncio.run(terminal_service.create_terminal("codex", "dev"))
    assert calls == []


def test_resume_publication_and_identity_confirmation(monkeypatch):
    context = ForkContext(mode="resume", session_uuid="uuid-r", base_name="b",
                          provider="grok_cli", initial_preamble="")
    backend = MagicMock()
    backend.session_exists.return_value = False
    backend.supports_event_inbox.return_value = True
    backend.window_liveness.return_value = "gone"
    provider = MagicMock(supports_reauth_rebind=True, allocated_session_uuid=None)
    provider.initialize = AsyncMock(return_value=True)
    provider.resume_session_uuid.return_value = "uuid-r"
    provider.validate_session_artifact.return_value = None
    provider.shell_baseline = "bash"
    published = []
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "load_agent_profile", MagicMock(side_effect=FileNotFoundError))
    monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "new")
    monkeypatch.setattr(terminal_service, "generate_session_name", lambda: "cao-s")
    monkeypatch.setattr(terminal_service, "generate_window_name", lambda _p: "w")
    monkeypatch.setattr(terminal_service, "db_create_terminal",
                        lambda *_a, **kw: published.append(kw.get("provider_session_id")))
    monkeypatch.setattr(terminal_service, "list_terminals_by_provider_session_id", lambda _u: [])
    monkeypatch.setattr(terminal_service.provider_manager, "create_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(
        terminal_service, "_persist_provider_runtime_identity", lambda *_a, **_k: None
    )
    monkeypatch.setattr(terminal_service, "dispatch_plugin_event", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    result = asyncio.run(terminal_service.create_terminal(
        "grok_cli", "dev", new_session=True, fork_context=context
    ))
    assert published == ["uuid-r"] and result.provider_session_id == "uuid-r"


@pytest.mark.parametrize("kind", ["fresh_codex", "fresh_grok"])
def test_fresh_reauth_identity_persists_null_to_allocated_or_capture(
    real_db, monkeypatch, kind,
):
    database.create_terminal("term", "s", "w", "codex" if kind == "fresh_codex" else "grok_cli")
    calls = []

    class Provider:
        supports_reauth_rebind = True
        shell_baseline = "bash"
        allocated_session_uuid = "grok-u" if kind == "fresh_grok" else None
        def resume_session_uuid(self):
            calls.append("hint")
            return None
        def capture_session_uuid(self, *_args):
            calls.append("capture")
            return "codex-u"
        def validate_session_artifact(self, uuid, _cwd):
            calls.append(("validate", uuid))

    backend = MagicMock()
    backend.get_pane_working_directory.return_value = "/work"
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fork_context_service.pane_pid", lambda *_a: 1
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fork_context_service.pane_launch_epoch", lambda _p: 1
    )
    terminal_service._persist_provider_runtime_identity(Provider(), "term")
    expected = "grok-u" if kind == "fresh_grok" else "codex-u"
    assert database.get_terminal_metadata("term")["provider_session_id"] == expected
    assert ("capture" in calls) is (kind == "fresh_codex")


@pytest.mark.parametrize(
    "allocated,hint,capture,expected",
    [("allocated", "hint", "captured", "allocated"),
     (None, "hint", "captured", "hint"),
     (None, None, "captured", "captured")],
)
def test_identity_precedence_allocated_then_hint_then_capture(
    real_db, monkeypatch, allocated, hint, capture, expected,
):
    database.create_terminal("term", "s", "w", "grok_cli")
    calls = []
    class Provider:
        supports_reauth_rebind = True
        shell_baseline = "bash"
        allocated_session_uuid = allocated
        def resume_session_uuid(self):
            calls.append("hint")
            return hint
        def capture_session_uuid(self, *_args):
            calls.append("capture")
            return capture
        def validate_session_artifact(self, uuid, _cwd):
            assert uuid == expected
    backend = MagicMock()
    backend.get_pane_working_directory.return_value = "/work"
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr("cli_agent_orchestrator.services.fork_context_service.pane_pid", lambda *_a: 1)
    monkeypatch.setattr("cli_agent_orchestrator.services.fork_context_service.pane_launch_epoch", lambda _p: 1)
    terminal_service._persist_provider_runtime_identity(Provider(), "term")
    assert database.get_terminal_metadata("term")["provider_session_id"] == expected
    assert ("capture" in calls) is (allocated is None and hint is None)


@pytest.mark.parametrize("bad", [object(), RuntimeError("hint boom")])
def test_malformed_or_raising_hint_fails_closed(real_db, monkeypatch, bad):
    database.create_terminal("term", "s", "w", "grok_cli")
    class Provider:
        supports_reauth_rebind = True
        shell_baseline = "bash"
        allocated_session_uuid = None
        def resume_session_uuid(self):
            if isinstance(bad, Exception):
                raise bad
            return bad
        def capture_session_uuid(self, *_args):
            pytest.fail("malformed hint must not fall through")
    backend = MagicMock()
    backend.get_pane_working_directory.return_value = "/work"
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr("cli_agent_orchestrator.services.fork_context_service.pane_pid", lambda *_a: 1)
    monkeypatch.setattr("cli_agent_orchestrator.services.fork_context_service.pane_launch_epoch", lambda _p: 1)
    with pytest.raises(RuntimeError, match="identity_persist_failed"):
        terminal_service._persist_provider_runtime_identity(Provider(), "term")
    assert database.get_terminal_metadata("term")["provider_session_id"] is None


@pytest.mark.parametrize("caller", ["sync", "deferred"])
def test_published_failure_waits_for_public_teardown_holder_and_settles(
    monkeypatch, caller,
):
    terminal_id = f"race-{caller}"
    uuid = f"uuid-{caller}"
    uuid_token = acquire_provider_session_lease(uuid)
    public = acquire_rebind_lease(terminal_id)
    assert uuid_token is not None and public is not None
    calls = []
    monkeypatch.setattr(
        terminal_service, "get_terminal_metadata",
        lambda _tid: {"id": terminal_id, "provider_session_id": uuid},
    )
    monkeypatch.setattr(
        terminal_service, "_delete_terminal_under_lease",
        lambda *_a, **_k: calls.append("confirmed-delete") or {"terminal_deleted": True},
    )
    result = {}
    thread = threading.Thread(target=lambda: result.update(
        terminal_service._settle_published_creation_failure(
            terminal_id, uuid, uuid_token, None
        )
    ))
    thread.start()
    time.sleep(0.03)
    assert calls == []
    release_rebind_lease(public)
    thread.join(2)
    assert result == {"status": "deleted", "error_code": None}
    assert calls == ["confirmed-delete"]
    release_provider_session_lease(uuid_token)


@pytest.mark.parametrize("token_kind", ["missing", "wrong", "stale"])
def test_provisional_teardown_rejects_missing_wrong_or_stale_owner_token(
    monkeypatch, token_kind,
):
    uuid = f"guard-{token_kind}"
    owner = acquire_provider_session_lease(uuid)
    assert owner is not None
    rebind = acquire_rebind_lease(f"term-{token_kind}")
    assert rebind is not None
    presented = None
    if token_kind == "wrong":
        presented = acquire_provider_session_lease("other-uuid")
    elif token_kind == "stale":
        stale = acquire_provider_session_lease("stale-uuid")
        release_provider_session_lease(stale)
        presented = stale
    monkeypatch.setattr(
        terminal_service, "get_terminal_metadata",
        lambda _tid: {"provider_session_id": uuid},
    )
    with pytest.raises(RuntimeError, match="resume_in_progress"):
        terminal_service._delete_terminal_under_lease(
            f"term-{token_kind}", rebind, require_confirmed_death=True,
            uuid_lease_token=presented,
        )
    release_rebind_lease(rebind)
    release_provider_session_lease(owner)
    if token_kind == "wrong":
        release_provider_session_lease(presented)


@pytest.mark.parametrize("token_kind", ["missing", "wrong", "stale"])
def test_fallback_source_requires_exact_live_source_token(monkeypatch, token_kind):
    source_id = f"source-{token_kind}"
    valid = acquire_rebind_lease(source_id)
    assert valid is not None
    token = None
    extra = None
    if token_kind == "wrong":
        extra = acquire_rebind_lease("unrelated")
        token = extra
    elif token_kind == "stale":
        stale = acquire_rebind_lease("stale-source")
        release_rebind_lease(stale)
        token = stale
    monkeypatch.setattr(
        terminal_service, "list_terminals_by_provider_session_id",
        lambda _u: [{"id": source_id, "tmux_session": "s", "tmux_window": "w"}],
    )
    monkeypatch.setattr(
        terminal_service, "get_terminal_metadata",
        lambda _tid: {"provider_session_id": "fallback-u", "recovery_state": "fallback_starting"},
    )
    context = ForkContext(mode="resume", session_uuid="fallback-u", base_name="b",
                          provider="grok_cli", initial_preamble="")
    with pytest.raises(RuntimeError, match="owner_conflict"):
        asyncio.run(terminal_service.create_terminal(
            "grok_cli", "dev", fork_context=context,
            fallback_source_terminal_id=source_id,
            fallback_source_lease_token=token,
        ))
    assert not provider_session_lease_held("fallback-u")
    release_rebind_lease(valid)
    if extra is not None:
        release_rebind_lease(extra)


def test_live_other_owner_conflicts_even_under_unrelated_rebind_lease(monkeypatch):
    source = acquire_rebind_lease("source-valid")
    unrelated = acquire_rebind_lease("other-live")
    assert source is not None and unrelated is not None
    monkeypatch.setattr(
        terminal_service, "list_terminals_by_provider_session_id",
        lambda _u: [
            {"id": "source-valid", "tmux_session": "s", "tmux_window": "source"},
            {"id": "other-live", "tmux_session": "s", "tmux_window": "other"},
        ],
    )
    monkeypatch.setattr(
        terminal_service, "get_terminal_metadata",
        lambda _tid: {"provider_session_id": "fallback-live", "recovery_state": "fallback_starting"},
    )
    backend = MagicMock()
    backend.window_liveness.return_value = "live"
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    context = ForkContext(mode="resume", session_uuid="fallback-live", base_name="b",
                          provider="grok_cli", initial_preamble="")
    with pytest.raises(RuntimeError, match="owner_conflict"):
        asyncio.run(terminal_service.create_terminal(
            "grok_cli", "dev", fork_context=context,
            fallback_source_terminal_id="source-valid",
            fallback_source_lease_token=source,
        ))
    release_rebind_lease(unrelated)
    release_rebind_lease(source)


def test_fallback_stops_before_settlement_with_all_four_effects_absent(real_db, monkeypatch):
    """A crash after replacement create-return preserves the whole D12 boundary."""
    from cli_agent_orchestrator.services import provider_rebind_service

    database.create_terminal(
        "source", "cao-s", "old", "grok_cli", provider_session_id="fallback-crash"
    )
    database.set_terminal_recovery_state("source", "fallback_starting")
    database.create_inbox_message("sender", "source", "pending")
    source_lease = acquire_rebind_lease("source")
    assert source_lease is not None
    lifecycle_lease = acquire_session_lifecycle_shared("cao-s")
    assert lifecycle_lease is not None

    async def create_replacement(**_kwargs):
        database.create_terminal(
            "replacement", "cao-s", "new", "grok_cli",
            provider_session_id="fallback-crash",
        )
        return SimpleNamespace(id="replacement")

    def crash_at_settlement(old_id, new_id):
        source = database.get_terminal_metadata(old_id)
        pending = database.get_pending_messages(old_id, limit=10)
        assert source["provider_session_id"] == "fallback-crash"  # UUID transfer absent
        assert source["fallback_terminal_id"] is None              # pointer absent
        assert source["recovery_state"] == "fallback_starting"    # ready transition absent
        assert [message.receiver_id for message in pending] == [old_id]  # rewrite absent
        raise RuntimeError("crash_before_settle")

    backend = MagicMock()
    backend.get_pane_working_directory.return_value = "/work"
    monkeypatch.setattr(terminal_service, "create_terminal", create_replacement)
    monkeypatch.setattr(provider_rebind_service, "get_backend", lambda: backend)
    monkeypatch.setattr(provider_rebind_service, "settle_terminal_fallback", crash_at_settlement)
    metadata = database.get_terminal_metadata("source")
    with pytest.raises(RuntimeError, match="crash_before_settle"):
        asyncio.run(provider_rebind_service._fallback(
            metadata, "fallback-crash", source_lease, lifecycle_lease
        ))
    release_session_lifecycle_lease(lifecycle_lease)
    release_rebind_lease(source_lease)


def test_fallback_call_graph_never_runs_seed_resume_bootstrap(monkeypatch):
    from cli_agent_orchestrator.services import provider_rebind_service

    seed = MagicMock(side_effect=AssertionError("fallback invoked seed"))
    async def create_replacement(**kwargs):
        assert kwargs["fork_context"].mode == "resume"
        return SimpleNamespace(id="replacement")

    monkeypatch.setattr(terminal_service, "seed_resume_bootstrap", seed)
    monkeypatch.setattr(terminal_service, "create_terminal", create_replacement)
    monkeypatch.setattr(provider_rebind_service, "settle_terminal_fallback", lambda *_a: 0)
    backend = MagicMock()
    backend.get_pane_working_directory.return_value = "/work"
    monkeypatch.setattr(provider_rebind_service, "get_backend", lambda: backend)
    monkeypatch.setattr(provider_rebind_service, "set_terminal_recovery_state", MagicMock())
    metadata = {
        "id": "source", "provider": "grok_cli", "agent_profile": "dev",
        "tmux_session": "cao-s", "tmux_window": "old",
    }
    lifecycle_lease = acquire_session_lifecycle_shared("cao-s")
    assert lifecycle_lease is not None
    result = asyncio.run(provider_rebind_service._fallback(
        metadata, "uuid", object(), lifecycle_lease
    ))
    release_session_lifecycle_lease(lifecycle_lease)
    assert result["status"] == "respawned"
    seed.assert_not_called()


def test_codex_seed_and_interactive_share_resolved_model_config(monkeypatch):
    profile = AgentProfile(
        name="dev", description="", model="profile-model",
        codexConfig={"service_tier": "fast", "features.fast_mode": True},
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.codex.load_agent_profile", lambda _n: profile
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.codex.get_provider_defaults",
        lambda _n: {"model": "default-model", "reasoning_effort": "high"},
    )
    provider = CodexProvider("t", "s", "w", "dev")
    interactive = provider._build_codex_command()
    captured = {}
    completed = SimpleNamespace(
        returncode=0,
        stdout="session id: 12345678-1234-1234-1234-123456789abc\n",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.codex.subprocess.run",
        lambda argv, **kwargs: captured.update(argv=argv, kwargs=kwargs) or completed,
    )
    monkeypatch.setattr(CodexProvider, "validate_session_artifact", lambda *_a: None)
    assert CodexProvider.seed_resume_identity("/work", "dev").startswith("12345678")
    for token in (
        "--model default-model", 'service_tier="fast"',
        "features.fast_mode=true", 'model_reasoning_effort="high"',
    ):
        assert token in interactive
        assert token in " ".join(captured["argv"])
    assert "env" not in captured["kwargs"]  # both inherit the launching process environment


@pytest.mark.parametrize(
    "liveness,quarantined,proceeds",
    [("live", False, False), ("error", False, False),
     ("gone", False, True), ("gone", True, True)],
)
def test_owner_preflight_matrix(monkeypatch, liveness, quarantined, proceeds):
    uuid = f"matrix-{liveness}-{quarantined}"
    monkeypatch.setattr(
        terminal_service, "list_terminals_by_provider_session_id",
        lambda _u: [{"id": "old", "tmux_session": "s", "tmux_window": "w",
                     "recovery_state": "rebind_failed" if quarantined else None}],
    )
    backend = MagicMock()
    backend.window_liveness.return_value = liveness
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service, "generate_terminal_id",
        MagicMock(side_effect=RuntimeError("preflight_passed")),
    )
    context = ForkContext(mode="resume", session_uuid=uuid, base_name="b",
                          provider="grok_cli", initial_preamble="")
    expected = "preflight_passed" if proceeds else "owner_conflict"
    with pytest.raises(RuntimeError, match=expected):
        asyncio.run(terminal_service.create_terminal("grok_cli", "dev", fork_context=context))
    # Pre-publication failure cannot leak the internally acquired UUID lease.
    if provider_session_lease_held(uuid):
        # create failed after preflight but before entering its rollback try;
        # release is part of the assertion target for the production fix below.
        pytest.fail("pre-publication owner lease leaked")


def test_sync_failure_racing_public_teardown_holder_retries_and_releases_uuid(monkeypatch):
    terminal_id, uuid = "sync-race", "sync-race-u"
    public = acquire_rebind_lease(terminal_id)
    assert public is not None
    backend = MagicMock()
    backend.session_exists.return_value = False
    backend.supports_event_inbox.return_value = True
    provider = MagicMock(allocated_session_uuid=None)
    provider.initialize = AsyncMock(side_effect=RuntimeError("init failed"))
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "load_agent_profile", MagicMock(side_effect=FileNotFoundError))
    monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: terminal_id)
    monkeypatch.setattr(terminal_service, "generate_session_name", lambda: "cao-s")
    monkeypatch.setattr(terminal_service, "generate_window_name", lambda _p: "w")
    monkeypatch.setattr(terminal_service, "db_create_terminal", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_service, "list_terminals_by_provider_session_id", lambda _u: [])
    monkeypatch.setattr(terminal_service.provider_manager, "create_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _tid: {
        "id": terminal_id, "provider_session_id": uuid,
    })
    deleted = []
    monkeypatch.setattr(
        terminal_service, "_delete_terminal_under_lease",
        lambda *_a, **_k: deleted.append("settled") or {"terminal_deleted": True},
    )
    release_thread = threading.Thread(
        target=lambda: (time.sleep(0.03), release_rebind_lease(public))
    )
    release_thread.start()
    context = ForkContext(mode="resume", session_uuid=uuid, base_name="b",
                          provider="grok_cli", initial_preamble="")
    with pytest.raises(RuntimeError, match="init failed"):
        asyncio.run(terminal_service.create_terminal(
            "grok_cli", "dev", new_session=True, fork_context=context
        ))
    release_thread.join(1)
    assert deleted == ["settled"]
    assert not provider_session_lease_held(uuid)


@pytest.mark.asyncio
async def test_deferred_failure_racing_session_teardown_holder_notifies_after_settlement(monkeypatch):
    terminal_id, uuid = "deferred-race", "deferred-race-u"
    uuid_token = acquire_provider_session_lease(uuid)
    public = acquire_rebind_lease(terminal_id)
    assert uuid_token is not None and public is not None
    provider = MagicMock()
    provider.initialize = AsyncMock(side_effect=RuntimeError("deferred failed"))
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _tid: {
        "id": terminal_id, "provider_session_id": uuid, "caller_id": "caller",
    })
    deleted, notices = [], []
    monkeypatch.setattr(
        terminal_service, "_delete_terminal_under_lease",
        lambda *_a, **_k: deleted.append("settled") or {"terminal_deleted": True},
    )
    monkeypatch.setattr(
        terminal_service, "_notify_caller_of_deferred_failure",
        lambda _tid, message, _registry, delete_worker: notices.append((message, delete_worker)),
    )
    release_thread = threading.Thread(
        target=lambda: (time.sleep(0.03), release_rebind_lease(public))
    )
    release_thread.start()
    terminal_service._schedule_deferred_init(
        provider, terminal_id, None, None, None,
        uuid_lease_token=uuid_token, owns_uuid_lease=True, settlement_form="resume",
    )
    await asyncio.gather(*list(terminal_service._deferred_init_tasks))
    release_thread.join(1)
    assert deleted == ["settled"]
    assert notices and "has been deleted" in notices[0][0]
    assert notices[0][1] is False
    assert not provider_session_lease_held(uuid)
