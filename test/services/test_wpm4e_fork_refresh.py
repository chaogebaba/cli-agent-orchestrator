"""WPM4-E E1 refresh ordering, fallback, and coalescing acceptance."""

import asyncio
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import ForkContext
from cli_agent_orchestrator.services import terminal_service as terminals


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


@pytest.mark.asyncio
async def test_e1_planted_token_refresh_a_b_precedes_preamble_bake(monkeypatch):
    memory = {"token": "OLD-TOKEN"}
    calls = []

    async def refresh(*_args):
        calls.append("refresh")
        memory["token"] = "NEW-TOKEN"
        return "[FRESH]"

    monkeypatch.setattr(terminals, "_prepare_fork_refresh", refresh)
    context = ForkContext(
        mode="fork", session_uuid="uuid", base_name="base", provider="codex",
        initial_preamble="[STALE]",
    )

    cold_message = await terminals._prepare_fork_message(
        "worker-a", "g-a", "quote token", context, None, None, {}
    )
    cold_answer = memory["token"]
    memory["token"] = "OLD-TOKEN"
    refreshed_message = await terminals._prepare_fork_message(
        "worker-b", "g-b", "quote token", context, "base", None, {}
    )
    refreshed_answer = memory["token"]

    assert cold_answer == "OLD-TOKEN"
    assert refreshed_answer == "NEW-TOKEN"
    assert cold_message.startswith("[STALE]")
    assert refreshed_message.startswith("[FRESH]")
    assert calls == ["refresh"]


