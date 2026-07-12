from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import session_close_service as service


class Backend:
    def __init__(self, alive=True, kill_fails=False):
        self.alive = alive
        self.kill_fails = kill_fails
    def session_exists(self, _name):
        return self.alive
    def kill_session(self, _name):
        if self.kill_fails:
            raise RuntimeError("kill")
        self.alive = False


def _install(monkeypatch, *, terminals=None, registrations=None, intents=None,
             deleted=True, keep_rows_after=False, backend=None):
    terminals = list(terminals or [])
    registrations = list(registrations or [])
    intents = list(intents or [])
    terminal_reads = {"count": 0}
    def list_rows(_):
        terminal_reads["count"] += 1
        return list(terminals) if keep_rows_after or terminal_reads["count"] == 1 else []
    monkeypatch.setattr(service, "list_terminals_by_session", list_rows)
    monkeypatch.setattr(service, "list_ready_provider_sessions_for_session", lambda _: registrations)
    monkeypatch.setattr(service, "get_ready_provider_session_by_source_terminal", lambda _: None)
    monkeypatch.setattr(service, "list_warm_intents", lambda _: intents)
    monkeypatch.setattr(service, "load_agent_profile", lambda _: SimpleNamespace(protected=False))
    monkeypatch.setattr(service, "acquire_rebind_lease",
                        lambda tid: SimpleNamespace(terminal_id=tid))
    monkeypatch.setattr(service, "release_rebind_lease", lambda _: None)
    monkeypatch.setattr(service, "get_backend", lambda: backend or Backend())
    monkeypatch.setattr(service, "get_terminal_metadata", lambda tid: next(
        (row for row in terminals if row["id"] == tid), None))
    monkeypatch.setattr(service, "retire_provider_session", lambda _: {"status": "retired"})
    monkeypatch.setattr(service, "delete_warm_intents_for_session", lambda _: len(intents))
    monkeypatch.setattr(service, "delete_session_epoch", lambda _: True)
    from cli_agent_orchestrator.services import terminal_service
    monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease", lambda *a, **kw: {
        "terminal_deleted": deleted, "intent_deleted": deleted and not kw.get("preserve_warm_intent"),
        "intent_error": None, "intent_retain_reason": "keep_bases" if kw.get("preserve_warm_intent") else None,
    })


def test_d6_ready_base_guard_override_but_profile_protection_remains(monkeypatch):
    terminal = {"id": "t", "agent_profile": "base", "tmux_session": "cao-s"}
    _install(monkeypatch, terminals=[terminal], registrations=[
        {"name": "scoped", "source_terminal_id": "t", "session_name": "cao-s"}
    ])
    monkeypatch.setattr(service, "get_ready_provider_session_by_source_terminal",
                        lambda _: {"name": "scoped", "session_name": "cao-s"})
    monkeypatch.setattr(service, "load_agent_profile", lambda _: SimpleNamespace(name="base", protected=True))
    with pytest.raises(PermissionError):
        service.close_session("cao-s")


def test_d6_unscoped_ready_base_owner_is_not_overridden(monkeypatch):
    terminal = {"id": "t", "agent_profile": "base", "tmux_session": "cao-s"}
    _install(monkeypatch, terminals=[terminal])
    monkeypatch.setattr(service, "get_ready_provider_session_by_source_terminal",
                        lambda _: {"name": "unscoped"})
    with pytest.raises(PermissionError, match="not scoped"):
        service.close_session("cao-s")


@pytest.mark.parametrize("keep,source_present,deleted,expected", [
    (False, True, True, "retired"),
    (True, True, True, "kept"),
    (False, True, False, "source_not_deleted"),
    (False, False, True, "source_missing"),
    (True, False, True, "kept"),
])
def test_d7_registration_settlement_rows(monkeypatch, keep, source_present, deleted, expected):
    terminal = {"id": "t", "agent_profile": "base", "tmux_session": "cao-s"}
    _install(monkeypatch, terminals=[terminal] if source_present else [], deleted=deleted,
             registrations=[{"name": "b", "source_terminal_id": "t", "session_name": "cao-s"}])
    result = service.close_session("cao-s", keep_bases=keep)
    assert result["bases"] == [{"base": "b", "status": expected}]


