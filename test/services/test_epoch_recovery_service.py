import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services import epoch_recovery_service as service
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services import fork_context_service
from cli_agent_orchestrator.services.fork_context_service import SnapshotDelta


@pytest.fixture
def epoch_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'epoch.db'}", connect_args={"check_same_thread": False})
    local = sessionmaker(bind=engine)
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", local)
    return local


def _publish(database, tid, *, base="codex", hook=None):
    return database.create_terminal_with_warm_intent(
        terminal_id=tid, tmux_session="cao-s", tmux_window=f"w-{tid}",
        provider="codex", agent_profile="dev", allowed_tools=None, caller_id=None,
        parent_base_name=base, fork_mode="fork", cas_hook=hook,
    )


def test_d2_session_scope_and_epoch_counter_roundtrip(epoch_db):
    database.register_provider_session(
        name="codex", provider="codex", session_uuid="u", cwd="/tmp",
        agent_profile="dev", dirty_hashes="{}", source_terminal_id="old",
        session_name="cao-s",
    )
    assert [r["name"] for r in database.list_ready_provider_sessions_for_session("cao-s")] == ["codex"]
    assert database.list_ready_provider_sessions_for_session("cao-other") == []
    assert database.increment_session_epoch("cao-s")["count"] == 1
    assert database.increment_session_epoch("cao-s")["count"] == 2
    assert database.get_session_epoch("cao-s")["count"] == 2
    assert database.delete_session_epoch("cao-s") is True
    assert database.get_session_epoch("cao-s") is None
    assert database.increment_session_epoch("cao-s")["count"] == 1


def test_d9_atomic_epoch_increment_eight_threads(epoch_db):
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda _: database.increment_session_epoch("cao-race")["count"], range(8)))
    assert sorted(counts) == list(range(1, 9))
    assert database.get_session_epoch("cao-race")["count"] == 8


def test_d3_distinct_live_forks_and_oldest_dead_consume(epoch_db):
    _publish(database, "live-a")
    _publish(database, "live-b")
    assert len(database.list_warm_intents("cao-s")) == 2
    database.delete_terminal_and_warm_intent("live-a", preserve_warm_intent=True)
    old = next(r for r in database.list_warm_intents("cao-s") if r["worker_terminal_id"] == "live-a")
    _publish(database, "new-a")
    rows = database.list_warm_intents("cao-s")
    consumed = next(r for r in rows if r["intent_id"] == old["intent_id"])
    assert consumed["worker_terminal_id"] == "new-a"
    assert consumed["replaces_worker_terminal_id"] == "live-a"
    assert any(r["worker_terminal_id"] == "live-b" for r in rows)


def test_d3_oldest_dead_intent_ordering_and_cas_predicates(epoch_db):
    _publish(database, "dead-a")
    _publish(database, "dead-b")
    database.delete_terminal_and_warm_intent("dead-a", preserve_warm_intent=True)
    database.delete_terminal_and_warm_intent("dead-b", preserve_warm_intent=True)
    rows = database.list_warm_intents("cao-s")
    assert len(rows) == 2
    oldest = rows[0]
    _publish(database, "replacement")
    after = database.list_warm_intents("cao-s")
    replaced = next(row for row in after if row["worker_terminal_id"] == "replacement")
    assert replaced["intent_id"] == oldest["intent_id"]
    assert replaced["replaces_worker_terminal_id"] == oldest["worker_terminal_id"]
    assert len(after) == 2


def test_d2_unscoped_default_exclusion_and_mark_ready_rescope(epoch_db, monkeypatch):
    database.register_provider_session(
        name="base", provider="grok_cli", session_uuid="u", cwd="/tmp",
        agent_profile="dev", dirty_hashes="{}", source_terminal_id="old",
        session_name=None,
    )
    assert database.list_ready_provider_sessions_for_session("cao-s") == []
    database.create_terminal(
        "joined", "cao-s", "w", "grok_cli", "dev", provider_session_id="u",
    )
    monkeypatch.setattr(fork_context_service, "snapshot", lambda _: SnapshotDelta("sha"))
    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend",
        lambda: SimpleNamespace(get_pane_working_directory=lambda *_: "/tmp"),
    )
    row = fork_context_service.mark_ready("joined", "base", None)
    assert row["session_name"] == "cao-s"
    assert [r["name"] for r in database.list_ready_provider_sessions_for_session("cao-s")] == ["base"]


