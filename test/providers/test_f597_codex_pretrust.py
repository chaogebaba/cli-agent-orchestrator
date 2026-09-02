"""F597 #454: codex pre-trusts the worker cwd so the trust-directory dialog is
never shown for a fresh worktree seat.

Approach (config-file, not ``-c``): codex's dotted-path ``-c`` override parser
has no TOML quoted-key support (openai/codex#34261), so
``-c 'projects."<abs path>".trust_level="trusted"'`` is silently misapplied and
the unquoted workaround breaks for any path containing a ``.`` — every
``.cao/worktrees/...`` seat. Live-verified 2026-08-29 against codex-cli 0.151.0:
a config.toml ``[projects."<cwd>"] trust_level = "trusted"`` table in the launch
CODEX_HOME suppresses the interactive trust dialog outright.
"""

import tomllib
from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.codex import _pretrust_cwd_in_codex_home


@pytest.fixture
def worktree_cwd(tmp_path: Path) -> str:
    """A REAL worktree-style directory whose dots (.cao) are exactly what codex's
    -c quoted-key parser mishandles — the reason this is a config-file write, not
    a -c override.

    It must exist on disk: F703 (#558) narrowed the writer to admit only an
    existing absolute directory, after a MagicMock cwd from a mocked backend
    reached it and wrote junk trust tables into the operator's live ~/.codex.
    """
    path = tmp_path / "seat" / ".cao" / "worktrees" / "abcd1234"
    path.mkdir(parents=True)
    return str(path)


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    """A codex home separate from the trusted cwd, as in production."""
    home = tmp_path / "codex-home"
    home.mkdir()
    return home


def test_writes_trusted_table_for_cwd(worktree_cwd: str, codex_home: Path) -> None:
    assert _pretrust_cwd_in_codex_home(worktree_cwd, codex_home) is True
    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert parsed["projects"][worktree_cwd]["trust_level"] == "trusted"


def test_idempotent_no_duplicate(worktree_cwd: str, codex_home: Path) -> None:
    assert _pretrust_cwd_in_codex_home(worktree_cwd, codex_home) is True
    assert _pretrust_cwd_in_codex_home(worktree_cwd, codex_home) is True
    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    # Exactly one entry — the second call is a no-op.
    assert text.count(f'[projects."{worktree_cwd}"]') == 1
    assert text.count("trust_level") == 1


def test_preserves_existing_content(worktree_cwd: str, codex_home: Path) -> None:
    config = codex_home / "config.toml"
    config.write_text(
        'model = "gpt-5.6-sol"\n' '[projects."/home/chao"]\n' 'trust_level = "trusted"\n',
        encoding="utf-8",
    )
    assert _pretrust_cwd_in_codex_home(worktree_cwd, codex_home) is True
    text = config.read_text(encoding="utf-8")
    # Nothing clobbered.
    assert 'model = "gpt-5.6-sol"' in text
    assert '[projects."/home/chao"]' in text
    # New entry appended.
    parsed = tomllib.loads(text)
    assert parsed["projects"]["/home/chao"]["trust_level"] == "trusted"
    assert parsed["projects"][worktree_cwd]["trust_level"] == "trusted"


def test_relative_cwd_is_refused(codex_home: Path) -> None:
    # Narrowed by F703 (#558): a relative cwd used to be absolutized against the
    # process cwd. That is exactly how a mocked backend's "MagicMock/<name>/<id>"
    # became a trust table, and the pane cwd is always absolute in production, so
    # a relative value is now refused instead.
    assert _pretrust_cwd_in_codex_home(".", codex_home) is False
    assert not (codex_home / "config.toml").exists()


def test_invalid_toml_is_not_modified(worktree_cwd: str, codex_home: Path) -> None:
    config = codex_home / "config.toml"
    config.write_text("this is [not valid toml", encoding="utf-8")
    # Refuses to touch a file it cannot parse; returns False, content unchanged.
    assert _pretrust_cwd_in_codex_home(worktree_cwd, codex_home) is False
    assert config.read_text(encoding="utf-8") == "this is [not valid toml"
