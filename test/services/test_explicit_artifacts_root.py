"""AC-LITE-4 — the artifact root is explicit, never inferred from a directory's presence.

``wp-arch-modular-core.md`` A.4/B slice 1: ``canonical_session_env`` used to probe for an
``orchestrator/`` subdirectory and silently relocate artifacts under it. That is the single
place where the mere existence of a skill-owned directory changed infrastructure behaviour,
which is what A.5's AC-LITE-1 mutation arm ("presence must stop meaning anything") forbids.

The override itself is untouched by slice 1; it is retested here because it is now the ONLY
way to move the root, so its contract carries the weight the probe used to.

This repo keeps its own artifacts at ``<repo>/orchestrator/tmp/orch`` by setting
``CAO_ARTIFACTS_DIR`` explicitly (``cao env set``, D1 2026-09-16), not by being sniffed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.session_service import (
    ARTIFACTS_DIR_ENV,
    canonical_session_env,
)


def _root(env: dict[str, str]) -> Path:
    return Path(env[ARTIFACTS_DIR_ENV])


def test_explicit_override_is_honoured_and_resolved(tmp_path: Path) -> None:
    explicit = tmp_path / "repo" / "orchestrator" / "tmp" / "orch"
    env = canonical_session_env(str(tmp_path), {ARTIFACTS_DIR_ENV: str(explicit)})
    assert _root(env) == explicit.resolve()


def test_override_wins_over_the_working_directory(tmp_path: Path) -> None:
    """The override is absolute and immutable — cwd must not be able to move it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = canonical_session_env(
        str(tmp_path / "some" / "other" / "cwd"), {ARTIFACTS_DIR_ENV: str(elsewhere)}
    )
    assert _root(env) == elsewhere.resolve()


@pytest.mark.parametrize("bad", ["", "relative/tmp/orch", "./orch"])
def test_a_non_absolute_override_is_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="artifacts_dir_not_absolute"):
        canonical_session_env(str(tmp_path), {ARTIFACTS_DIR_ENV: bad})


def test_fallback_is_the_neutral_cwd_root(tmp_path: Path) -> None:
    env = canonical_session_env(str(tmp_path), {})
    assert _root(env) == tmp_path.resolve() / "tmp" / "orch"


def test_fallback_ignores_a_present_orchestrator_directory(tmp_path: Path) -> None:
    """The mutation arm of AC-LITE-4, stated positively.

    An ``orchestrator/`` directory in the working tree must produce a root byte-identical
    to the one produced without it. The old behaviour returning here is RED.
    """
    (tmp_path / "orchestrator").mkdir()
    with_dir = canonical_session_env(str(tmp_path), {})

    bare = tmp_path.parent / (tmp_path.name + "-bare")
    bare.mkdir()
    without_dir = canonical_session_env(str(bare), {})

    assert _root(with_dir) == tmp_path.resolve() / "tmp" / "orch"
    assert _root(with_dir) != tmp_path.resolve() / "orchestrator" / "tmp" / "orch"
    # Same shape on both sides: only the directory name differs, never the layout.
    assert _root(with_dir).relative_to(tmp_path.resolve()) == _root(without_dir).relative_to(
        bare.resolve()
    )


@pytest.mark.parametrize(
    "present",
    ["orchestrator", "doctrine", "blueprints", ".claude"],
)
def test_no_skill_owned_directory_changes_the_root(tmp_path: Path, present: str) -> None:
    """Presence must stop meaning anything — for every A.2 directory, not just one."""
    expected = tmp_path.resolve() / "tmp" / "orch"
    (tmp_path / present).mkdir()
    assert _root(canonical_session_env(str(tmp_path), {})) == expected


def test_fallback_uses_the_process_cwd_when_no_working_directory_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "orchestrator").mkdir()
    monkeypatch.chdir(tmp_path)
    env = canonical_session_env(None, {})
    assert _root(env) == Path(os.getcwd()).resolve() / "tmp" / "orch"


def test_other_env_vars_are_preserved(tmp_path: Path) -> None:
    env = canonical_session_env(str(tmp_path), {"CAO_TERMINAL_ID": "t-1", "FOO": "bar"})
    assert env["CAO_TERMINAL_ID"] == "t-1"
    assert env["FOO"] == "bar"
    assert ARTIFACTS_DIR_ENV in env