def test_d3_cas_barrier_miss_recomputes_and_exhaustion_rolls_back(epoch_db):
    _publish(database, "dead")
    database.delete_terminal_and_warm_intent("dead", preserve_warm_intent=True)
    attempts = []
    _publish(database, "winner", hook=lambda attempt, _old, _db: attempts.append(attempt) or attempt > 0)
    assert attempts == [0, 1]
    database.delete_terminal_and_warm_intent("winner", preserve_warm_intent=True)
    with pytest.raises(database.WarmIntentPublishError, match="db_publish_failed"):
        _publish(database, "rolled-back", hook=lambda *_: False)
    assert database.get_terminal_metadata("rolled-back") is None
    assert all(r["worker_terminal_id"] != "rolled-back" for r in database.list_warm_intents("cao-s"))


def test_d3_cas_old_id_predicate_preserves_concurrent_replacement(epoch_db):
    _publish(database, "dead")
    database.delete_terminal_and_warm_intent("dead", preserve_warm_intent=True)
    def replace_once(attempt, _old, db):
        if attempt:
            return True
        row = db.query(database.WarmIntentModel).filter_by(worker_terminal_id="dead").one()
        row.worker_terminal_id = "winner"
        db.add(database.TerminalModel(
            id="winner", tmux_session="cao-s", tmux_window="w-winner",
            provider="codex", agent_profile="dev",
        ))
        return True
    _publish(database, "new", hook=replace_once)
    workers = {row["worker_terminal_id"] for row in database.list_warm_intents("cao-s")}
    assert workers == {"winner", "new"}


def test_d3_cas_no_live_predicate_preserves_resurrected_owner(epoch_db):
    _publish(database, "dead")
    database.delete_terminal_and_warm_intent("dead", preserve_warm_intent=True)
    def resurrect_once(attempt, old, db):
        if attempt:
            return True
        db.add(database.TerminalModel(
            id=old, tmux_session="cao-s", tmux_window="w-live",
            provider="codex", agent_profile="dev",
        ))
        return True
    _publish(database, "new", hook=resurrect_once)
    workers = {row["worker_terminal_id"] for row in database.list_warm_intents("cao-s")}
    assert workers == {"dead", "new"}


@pytest.mark.parametrize("raw,code", [
    (RuntimeError("window_create_failed"), "window_create_failed"),
    (RuntimeError("fifo_create_failed"), "fifo_create_failed"),
    (RuntimeError("db_publish_failed"), "db_publish_failed"),
    (RuntimeError("context_build_failed"), "context_build_failed"),
    (RuntimeError("provider_construct_failed"), "provider_construct_failed"),
    (TimeoutError("late"), "initialize_timeout"),
    (RuntimeError("trust failed"), "initialize_failed"),
    (RuntimeError("session_capture_ambiguous"), "session_capture_ambiguous"),
    (RuntimeError("session_capture_mismatch"), "session_capture_mismatch"),
    (RuntimeError("artifact_invalid"), "artifact_invalid"),
    (RuntimeError("shell_baseline_unavailable"), "identity_persist_failed"),
    (RuntimeError("herdr_register_failed"), "herdr_register_failed"),
    (RuntimeError("rollback_kill_uncertain"), "rollback_kill_uncertain"),
    (RuntimeError("quarantine_persist_failed"), "quarantine_persist_failed"),
])
def test_d5_exception_normalization_is_closed(raw, code):
    assert service._normalize_creation_error(raw) == code