def test_d7_other_session_source_is_skipped(monkeypatch):
    other = {"id": "t", "agent_profile": "base", "tmux_session": "cao-other"}
    _install(monkeypatch, terminals=[], registrations=[
        {"name": "b", "source_terminal_id": "t", "session_name": "cao-s"}])
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _: other)
    result = service.close_session("cao-s")
    assert result["bases"][0]["status"] == "skipped_other_session"


def test_d7_source_snapshot_survives_concurrent_other_session_delete(monkeypatch):
    other = {"id": "t", "agent_profile": "base", "tmux_session": "cao-other"}
    _install(monkeypatch, terminals=[], registrations=[
        {"name": "b", "source_terminal_id": "t", "session_name": "cao-s"}])
    reads = []
    def getter(_):
        reads.append(True)
        return other if len(reads) == 1 else None
    monkeypatch.setattr(service, "get_terminal_metadata", getter)
    result = service.close_session("cao-s")
    assert result["bases"][0]["status"] == "skipped_other_session"
    assert len(reads) == 1


def test_d7_retire_failure_is_reported_without_rollback(monkeypatch):
    terminal = {"id": "t", "agent_profile": "base", "tmux_session": "cao-s"}
    _install(monkeypatch, terminals=[terminal], registrations=[
        {"name": "b", "source_terminal_id": "t", "session_name": "cao-s"}])
    monkeypatch.setattr(service, "retire_provider_session", lambda _: (_ for _ in ()).throw(RuntimeError()))
    result = service.close_session("cao-s")
    assert result["terminals"][0]["status"] == "deleted"
    assert result["bases"][0]["status"] == "retire_failed"


def test_d8_keep_bases_retains_every_intent(monkeypatch):
    terminal = {"id": "t", "agent_profile": "dev", "tmux_session": "cao-s"}
    intents = [{"intent_id": "i", "worker_terminal_id": "t"}]
    _install(monkeypatch, terminals=[terminal], intents=intents)
    epochs = []
    monkeypatch.setattr(service, "delete_session_epoch", lambda _: epochs.append("deleted"))
    result = service.close_session("cao-s", keep_bases=True)
    assert result["session_closed"] is True
    assert result["intents"] == {"removed": 0, "retained": 1, "errors": []}
    assert epochs == ["deleted"]


def test_d8_failed_session_kill_retains_intents_and_epoch(monkeypatch):
    terminal = {"id": "t", "agent_profile": "dev", "tmux_session": "cao-s"}
    _install(monkeypatch, terminals=[terminal], intents=[{"intent_id": "i"}],
             backend=Backend(kill_fails=True), keep_rows_after=True)
    epoch = []
    monkeypatch.setattr(service, "delete_session_epoch", lambda _: epoch.append(True))
    result = service.close_session("cao-s")
    assert result["session_closed"] is False
    assert result["intents"]["retained"] == 1
    assert epoch == []


def test_close_lease_conflict_has_zero_teardown(monkeypatch):
    terminals = [{"id": "a", "agent_profile": "dev", "tmux_session": "cao-s"},
                 {"id": "b", "agent_profile": "dev", "tmux_session": "cao-s"}]
    _install(monkeypatch, terminals=terminals)
    acquired = []
    def acquire(tid):
        acquired.append(tid)
        return None if tid == "b" else SimpleNamespace(terminal_id=tid)
    monkeypatch.setattr(service, "acquire_rebind_lease", acquire)
    with pytest.raises(RuntimeError, match="rebind_in_progress"):
        service.close_session("cao-s")
    assert acquired == ["a", "b"]
