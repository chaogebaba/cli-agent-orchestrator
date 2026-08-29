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

from cli_agent_orchestrator.providers.codex import _pretrust_cwd_in_codex_home

# A worktree-style path whose dots (.cao) are exactly what codex's -c quoted-key
# parser mishandles — the reason this is a config-file write, not a -c override.
WORKTREE_CWD = "/data/cao-scratch/e58bc008/.cao/worktrees/abcd1234"


def test_writes_trusted_table_for_cwd(tmp_path: Path) -> None:
    assert _pretrust_cwd_in_codex_home(WORKTREE_CWD, tmp_path) is True
    parsed = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert parsed["projects"][WORKTREE_CWD]["trust_level"] == "trusted"


def test_idempotent_no_duplicate(tmp_path: Path) -> None:
    assert _pretrust_cwd_in_codex_home(WORKTREE_CWD, tmp_path) is True
    assert _pretrust_cwd_in_codex_home(WORKTREE_CWD, tmp_path) is True
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    # Exactly one entry — the second call is a no-op.
    assert text.count(f'[projects."{WORKTREE_CWD}"]') == 1
    assert text.count("trust_level") == 1


def test_preserves_existing_content(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt-5.6-sol"\n' '[projects."/home/chao"]\n' 'trust_level = "trusted"\n',
        encoding="utf-8",
    )
    assert _pretrust_cwd_in_codex_home(WORKTREE_CWD, tmp_path) is True
    text = config.read_text(encoding="utf-8")
    # Nothing clobbered.
    assert 'model = "gpt-5.6-sol"' in text
    assert '[projects."/home/chao"]' in text
    # New entry appended.
    parsed = tomllib.loads(text)
    assert parsed["projects"]["/home/chao"]["trust_level"] == "trusted"
    assert parsed["projects"][WORKTREE_CWD]["trust_level"] == "trusted"


def test_relative_cwd_is_absolutized(tmp_path: Path) -> None:
    # A relative cwd is stored as its absolute form (codex keys trust on the
    # canonical absolute project path).
    assert _pretrust_cwd_in_codex_home(".", tmp_path) is True
    import os

    parsed = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert os.path.abspath(".") in parsed["projects"]


def test_invalid_toml_is_not_modified(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("this is [not valid toml", encoding="utf-8")
    # Refuses to touch a file it cannot parse; returns False, content unchanged.
    assert _pretrust_cwd_in_codex_home(WORKTREE_CWD, tmp_path) is False
    assert config.read_text(encoding="utf-8") == "this is [not valid toml"
