"""F634 (#489) D16 / AC21 — the laptop shim is HOST-AWARE at the SPAWN path.

F620 (#476) shipped ``should_inject_shim`` keyed on repo CONTENTS, never on
host; F636 (#491) armed it for the nested fork. So a lane created by a box
``cao-server`` in the box's own fork checkout (rsynced complete with
``boxes.tsv`` and the shims) would be hard-denied ``pytest``/``mypy``/``uv``
(exit 97) on the very machine F634 relocated it to.

D16 threads an ``is_box_hosted`` input from the amended create route through
``terminal_service.create_terminal`` -> ``laptop_shim.maybe_shim_env`` ->
``should_inject_shim`` so a box lane is never shimmed, while the laptop's own
workers (``is_box_hosted`` absent/False) keep the guard unchanged.

This file drives the REAL worker spawn (existing session + caller_id) on a REAL
nested-repo layout with only resource deps stubbed — the same production
compose path F636's suite exercises — and asserts the composed PATH the backend
receives:

* AC21 SENTINEL: a box-hosted worker (``is_box_hosted=True``) on the fork is
  NOT shimmed even though EVERY repo-content condition holds; the laptop worker
  in the SAME repo (``is_box_hosted=False``) IS shimmed. The mutant "keep
  keying the shim on repo contents alone" (drop the ``is_box_hosted`` early
  return in ``should_inject_shim``) makes the box arm go RED.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
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
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(str(path), "init", "-q")
    _git(str(path), "config", "user.email", "t@t")
    _git(str(path), "config", "user.name", "t")


@pytest.fixture
def nested_fork(tmp_path: Path) -> dict[str, Path]:
    """ROOT repo owns ``scripts/boxes.tsv``; nested FORK owns
    ``scripts/laptop-shims`` — the production split (see F636 suite)."""
    root = tmp_path / "cli-subagents"
    _init_repo(root)
    (root / "scripts").mkdir()
    (root / "scripts" / "boxes.tsv").write_text(_ACTIVE_BOXES_TSV)

    fork = root / "cli-agent-orchestrator"
    _init_repo(fork)
    shim_dir = fork / "scripts" / "laptop-shims"
    shim_dir.mkdir(parents=True)
    for name in ("pytest", "mypy", "uv"):
        (shim_dir / name).write_text("#!/usr/bin/env bash\nexit 97\n")
    return {"root": root, "fork": fork, "shim_dir": shim_dir}


@pytest.fixture
def clean_shim_env(monkeypatch):
    monkeypatch.delenv("LAPTOP_OK", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def _spawn_seam(*, captured: dict):
    """Patch create_terminal's resource deps while keeping the REAL worker
    branch, the REAL ``worktree_service.find_repo_root`` and the REAL
    ``laptop_shim`` compose path. ``create_window`` records the ``extra_env``
    it is handed so the test can assert the composed PATH."""

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

    return (
        patch.multiple(
            ts,
            _resolve_worker_terminal_cap=lambda *a, **k: 0,
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
        ),
        backend,
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


async def _create(*, working_directory: str, caller_id: str | None, is_box_hosted: bool):
    return await ts.create_terminal(
        provider="mock_cli",
        agent_profile="developer",
        session_name="cao-f634",
        new_session=False,
        caller_id=caller_id,
        working_directory=working_directory,
        is_box_hosted=is_box_hosted,
    )


async def _spawn_and_capture(nested_fork, *, is_box_hosted: bool) -> str:
    captured: dict = {}
    seam, backend = _spawn_seam(captured=captured)
    l1, l2 = _lease_patches()
    with seam, l1, l2, patch("cli_agent_orchestrator.backends.registry._backend", backend):
        await _create(
            working_directory=str(nested_fork["fork"]),
            caller_id=SUPERVISOR,
            is_box_hosted=is_box_hosted,
        )
    return (captured.get("extra_env") or {}).get("PATH", "")


@pytest.mark.asyncio
async def test_laptop_worker_in_fork_is_shimmed(nested_fork, clean_shim_env):
    """Control: is_box_hosted defaults False -> the F620/F636 guard fires, the
    fork shim dir leads PATH. This is the "laptop-hosted worker in the same repo
    still gets the denial" half of AC21."""
    path = await _spawn_and_capture(nested_fork, is_box_hosted=False)
    shim_dir = str(nested_fork["shim_dir"])
    assert (
        path.split(os.pathsep)[0] == shim_dir
    ), f"laptop worker PATH must lead with the fork shim dir; got PATH={path!r}"


@pytest.mark.asyncio
async def test_box_hosted_worker_in_fork_is_not_shimmed(nested_fork, clean_shim_env):
    """AC21 SENTINEL. Same repo, same worker, same active roster — but the lane
    is box-hosted, so the shim dir must NOT appear on PATH. Dropping D16's
    is_box_hosted early return in should_inject_shim makes this go RED (the box
    lane would be shimmed and denied pytest/mypy/uv at exit 97)."""
    path = await _spawn_and_capture(nested_fork, is_box_hosted=True)
    shim_dir = str(nested_fork["shim_dir"])
    assert shim_dir not in path.split(
        os.pathsep
    ), f"box-hosted lane must NOT be shimmed; got PATH={path!r}"
