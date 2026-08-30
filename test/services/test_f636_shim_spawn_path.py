"""F636 (#491) — laptop-shim guard exercised through a REAL worker SPAWN.

F620 (#476) shipped ``should_inject_shim`` and wired it into
``terminal_service.create_terminal``'s worker branch, but its whole test
suite drove the PREDICATE directly with a fixture repo that put BOTH
``scripts/boxes.tsv`` and ``scripts/laptop-shims`` under ONE root. Production
never has that layout: ``boxes.tsv`` lives only in the ROOT repo and
``scripts/laptop-shims`` only in the nested CAO FORK, and ``find_repo_root``
(``git rev-parse --show-toplevel``) stops at the fork's own ``.git``. So the
guard was inert for every worker everywhere, and a green predicate suite never
noticed — the blind spot this file closes.

These tests build the REAL nested-repo layout on disk (a root git repo with an
active ``boxes.tsv``, a fork git repo nested inside it carrying the shim dir)
and drive the REAL ``create_terminal`` worker branch with only its resource
dependencies stubbed. ``worktree_service.find_repo_root`` and the whole
``laptop_shim`` module run for real, so the assertion is on the PATH that the
production compose path actually hands the backend's ``create_window``:

* a WORKER spawned with cwd inside the fork gets ``scripts/laptop-shims``
  prepended to ``PATH`` (the guard fires — this is the mutant sentinel;
  reverting the F636 parent-repo lookup makes this go RED because the single
  root only finds the shim dir, never the roster);
* an OPERATOR launch (new session, no caller) is never shimmed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.session_lifecycle_lease import (
    SessionLifecycleLeaseToken,
)

SUPERVISOR = "aaaaaaaa"

_ACTIVE_BOXES_TSV = (
    "# host\tstate\tsince\treason\n"
    "box@grok-box-1\tfrozen\t2026-08-27\tin use elsewhere\n"
    "box@grok-box-2\tactive\t-\n"
)


def _git(cwd: str, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    """A minimal, self-contained git repo (no user config dependency)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(str(path), "init", "-q")
    # Local identity so commit-less `rev-parse --show-toplevel` still resolves
    # without a global gitconfig; init alone is enough for --show-toplevel.
    _git(str(path), "config", "user.email", "t@t")
    _git(str(path), "config", "user.name", "t")


@pytest.fixture
def nested_fork(tmp_path: Path) -> dict[str, Path]:
    """Reproduce the production split: ROOT repo owns ``scripts/boxes.tsv``;
    a nested FORK repo owns ``scripts/laptop-shims`` and nothing else.

    Returns the root repo path, the fork repo path (a worker's cwd), and the
    fork's shim dir (the value expected at the head of a shimmed PATH).
    """
    root = tmp_path / "cli-subagents"
    _init_repo(root)
    (root / "scripts").mkdir()
    (root / "scripts" / "boxes.tsv").write_text(_ACTIVE_BOXES_TSV)

    fork = root / "cli-agent-orchestrator"
    _init_repo(fork)  # nested repo -> its own .git; find_repo_root stops here
    shim_dir = fork / "scripts" / "laptop-shims"
    shim_dir.mkdir(parents=True)
    for name in ("pytest", "mypy", "uv"):
        (shim_dir / name).write_text("#!/usr/bin/env bash\nexit 97\n")

    # Sanity: find_repo_root really does stop at the fork (the whole bug).
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(fork),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(top).resolve() == fork.resolve()

    return {"root": root, "fork": fork, "shim_dir": shim_dir}


@pytest.fixture
def clean_shim_env(monkeypatch):
    """LAPTOP_OK unset and a deterministic base PATH so the shim decision and
    the composed value are both reproducible (``maybe_shim_env`` reads both
    from the process env when not threaded explicitly)."""
    monkeypatch.delenv("LAPTOP_OK", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def _spawn_seam(*, captured: dict):
    """Patch create_terminal's resource deps while keeping the REAL worker
    branch, the REAL ``worktree_service.find_repo_root`` and the REAL
    ``laptop_shim`` compose path. ``create_window`` records the ``extra_env``
    it is handed so the test can assert the composed PATH.
    """

    def _create_window(session, window, *a, **kw):
        captured["extra_env"] = kw.get("extra_env")
        return window

    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.side_effect = _create_window
    backend.supports_event_inbox.return_value = False
    backend.set_window_parent = None

    provider = AsyncMock()
    provider.initialize.return_value = True
    provider.shell_baseline = None

    published: list = []

    def _db_create(terminal_id, tmux_session, tmux_window, provider_name, *args, **kw):
        published.append({"id": terminal_id, "caller_id": kw.get("caller_id")})
        return {"id": terminal_id}

    ids = iter(f"wrk0{i:04d}" for i in range(1, 1000))

    return patch.multiple(
        ts,
        _resolve_worker_terminal_cap=lambda *a, **k: 0,  # cap disabled: isolate the shim seam
        list_terminals_by_session=lambda s: list(published),
        db_create_terminal=_db_create,
        delete_terminals_by_session=MagicMock(),
        generate_terminal_id=lambda: next(ids),
        generate_window_name=lambda profile, tid: f"{profile}-{tid}",
        provider_manager=MagicMock(create_provider=MagicMock(return_value=provider)),
        fifo_manager=MagicMock(),
        _schedule_deferred_init=MagicMock(),
        require_provider_admitted=lambda provider: None,
        load_agent_profile=lambda name: AgentProfile(name="developer", description="dev"),
        get_provider_class=lambda name: type(
            "Cap",
            (),
            {"supports_seed_resume_identity": False, "has_process_child": False},
        ),
    ), backend


async def _create(
    *,
    working_directory: str,
    new_session: bool,
    caller_id: str | None,
    session_name: str = "cao-f636",
):
    return await ts.create_terminal(
        provider="mock_cli",
        agent_profile="developer",
        session_name=session_name,
        new_session=new_session,
        caller_id=caller_id,
        working_directory=working_directory,
    )


def _lease_patches():
    return (
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease."
            "acquire_session_lifecycle_shared",
            lambda session_name: SessionLifecycleLeaseToken(
                session_name=session_name, mode="shared", nonce="t"
            ),
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease."
            "release_session_lifecycle_lease",
            lambda token: None,
        ),
    )