@pytest.mark.parametrize("status,error,retry", [
    ("not_found", None, False), ("not_ready", "retired", False),
    ("not_ready", "superseded", False), ("wrong_session", None, False),
    ("skipped_live_owner", None, False), ("artifact_missing", None, False),
    ("profile_unresolvable", "provider_mismatch", False),
    ("profile_unresolvable", "provider_lacks_fork_capability", False),
    ("profile_unresolvable", "profile_load_failed", False),
    ("resume_failed", "initialize_failed", True),
    ("resume_failed", "rollback_kill_uncertain", False),
    ("resume_failed", "quarantine_persist_failed", False),
    ("skipped_busy", "rebind_in_progress", True),
    ("resumed", None, False), ("resumed", "remark_failed", False),
])
def test_d5_result_vocabulary(status, error, retry):
    row = service._result("b", status, error_code=error)
    assert row == {"base": "b", "status": status, "terminal_id": None,
                   "error_code": error, "retryable": retry}


@pytest.mark.asyncio
async def test_d2b_real_creation_seam_is_synchronous_leased_and_strict(monkeypatch):
    row = {"name": "b", "provider": "codex", "session_uuid": "u", "cwd": "/w",
           "agent_profile": "dev", "session_name": "cao-s", "summary": None}
    monkeypatch.setattr(service, "get_backend", lambda: SimpleNamespace(session_exists=lambda _: True))
    monkeypatch.setattr(service, "list_ready_provider_sessions_for_session", lambda _: [row])
    monkeypatch.setattr(service, "_artifact_exists", lambda _: True)
    monkeypatch.setattr(service, "provider_session_owner", lambda _: {"state": "gone"})
    monkeypatch.setattr(service, "load_agent_profile", lambda _: SimpleNamespace())
    monkeypatch.setattr(service, "resolve_provider", lambda *_: "codex")
    monkeypatch.setattr(service, "get_provider_class", lambda _: SimpleNamespace(supports_fork_context=True))
    token = SimpleNamespace(terminal_id="new")
    monkeypatch.setattr(service, "generate_terminal_id", lambda: "new")
    monkeypatch.setattr(service, "acquire_rebind_lease", lambda _: token)
    monkeypatch.setattr(service, "release_rebind_lease", lambda _: None)
    seen = {}
    async def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(id="new")
    monkeypatch.setattr(service, "create_terminal", create)
    monkeypatch.setattr(service, "mark_ready", lambda *_: None)
    monkeypatch.setattr(service, "staleness", lambda _: SimpleNamespace(changed_count=0))
    monkeypatch.setattr(service, "increment_session_epoch", lambda _: {"count": 1})
    monkeypatch.setattr(service, "list_warm_intents", lambda _: [])
    monkeypatch.setattr(service, "build_session_manifest", lambda _: {}, raising=False)
    result = await service.recover_epoch("cao-s")
    assert result["results"][0]["status"] == "resumed"
    assert seen["defer_init"] is False
    assert seen["new_session"] is False
    assert seen["terminal_id"] == "new" and seen["lease_token"] is token
    assert seen["strict_backend_registration"] is True