@pytest.mark.asyncio
async def test_e1_real_git_planted_token_refresh_resets_row_fresh(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    token_file = repo / "token.txt"
    token_file.write_text("OLD-TOKEN\n", encoding="utf-8")
    _git(repo, "add", "token.txt")
    _git(repo, "commit", "-qm", "base")
    sha, hashes = terminals.fork_snapshot(str(repo))
    row = {
        "id": 9, "name": "base", "kind": "base", "source_terminal_id": "base-term",
        "session_uuid": "uuid", "cwd": str(repo), "git_sha": sha,
        "dirty_hashes": hashes,
    }
    inherited = {"token": token_file.read_text(encoding="utf-8").strip()}
    cold_answer = inherited["token"]
    token_file.write_text("NEW-TOKEN\n", encoding="utf-8")

    async def inline(_terminal, _generation, _kind, _operation, function, *args,
                     deadline=None, **kwargs):
        return function(*args, **kwargs), time.monotonic()

    async def ready(*_args, **_kwargs):
        return True

    def dispatch(_terminal_id, prompt, **_kwargs):
        assert "token.txt" in prompt
        inherited["token"] = token_file.read_text(encoding="utf-8").strip()
        return True

    def update(_row_id, *, git_sha, dirty_hashes):
        row.update(git_sha=git_sha, dirty_hashes=dirty_hashes)
        return dict(row)

    terminals._fork_refresh_locks.clear()
    monkeypatch.setattr(terminals, "_tracked_blocking", inline)
    monkeypatch.setattr(terminals, "get_ready_provider_session", lambda _name: dict(row))
    monkeypatch.setattr(terminals, "update_provider_session_snapshot", update)
    monkeypatch.setattr(terminals, "_dispatch_base_refresh", dispatch)
    monkeypatch.setattr(terminals, "_wait_for_base_ready", ready)
    monkeypatch.setattr(terminals.status_monitor, "get_input_gen", lambda _id: 1)

    preamble = await terminals._prepare_fork_refresh(
        "worker", "g", "base", "[STALE]", None, {}
    )
    refreshed_answer = inherited["token"]

    assert cold_answer == "OLD-TOKEN"
    assert refreshed_answer == "NEW-TOKEN"
    assert preamble.startswith("[FRESH]")
    assert terminals.fork_staleness(row)[0] == []


@pytest.mark.asyncio
async def test_e1_two_concurrent_stale_forks_dispatch_once_and_reset_baseline(monkeypatch):
    terminals._fork_refresh_locks.clear()
    state = {"fresh": False, "dispatches": 0, "writes": 0}
    row = {
        "id": 7, "name": "base", "kind": "base", "source_terminal_id": "base-term",
        "session_uuid": "uuid", "cwd": "/repo", "git_sha": "old",
        "dirty_hashes": "{}",
    }

    async def inline(_terminal, _generation, _kind, operation, function, *args,
                     deadline=None, **kwargs):
        if operation == "fork_refresh_send":
            await asyncio.sleep(0.02)
        return function(*args, **kwargs), time.monotonic()

    def compare(_row):
        return ([], "[FRESH]") if state["fresh"] else (["token.txt"], "[STALE]")

    def dispatch(*_args, **_kwargs):
        state["dispatches"] += 1
        return True

    def update(_row_id, **_kwargs):
        state["writes"] += 1
        state["fresh"] = True
        return dict(row, git_sha="new")

    async def ready(*_args, **_kwargs):
        return True

    monkeypatch.setattr(terminals, "_tracked_blocking", inline)
    monkeypatch.setattr(terminals, "get_ready_provider_session", lambda _name: dict(row))
    monkeypatch.setattr(terminals, "fork_staleness", compare)
    monkeypatch.setattr(terminals, "fork_snapshot", lambda _cwd: ("new", "{}"))
    monkeypatch.setattr(terminals, "update_provider_session_snapshot", update)
    monkeypatch.setattr(terminals, "_dispatch_base_refresh", dispatch)
    monkeypatch.setattr(terminals, "_wait_for_base_ready", ready)
    monkeypatch.setattr(terminals.status_monitor, "get_input_gen", lambda _id: 1)

    first, second = await asyncio.gather(
        terminals._prepare_fork_refresh("worker-1", "g1", "base", "[STALE]", None, {}),
        terminals._prepare_fork_refresh("worker-2", "g2", "base", "[STALE]", None, {}),
    )
    third = await terminals._prepare_fork_refresh(
        "worker-3", "g3", "base", "[STALE]", None, {}
    )

    assert (first, second, third) == ("[FRESH]", "[FRESH]", "[FRESH]")
    assert state == {"fresh": True, "dispatches": 1, "writes": 1}


@pytest.mark.asyncio
async def test_e1_false_refresh_dispatch_preserves_baseline_and_falls_back_stale(
    monkeypatch,
):
    terminals._fork_refresh_locks.clear()
    row = {
        "id": 7, "name": "base", "kind": "base", "source_terminal_id": "base-term",
        "session_uuid": "uuid", "cwd": "/repo", "git_sha": "old",
        "dirty_hashes": '{"old.py":"hash"}',
    }
    writes = []

    async def inline(_terminal, _generation, _kind, _operation, function, *args,
                     deadline=None, **kwargs):
        return function(*args, **kwargs), time.monotonic()

    async def ready(*_args, **_kwargs):
        return True

    monkeypatch.setattr(terminals, "_tracked_blocking", inline)
    monkeypatch.setattr(terminals, "get_ready_provider_session", lambda _name: dict(row))
    monkeypatch.setattr(terminals, "fork_staleness", lambda _row: (["new.py"], "[STALE]"))
    monkeypatch.setattr(terminals, "_wait_for_base_ready", ready)
    monkeypatch.setattr(terminals, "_dispatch_base_refresh", lambda *_a, **_k: False)
    monkeypatch.setattr(terminals, "fork_snapshot", lambda _cwd: ("new", "{}"))
    monkeypatch.setattr(
        terminals,
        "update_provider_session_snapshot",
        lambda *_a, **_k: writes.append(True),
    )

    preamble = await terminals._prepare_fork_refresh(
        "worker", "g", "base", "[STALE]", None, {}
    )

    assert preamble == "[STALE]"
    assert (row["git_sha"], row["dirty_hashes"]) == (
        "old", '{"old.py":"hash"}',
    )
    assert writes == []


@pytest.mark.asyncio
async def test_e1_busy_refresh_timeout_falls_back_stale_within_budget(monkeypatch):
    terminals._fork_refresh_locks.clear()
    row = {
        "id": 7, "name": "base", "kind": "base", "source_terminal_id": "base-term",
        "session_uuid": "uuid", "cwd": "/repo",
    }

    async def inline(_terminal, _generation, _kind, _operation, function, *args,
                     deadline=None, **kwargs):
        return function(*args, **kwargs), time.monotonic()

    async def busy(_terminal_id, deadline, **_kwargs):
        await asyncio.sleep(max(0.0, deadline - time.monotonic()))
        return False

    dispatches = []
    monkeypatch.setattr(terminals, "FORK_REFRESH_WAIT_BUDGET", 0.02)
    monkeypatch.setattr(terminals, "_tracked_blocking", inline)
    monkeypatch.setattr(terminals, "get_ready_provider_session", lambda _name: row)
    monkeypatch.setattr(terminals, "fork_staleness", lambda _row: (["x"], "[STALE]"))
    monkeypatch.setattr(terminals, "_wait_for_base_ready", busy)
    monkeypatch.setattr(
        terminals, "_dispatch_base_refresh", lambda *_a, **_k: dispatches.append(True)
    )

    started = time.monotonic()
    preamble = await terminals._prepare_fork_refresh(
        "worker", "g", "base", "[STALE]", None, {}
    )

    assert preamble == "[STALE]"
    assert time.monotonic() - started < 0.2
    assert dispatches == []


@pytest.mark.asyncio
async def test_e1_hung_refresh_dispatch_completes_with_stale_preamble(monkeypatch):
    terminals._fork_refresh_locks.clear()
    row = {
        "id": 7, "name": "base", "kind": "base", "source_terminal_id": "base-term",
        "session_uuid": "uuid", "cwd": "/repo",
    }

    async def inline(_terminal, _generation, _kind, operation, function, *args,
                     deadline=None, **kwargs):
        if operation == "fork_refresh_send":
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            raise TimeoutError("hung refresh")
        return function(*args, **kwargs), time.monotonic()

    async def ready(*_args, **_kwargs):
        return True

    monkeypatch.setattr(terminals, "FORK_REFRESH_WAIT_BUDGET", 0.02)
    monkeypatch.setattr(terminals, "_tracked_blocking", inline)
    monkeypatch.setattr(terminals, "get_ready_provider_session", lambda _name: row)
    monkeypatch.setattr(terminals, "fork_staleness", lambda _row: (["x"], "[STALE]"))
    monkeypatch.setattr(terminals, "_wait_for_base_ready", ready)

    started = time.monotonic()
    preamble = await terminals._prepare_fork_refresh(
        "worker", "g", "base", "[STALE]", None, {}
    )

    assert preamble == "[STALE]"
    assert time.monotonic() - started < 0.2


@pytest.mark.asyncio
async def test_e1_deferred_schedule_returns_while_refresh_is_running(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def refresh(*_args):
        entered.set()
        await release.wait()
        return "[FRESH]"

    provider = SimpleNamespace(
        initialize=AsyncMock(), supports_reauth_rebind=False, shell_baseline=None,
    )
    context = ForkContext(
        mode="fork", session_uuid="uuid", base_name="base", provider="codex",
        initial_preamble="[STALE]",
    )
    sent = []
    monkeypatch.setattr(terminals, "_prepare_fork_refresh", refresh)
    monkeypatch.setattr(terminals, "send_input", lambda _id, message, **_kw: sent.append(message))
    monkeypatch.setattr(terminals, "mark_terminal_init_ready", lambda *_a, **_k: True)
    snapshot = {
        "caller_id": "caller", "agent_profile": "dev", "provider": "codex",
        "init_deadline_s": 1.0,
    }

    terminals._schedule_deferred_init(
        provider, "worker", "task", OrchestrationType.ASSIGN, None,
        caller_snapshot=snapshot, fork_context=context, refresh_base_name="base",
    )
    await asyncio.wait_for(entered.wait(), 0.2)
    record = terminals._deferred_tasks_by_terminal["worker"]
    assert record.task.done() is False

    release.set()
    await asyncio.gather(*list(terminals._deferred_init_tasks))
    assert sent == ["[FRESH]\n\ntask"]


@pytest.mark.asyncio
async def test_e1_refresh_wait_is_cancelled_by_terminal_quiesce(monkeypatch):
    entered = asyncio.Event()

    async def refresh(*_args):
        entered.set()
        await asyncio.Event().wait()

    provider = SimpleNamespace(
        initialize=AsyncMock(), supports_reauth_rebind=False, shell_baseline=None,
    )
    context = ForkContext(
        mode="fork", session_uuid="uuid", base_name="base", provider="codex",
        initial_preamble="[STALE]",
    )
    monkeypatch.setattr(terminals, "_prepare_fork_refresh", refresh)
    snapshot = {
        "caller_id": "caller", "agent_profile": "dev", "provider": "codex",
        "init_deadline_s": 1.0,
    }
    terminals._schedule_deferred_init(
        provider, "worker-cancel", "task", OrchestrationType.ASSIGN, None,
        caller_snapshot=snapshot, fork_context=context, refresh_base_name="base",
    )
    await asyncio.wait_for(entered.wait(), 0.2)

    await terminals.quiesce_deferred_terminal("worker-cancel", timeout_s=0.1)

    assert terminals._deferred_tasks_by_terminal.get("worker-cancel") is None
