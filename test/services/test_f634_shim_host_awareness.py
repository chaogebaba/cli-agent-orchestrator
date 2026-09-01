"""F634 (#489) D16 — the F620 laptop shim becomes HOST-AWARE.

AC21. F620's ``should_inject_shim`` keys on repo CONTENTS, never on host: a
worker, a resolvable repo root, ``LAPTOP_OK`` unset, an active row in
``scripts/boxes.tsv`` and a ``scripts/laptop-shims`` directory. ``box-setup.sh``
rsyncs the root repo (active-row roster) together with the nested fork (the shim
dir) onto every box, so a BOX-hosted lane satisfies every one of those clauses
and is hard-denied ``pytest``/``mypy``/``uv`` (exit 97) on the very machine F634
moved it to — F636 (#491) armed exactly that nested-checkout case.

Two layers, because two different mutations break them:

1. the PREDICATE — ``should_inject_shim`` must return False for a box lane;
2. the WIRING — ``terminal_service.create_terminal`` must actually thread
   ``is_box_hosted`` into ``laptop_shim.maybe_shim_env``. It is one more
   parameter on the compose call, or the predicate never sees it, and a green
   predicate suite would not notice — the F636 blind spot repeated.

The layer-2 arms drive the REAL ``create_terminal`` worker branch against a real
nested-repo layout on disk, reusing the F636 spawn harness, so the assertion is
on the PATH the production compose path actually hands ``create_window`` — that
harness IS the production-shaped seam for this decision, and sharing it keeps
both guards' arms on one spawn path rather than two drifting copies. Its
fixtures are re-exported into this module's namespace so pytest resolves them
by name.
"""

from __future__ import annotations

import os

# ``test`` is a CPython stdlib package name, so isort sorts this import into the
# stdlib block — hence its position here rather than beside the first-party ones.
from test.services.test_f636_shim_spawn_path import (  # noqa: F401
    SUPERVISOR,
    _lease_patches,
    _spawn_seam,
    clean_shim_env,
    nested_fork,
)
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services import laptop_shim
from cli_agent_orchestrator.services import terminal_service as ts


class TestPredicateIsHostAware:
    """Layer 1 — ``should_inject_shim`` itself."""

    def test_box_hosted_lane_is_never_shimmed(self, nested_fork, clean_shim_env):
        """MUTANT SENTINEL (predicate ignores ``is_box_hosted``). Every F620
        clause holds here — worker, fork repo root, active roster in the parent,
        LAPTOP_OK unset — and the box flag alone must veto the shim."""
        assert (
            laptop_shim.should_inject_shim(
                is_worker=True,
                repo_root=str(nested_fork["fork"]),
                env={},
                is_box_hosted=True,
            )
            is False
        )

    def test_same_lane_on_the_laptop_still_gets_the_guard(self, nested_fork, clean_shim_env):
        """CONTRAST. Identical inputs minus the flag: the F620 guard is
        unchanged for the laptop's own workers, so the box exemption is the
        only behaviour F634 adds."""
        assert (
            laptop_shim.should_inject_shim(
                is_worker=True,
                repo_root=str(nested_fork["fork"]),
                env={},
                is_box_hosted=False,
            )
            is True
        )

    def test_default_is_laptop_behaviour(self, nested_fork, clean_shim_env):
        """Omitting the argument entirely must behave exactly as before F634 —
        every existing caller of the predicate keeps its verdict."""
        assert (
            laptop_shim.should_inject_shim(
                is_worker=True,
                repo_root=str(nested_fork["fork"]),
                env={},
            )
            is True
        )

    def test_maybe_shim_env_adds_no_path_for_a_box_lane(self, nested_fork, clean_shim_env):
        """The compose helper must leave the env untouched — not merely compose
        a different PATH — so a box lane inherits the box's own tooling."""
        extra_env: dict[str, str] = {}
        result = laptop_shim.maybe_shim_env(
            extra_env,
            is_worker=True,
            repo_root=str(nested_fork["fork"]),
            base_path="/usr/bin:/bin",
            is_box_hosted=True,
        )
        assert "PATH" not in result


async def _create_worker(*, working_directory: str, is_box_hosted: bool):
    return await ts.create_terminal(
        provider="mock_cli",
        agent_profile="developer",
        session_name="cao-f634",
        new_session=False,
        caller_id=SUPERVISOR,
        working_directory=working_directory,
        is_box_hosted=is_box_hosted,
    )


async def _spawn_and_capture_path(nested_fork, *, is_box_hosted: bool) -> str:
    captured: dict = {}
    seam, backend = _spawn_seam(captured=captured)
    l1, l2 = _lease_patches()
    with seam, l1, l2, patch("cli_agent_orchestrator.backends.registry._backend", backend):
        await _create_worker(
            working_directory=str(nested_fork["fork"]),
            is_box_hosted=is_box_hosted,
        )
    return (captured.get("extra_env") or {}).get("PATH", "")


class TestWiringThroughTheRealSpawnPath:
    """Layer 2 — the AC21 arm, through ``create_terminal``'s worker branch."""

    @pytest.mark.asyncio
    async def test_box_hosted_worker_spawn_is_not_shimmed(self, nested_fork, clean_shim_env):
        """MUTANT SENTINEL (drop the ``is_box_hosted`` kwarg from the
        ``maybe_shim_env`` call in ``create_terminal``). A box dev lane created
        via the terminals route — the assign path, the only one that sets
        ``caller_id`` and so the only one that arms the shim block at all —
        must reach ``create_window`` with NO shim dir on PATH, which is what
        lets it run the fork suite in its own worktree on the box. Dropping the
        kwarg leaves the predicate blind and the shim fires: RED."""
        path = await _spawn_and_capture_path(nested_fork, is_box_hosted=True)
        shim_dir = str(nested_fork["shim_dir"])
        assert shim_dir not in path.split(
            os.pathsep
        ), f"box-hosted worker PATH must not carry the shim dir; got PATH={path!r}"

    @pytest.mark.asyncio
    async def test_laptop_worker_spawn_in_the_same_repo_still_denied(
        self, nested_fork, clean_shim_env
    ):
        """CONTRAST, and the other half of AC21: the same worker, same repo,
        same session — only the host differs. A laptop worker still leads with
        the shim dir, so F634 relaxes the guard exactly where the lane is not on
        the laptop and nowhere else."""
        path = await _spawn_and_capture_path(nested_fork, is_box_hosted=False)
        shim_dir = str(nested_fork["shim_dir"])
        assert (
            path.split(os.pathsep)[0] == shim_dir
        ), f"laptop worker PATH must still lead with the shim dir; got PATH={path!r}"