@pytest.mark.asyncio
async def test_e3_epoch_recovery_preserves_anchor_kind_and_unforkable(
    epoch_db, monkeypatch,
):
    anchor = database.register_provider_session(
        name="root-anchor", provider="grok_cli", session_uuid="anchor-uuid",
        cwd="/repo", agent_profile="dev", dirty_hashes="{}", kind="anchor",
        summary="anchor context", source_terminal_id="old-anchor",
        session_name="cao-s",
    )
    monkeypatch.setattr(service, "_preflight", lambda *_: None)
    monkeypatch.setattr(service, "generate_terminal_id", lambda: "recovered-anchor")
    lease = SimpleNamespace(terminal_id="recovered-anchor")
    monkeypatch.setattr(service, "acquire_rebind_lease", lambda _: lease)
    monkeypatch.setattr(service, "release_rebind_lease", lambda _: None)

    from cli_agent_orchestrator.services import provider_session_lease
    from cli_agent_orchestrator.services import session_lifecycle_lease

    monkeypatch.setattr(
        provider_session_lease, "acquire_provider_session_lease", lambda _: lease,
    )
    monkeypatch.setattr(provider_session_lease, "release_provider_session_lease", lambda _: None)
    monkeypatch.setattr(
        session_lifecycle_lease, "acquire_session_lifecycle_shared", lambda _: lease,
    )
    monkeypatch.setattr(session_lifecycle_lease, "release_session_lifecycle_lease", lambda _: None)

    async def create(**_kwargs):
        return SimpleNamespace(id="recovered-anchor")

    def remark(terminal_id, name, summary, kind="base"):
        return database.register_provider_session(
            name=name, provider=anchor["provider"], session_uuid=anchor["session_uuid"],
            cwd=anchor["cwd"], agent_profile=anchor["agent_profile"],
            git_sha=anchor["git_sha"], dirty_hashes=anchor["dirty_hashes"],
            kind=kind, summary=summary, source_terminal_id=terminal_id,
            session_name="cao-s",
        )

    monkeypatch.setattr(service, "create_terminal", create)
    monkeypatch.setattr(service, "mark_ready", remark)
    monkeypatch.setattr(service, "staleness", lambda _: SimpleNamespace(changed_count=0))

    result, _source = await service._recover_row(anchor, "cao-s")
    recovered = database.get_ready_provider_session("root-anchor")

    assert result["status"] == "resumed"
    assert recovered["kind"] == "anchor"
    with pytest.raises(
        fork_context_service.ForkContextError,
        match="anchor_not_forkable:root-anchor",
    ):
        fork_context_service.resolve_base("root-anchor")


def test_d10_non_goals_are_absent_from_epoch_service():
    text = Path(service.__file__).read_text()
    forbidden = ["quota banner", "auto-trigger", "usage-reset", "approval automation",
                 "get_raw_status", "receiver_id =", "fallback_terminal_id"]
    assert not [needle for needle in forbidden if needle in text]
    assert text.count("create_terminal(") == 1


@pytest.mark.parametrize("mutation,expected,error", [
    ({"session_name": "other"}, "wrong_session", None),
    ({"artifact": False}, "artifact_missing", None),
    ({"owner": "live"}, "skipped_live_owner", None),
    ({"resolved": "grok_cli"}, "profile_unresolvable", "provider_mismatch"),
    ({"supports": False}, "profile_unresolvable", "provider_lacks_fork_capability"),
])
def test_epoch_failure_matrix_preflight(monkeypatch, mutation, expected, error):
    row = {"name": "b", "provider": "codex", "session_uuid": "u", "cwd": "/w",
           "agent_profile": "dev", "session_name": "cao-s"}
    row.update({k: v for k, v in mutation.items() if k == "session_name"})
    monkeypatch.setattr(service, "_artifact_exists", lambda _: mutation.get("artifact", True))
    monkeypatch.setattr(service, "provider_session_owner",
                        lambda _: {"state": mutation.get("owner", "gone")})
    monkeypatch.setattr(service, "resolve_provider",
                        lambda *_: mutation.get("resolved", "codex"))
    monkeypatch.setattr(service, "load_agent_profile", lambda _: SimpleNamespace())
    monkeypatch.setattr(service, "get_provider_class",
                        lambda _: SimpleNamespace(supports_fork_context=mutation.get("supports", True)))
    result = service._preflight(row, "cao-s")
    assert (result["status"], result["error_code"], result["retryable"]) == (expected, error, False)


@pytest.mark.parametrize("failure", [FileNotFoundError("missing"), RuntimeError("unparseable")])
def test_profile_load_failure_is_zero_effect_preflight(monkeypatch, failure):
    row = {"name": "b", "provider": "codex", "session_uuid": "u", "cwd": "/w",
           "agent_profile": "bad", "session_name": "cao-s"}
    monkeypatch.setattr(service, "_artifact_exists", lambda _: True)
    monkeypatch.setattr(service, "provider_session_owner", lambda _: {"state": "gone"})
    monkeypatch.setattr(service, "load_agent_profile", lambda _: (_ for _ in ()).throw(failure))
    result = service._preflight(row, "cao-s")
    assert result["status"] == "profile_unresolvable"
    assert result["error_code"] == "profile_load_failed"
    assert result["terminal_id"] is None and result["retryable"] is False