@pytest.mark.asyncio
async def test_worker_spawn_on_nested_fork_gets_shim_prepended(
    nested_fork, clean_shim_env
):
    """MUTANT SENTINEL. A real worker spawn (existing session + caller_id) with
    cwd inside the fork must receive the fork's ``scripts/laptop-shims`` at the
    head of PATH — proving the F636 parent-repo ``boxes.tsv`` lookup fired
    through the production compose path. Reverting the fix (single-root lookup)
    fails to find the roster under the fork and this assertion goes RED.
    """
    captured: dict = {}
    seam, backend = _spawn_seam(captured=captured)
    l1, l2 = _lease_patches()
    with seam, l1, l2, patch(
        "cli_agent_orchestrator.backends.registry._backend", backend
    ):
        await _create(
            working_directory=str(nested_fork["fork"]),
            new_session=False,
            caller_id=SUPERVISOR,
        )

    extra_env = captured.get("extra_env") or {}
    path = extra_env.get("PATH", "")
    shim_dir = str(nested_fork["shim_dir"])
    assert path.split(os.pathsep)[0] == shim_dir, (
        f"worker PATH must lead with the fork shim dir; got PATH={path!r}"
    )


@pytest.mark.asyncio
async def test_worker_spawn_with_all_frozen_fleet_is_not_shimmed(
    nested_fork, clean_shim_env
):
    """Same worker create_window path, no active offload target. When the
    root's ``boxes.tsv`` has NO active row (an all-frozen or box-hosted fleet:
    the laptop is the only place to run, so obstructing it is pure harm), the
    worker's PATH must NOT carry the shim dir. Exercises the guard's negative
    arm through the production compose path, not just the predicate."""
    # Rewrite the roster so every box is frozen.
    (nested_fork["root"] / "scripts" / "boxes.tsv").write_text(
        "box@grok-box-1\tfrozen\t2026-08-27\tin use elsewhere\n"
    )
    captured: dict = {}
    seam, backend = _spawn_seam(captured=captured)
    l1, l2 = _lease_patches()
    with seam, l1, l2, patch(
        "cli_agent_orchestrator.backends.registry._backend", backend
    ):
        await _create(
            working_directory=str(nested_fork["fork"]),
            new_session=False,
            caller_id=SUPERVISOR,
        )

    extra_env = captured.get("extra_env") or {}
    shim_dir = str(nested_fork["shim_dir"])
    path = extra_env.get("PATH", "")
    assert shim_dir not in path.split(os.pathsep), (
        f"no active box -> worker must not be shimmed; got PATH={path!r}"
    )


@pytest.mark.asyncio
async def test_operator_new_session_launch_is_not_shimmed(
    nested_fork, clean_shim_env
):
    """A non-worker launch (new session, no caller_id — the operator's own
    terminal) never enters the worker shim branch: it goes through
    ``create_session``, not the shimmed ``create_window`` seam. Asserted by the
    worker create_window path never being reached at all."""
    captured: dict = {}
    seam, backend = _spawn_seam(captured=captured)
    l1, l2 = _lease_patches()
    with seam, l1, l2, patch(
        "cli_agent_orchestrator.backends.registry._backend", backend
    ):
        await _create(
            working_directory=str(nested_fork["fork"]),
            new_session=True,
            caller_id=None,
        )

    # The shimmed seam is create_window (existing-session worker branch); an
    # operator new-session launch uses create_session, so create_window — and
    # thus the shim compose — is never reached.
    assert backend.create_window.call_count == 0
    assert "extra_env" not in captured
