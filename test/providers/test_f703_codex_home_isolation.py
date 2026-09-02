"""F703 (#558): the test suite must never resolve — or write to — the REAL ~/.codex.

Incident 2026-09-01: after local fork test runs, the operator's live
``~/.codex/config.toml`` had grown from 6 to 58 ``[projects.…]`` tables. 52 of
them looked like::

    [projects."/data/cao-scratch/side-lane-fork/MagicMock/get_backend().get_pane_working_directory()/140003758390784"]
    trust_level = "trusted"

Chain: ``CodexProvider.initialize`` (codex.py:2679-2681) pre-trusts the pane cwd
via ``_pretrust_cwd_in_codex_home``; the home came from
``persona_context.resolve_codex_home``, which — with no persona plan, i.e. in
every unit test — fell through to ``provider_home("codex").home``, the real
``~/.codex``; and the cwd came from a MagicMock backend, which implements
``__fspath__`` and so yields the *relative* string ``MagicMock/<name>/<id>``
that ``os.path.abspath`` then anchored to the process cwd. Codex analogue of
#405 (F549), whose kiro-side pins live in ``test/conftest.py``.

Two independent layers now stop this, and each is pinned by its own test below
so that removing either one is caught on its own terms:

* ``test_initialize_never_resolves_the_real_codex_home`` — the ``CODEX_HOME``
  pin in ``test/conftest.py::_hermetic_cao_env``, asserted on the RESOLVED home
  rather than on a filesystem side effect, so the cwd guard cannot mask it.
* ``test_pretrust_refuses_a_mock_cwd`` — the existing-absolute-directory guard
  in ``_pretrust_cwd_in_codex_home``, asserted directly against a real home.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    _pretrust_cwd_in_codex_home,
    _resolved_codex_home,
)


@pytest.fixture
def sentinel_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand in for the operator's ~/.codex, so a leak is observable, not destructive."""
    from cli_agent_orchestrator.utils import provider_plane

    sentinel = tmp_path / "SENTINEL-real-codex-home"

    def _fake_production_home(provider: str) -> Path:
        if provider == "codex":
            return sentinel
        return tmp_path / f"SENTINEL-real-{provider}-home"

    monkeypatch.setattr(provider_plane, "_production_home", _fake_production_home)
    return sentinel


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
@patch("cli_agent_orchestrator.providers.codex.wait_until_status")
@patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
@patch("cli_agent_orchestrator.providers.codex.get_backend")
async def test_initialize_never_resolves_the_real_codex_home(
    mock_backend,
    mock_wait_shell,
    mock_wait_status,
    _mock_sleep,
    sentinel_real_home: Path,
) -> None:
    """Mutant: drop the CODEX_HOME pin in test/conftest.py::_hermetic_cao_env."""
    mock_wait_shell.return_value = True
    mock_wait_status.return_value = True
    mock_backend.return_value.get_history.return_value = "OpenAI Codex (v0.152.0)"
    # Left as a bare MagicMock on purpose: this is the exact shape of every
    # launch-exercising unit test in test/providers, and the shape that produced
    # the junk entries.

    assert await CodexProvider("test1234", "test-session", "window-0", None).initialize() is True

    # Asserted on the RESOLUTION, not on a write: the cwd guard would otherwise
    # suppress the side effect and hide a missing pin.
    resolved = _resolved_codex_home("test1234")
    assert resolved != sentinel_real_home, (
        "F703: a test resolved the REAL codex home — the CODEX_HOME pin in "
        "test/conftest.py::_hermetic_cao_env is missing or was overridden"
    )
    assert resolved == Path(os.environ["CODEX_HOME"])
    assert not sentinel_real_home.exists()

    # End to end: nothing was trusted anywhere as a result of this launch.
    config = resolved / "config.toml"
    written = config.read_text(encoding="utf-8") if config.exists() else ""
    assert "MagicMock" not in written
    assert "[projects." not in written


def test_pretrust_refuses_a_mock_cwd(tmp_path: Path) -> None:
    """Mutant: drop the isabs/isdir guard in _pretrust_cwd_in_codex_home."""
    home = tmp_path / "codex-home"
    mock_cwd = MagicMock(name="get_backend().get_pane_working_directory()")

    # A MagicMock satisfies os.PathLike (mock implements __fspath__) and fspaths
    # to a RELATIVE string, which abspath would happily anchor to the cwd — this
    # is the whole trap, so assert it rather than assuming it.
    assert isinstance(mock_cwd, os.PathLike)
    assert not os.path.isabs(os.fspath(mock_cwd))

    assert _pretrust_cwd_in_codex_home(mock_cwd, home) is False
    assert not (home / "config.toml").exists()


def test_pretrust_refuses_a_nonexistent_absolute_path(tmp_path: Path) -> None:
    """An absolute path that is not a directory is refused too."""
    home = tmp_path / "codex-home"
    assert _pretrust_cwd_in_codex_home(str(tmp_path / "no-such-worktree"), home) is False
    assert not (home / "config.toml").exists()


def test_pretrust_still_writes_for_a_real_directory(tmp_path: Path) -> None:
    """The guard must not break the F597 behaviour it is protecting."""
    import tomllib

    home = tmp_path / "codex-home"
    worktree = tmp_path / ".cao" / "worktrees" / "abcd1234"
    worktree.mkdir(parents=True)

    assert _pretrust_cwd_in_codex_home(str(worktree), home) is True
    parsed = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert parsed["projects"][str(worktree)]["trust_level"] == "trusted"