@pytest.mark.asyncio
async def test_explicit_not_found_and_historical_not_ready_are_reporting_only(monkeypatch):
    monkeypatch.setattr(service, "get_backend", lambda: SimpleNamespace(session_exists=lambda _: True))
    monkeypatch.setattr(service, "get_ready_provider_session", lambda _: None)
    monkeypatch.setattr(service, "get_provider_session_history",
                        lambda name: None if name == "absent" else {"status": "retired"})
    monkeypatch.setattr(service, "list_warm_intents", lambda _: [])
    result = await service.recover_epoch("cao-s", ["retired", "absent"])
    assert [(r["base"], r["status"], r["error_code"]) for r in result["results"]] == [
        ("absent", "not_found", None), ("retired", "not_ready", "retired")]


@pytest.mark.asyncio
async def test_session_missing_is_closed_before_selection(monkeypatch):
    monkeypatch.setattr(service, "get_backend", lambda: SimpleNamespace(session_exists=lambda _: False))
    with pytest.raises(ValueError, match="session_missing"):
        await service.recover_epoch("cao-missing")


@pytest.mark.asyncio
async def test_lease_conflict_and_remark_failure_matrix(monkeypatch):
    row = {"name": "b", "provider": "codex", "session_uuid": "u", "cwd": "/w",
           "agent_profile": "dev", "session_name": "cao-s", "summary": None}
    monkeypatch.setattr(service, "get_backend", lambda: SimpleNamespace(session_exists=lambda _: True))
    monkeypatch.setattr(service, "list_ready_provider_sessions_for_session", lambda _: [row])
    monkeypatch.setattr(service, "_preflight", lambda *_: None)
    monkeypatch.setattr(service, "generate_terminal_id", lambda: "new")
    monkeypatch.setattr(service, "acquire_rebind_lease", lambda _: None)
    monkeypatch.setattr(service, "list_warm_intents", lambda _: [])
    skipped = await service.recover_epoch("cao-s")
    assert skipped["results"][0]["status"] == "skipped_busy"

    token = SimpleNamespace(terminal_id="new")
    monkeypatch.setattr(service, "acquire_rebind_lease", lambda _: token)
    monkeypatch.setattr(service, "release_rebind_lease", lambda _: None)
    monkeypatch.setattr(service, "create_terminal", lambda **_: asyncio.sleep(0, result=SimpleNamespace(id="new")))
    monkeypatch.setattr(service, "mark_ready", lambda *_: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(service, "staleness", lambda _: SimpleNamespace(changed_count=0))
    monkeypatch.setattr(service, "increment_session_epoch", lambda _: {"count": 1})
    resumed = await service.recover_epoch("cao-s")
    assert resumed["results"][0]["status"] == "resumed"
    assert resumed["results"][0]["error_code"] == "remark_failed"
    assert resumed["epoch"] == {"count": 1}


@pytest.mark.asyncio
async def test_concurrent_duplicate_recovery_creates_exactly_one_terminal(monkeypatch):
    row = {"name": "b-race", "provider": "codex", "session_uuid": "u", "cwd": "/w",
           "agent_profile": "dev", "session_name": "cao-race", "summary": None}
    monkeypatch.setattr(service, "get_backend", lambda: SimpleNamespace(session_exists=lambda _: True))
    monkeypatch.setattr(service, "list_ready_provider_sessions_for_session", lambda _: [row])
    monkeypatch.setattr(service, "_preflight", lambda *_: None)
    monkeypatch.setattr(service, "generate_terminal_id", lambda: "new-race")
    token = SimpleNamespace(terminal_id="new-race")
    monkeypatch.setattr(service, "acquire_rebind_lease", lambda _: token)
    monkeypatch.setattr(service, "release_rebind_lease", lambda _: None)
    started = asyncio.Event()
    finish = asyncio.Event()
    creations = []
    async def create(**_):
        creations.append("new-race")
        started.set()
        await finish.wait()
        return SimpleNamespace(id="new-race")
    monkeypatch.setattr(service, "create_terminal", create)
    monkeypatch.setattr(service, "mark_ready", lambda *_: None)
    monkeypatch.setattr(service, "staleness", lambda _: SimpleNamespace(changed_count=0))
    monkeypatch.setattr(service, "increment_session_epoch", lambda _: {"count": 1})
    monkeypatch.setattr(service, "list_warm_intents", lambda _: [])
    first = asyncio.create_task(service.recover_epoch("cao-race"))
    await started.wait()
    second = await service.recover_epoch("cao-race")
    finish.set()
    first_result = await first
    assert first_result["results"][0]["status"] == "resumed"
    assert second["results"][0] == {
        "base": "b-race", "status": "skipped_busy", "terminal_id": None,
        "error_code": "rebind_in_progress", "retryable": True,
    }
    assert creations == ["new-race"]


@pytest.mark.parametrize("liveness,deleted,uncertain", [
    ("live", False, True),
    ("gone", True, False),
])
def test_addendum_rollback_requires_confirmed_death(
    monkeypatch, tmp_path, liveness, deleted, uncertain,
):
    metadata = {
        "tmux_session": "cao-s", "tmux_window": "w", "provider": "codex",
        "agent_profile": "dev", "allowed_tools": None, "caller_id": None,
    }
    backend = SimpleNamespace(
        get_history=lambda *a, **k: "", get_pane_working_directory=lambda *a: "/w",
        stop_pipe_pane=lambda *a: None, kill_window=lambda *a: None,
        window_liveness=lambda *a: liveness,
    )
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _: metadata)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    detached = []
    monkeypatch.setattr(terminal_service.fifo_manager, "stop_reader", lambda _: detached.append("fifo"))
    monkeypatch.setattr(terminal_service.status_monitor, "clear_terminal", lambda _: detached.append("status"))
    monkeypatch.setattr(terminal_service.provider_manager, "cleanup_provider", lambda _: None)
    monkeypatch.setattr(
        terminal_service, "delete_terminal_and_warm_intent",
        lambda *_a, **_k: {"terminal_deleted": True, "intent_deleted": False},
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease",
        lambda *_: None,
    )
    quarantined = []
    monkeypatch.setattr(database, "quarantine_terminal_owner",
                        lambda *args: quarantined.append(args) or True)
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", tmp_path)
    result = terminal_service._delete_terminal_under_lease(
        "t", SimpleNamespace(), require_confirmed_death=True,
    )
    assert result["terminal_deleted"] is deleted
    assert result["rollback_kill_uncertain"] is uncertain
    if uncertain:
        assert detached == []
        assert quarantined == [("t", None, "rollback_kill_uncertain")]
    else:
        assert detached == ["fifo", "status"]
        assert quarantined == []


@pytest.mark.asyncio
async def test_addendum_uncertain_rollback_is_nonretryable_epoch_result(monkeypatch):
    row = {"name": "b", "provider": "codex", "session_uuid": "u", "cwd": "/w",
           "agent_profile": "dev", "session_name": "cao-s", "summary": None}
    monkeypatch.setattr(service, "_preflight", lambda *_: None)
    monkeypatch.setattr(service, "generate_terminal_id", lambda: "new")
    token = SimpleNamespace(terminal_id="new")
    monkeypatch.setattr(service, "acquire_rebind_lease", lambda _: token)
    monkeypatch.setattr(service, "release_rebind_lease", lambda _: None)
    async def fail(**_):
        raise RuntimeError("rollback_kill_uncertain")
    monkeypatch.setattr(service, "create_terminal", fail)
    result, source = await service._recover_row(row, "cao-s")
    assert source is None
    assert result == {
        "base": "b", "status": "resume_failed", "terminal_id": "new",
        "error_code": "rollback_kill_uncertain", "retryable": False,
    }