def test_mutant_directory_probe_is_detectably_different(tmp_path: Path) -> None:
    """The removed probe, replayed: it and the shipped fallback must disagree.

    If this ever stopped disagreeing the test above would be vacuous, because the two
    behaviours would be indistinguishable on this fixture.
    """
    (tmp_path / "orchestrator").mkdir()

    def probing_fallback(working_directory: str) -> Path:
        base = Path(working_directory).resolve()
        orch_sub = base / "orchestrator"
        if orch_sub.is_dir():
            return orch_sub / "tmp" / "orch"
        return base / "tmp" / "orch"

    assert probing_fallback(str(tmp_path)) != _root(canonical_session_env(str(tmp_path), {}))


# ---------------------------------------------------------------------------------------
# D1 — the managed env store is honoured (2026-09-16 ruling, confirmed by lite-review r1)
# ---------------------------------------------------------------------------------------


@pytest.fixture
def managed_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point ``cao env``'s store at a temp file and hand back a setter.

    Patches the constant where ``utils.env`` READ it, not where it was defined — the module
    binds ``CAO_ENV_FILE`` at import time, so patching ``constants`` alone would be a no-op
    and every assertion below would pass against an empty store.
    """
    from cli_agent_orchestrator.utils import env as env_utils

    store = tmp_path / "store" / ".env"
    store.parent.mkdir(parents=True)
    store.touch()
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", store)

    def setter(key: str, value: str) -> None:
        env_utils.set_env_var(key, value)

    return setter


def test_store_value_is_used_when_no_explicit_override(tmp_path: Path, managed_store) -> None:
    """What `cao env set CAO_ARTIFACTS_DIR <abs>` is supposed to do, and now does."""
    explicit = tmp_path / "repo" / "orchestrator" / "tmp" / "orch"
    managed_store(ARTIFACTS_DIR_ENV, str(explicit))
    env = canonical_session_env(str(tmp_path), {})
    assert _root(env) == explicit.resolve()


def test_explicit_override_beats_the_store(tmp_path: Path, managed_store) -> None:
    """`cao launch --env` is a per-launch override and must outrank the persistent store."""
    managed_store(ARTIFACTS_DIR_ENV, str(tmp_path / "from-store"))
    per_launch = tmp_path / "from-env-flag"
    env = canonical_session_env(str(tmp_path), {ARTIFACTS_DIR_ENV: str(per_launch)})
    assert _root(env) == per_launch.resolve()


def test_empty_store_falls_back_to_the_neutral_root(tmp_path: Path, managed_store) -> None:
    del managed_store  # the store exists but carries no artifacts key
    env = canonical_session_env(str(tmp_path), {})
    assert _root(env) == tmp_path.resolve() / "tmp" / "orch"


def test_a_relative_store_value_is_rejected_like_a_relative_flag(
    tmp_path: Path, managed_store
) -> None:
    """The store gets the same validation as --env, and the error names where it came from."""
    managed_store(ARTIFACTS_DIR_ENV, "relative/tmp/orch")
    with pytest.raises(ValueError, match="artifacts_dir_not_absolute") as excinfo:
        canonical_session_env(str(tmp_path), {})
    assert "env store" in str(excinfo.value)


def test_only_the_artifacts_key_is_taken_from_the_store(tmp_path: Path, managed_store) -> None:
    """The store must not become a process-environment injector for unrelated keys.

    Operators already keep values like `API_TOKEN` there for `${VAR}` substitution. If the
    whole store were splatted into the session floor, every one of them would silently reach
    every worker process.
    """
    managed_store("API_TOKEN", "secret")
    managed_store("BASE_URL", "http://localhost:27124")
    managed_store(ARTIFACTS_DIR_ENV, str(tmp_path / "root"))
    env = canonical_session_env(str(tmp_path), {})
    assert "API_TOKEN" not in env
    assert "BASE_URL" not in env
    assert _root(env) == (tmp_path / "root").resolve()


def test_store_does_not_reintroduce_directory_sniffing(tmp_path: Path, managed_store) -> None:
    """AC-LITE-4's mutation arm must survive the D1 fix.

    The fix adds a second source for an EXPLICIT value; it must not add a second way for a
    directory's mere presence to matter.
    """
    del managed_store
    (tmp_path / "orchestrator").mkdir()
    env = canonical_session_env(str(tmp_path), {})
    assert _root(env) == tmp_path.resolve() / "tmp" / "orch"
    assert _root(env) != tmp_path.resolve() / "orchestrator" / "tmp" / "orch"
