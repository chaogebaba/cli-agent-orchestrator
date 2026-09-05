"""F729: CAO re-asserts the codex startup-dialog answers at every codex spawn.

Three startup cards stall a fresh codex seat, and each persists its answer under
a different key in the codex user layer (``$CODEX_HOME/config.toml``)::

    trust-directory  -> projects."<dir>".trust_level = "trusted"
    hooks review     -> hooks.state."<key>".trusted_hash = "sha256:<hex>"
    resume cwd       -> tui.resume_cwd = "current"

F597 owned only the first.  The answers come back for two independent reasons:
a fresh ``.cao`` worktree is a never-seen absolute path (both trust keys are
path-keyed), and cc-switch writes a per-account snapshot of config.toml over
``~/.codex/config.toml`` on an account switch, dropping every key the incoming
snapshot does not carry.

The hash constants below are GROUND TRUTH recorded by codex itself, not values
this implementation produced.  ``KNOWN_GRAPHIFY_HOOK_HASH`` appears in two
independent places in the operator's history for a byte-identical
``.codex/hooks.json`` at two different absolute paths, which is also the proof
that the hash is content-only and path-independent.  If codex ever changes its
normalization these tests fail loudly rather than letting CAO write entries
codex will silently reject.
"""

import json
import tomllib
from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.codex import (
    CODEX_RESUME_CWD_MODE,
    _codex_hook_event_label,
    _codex_hook_trust_entries,
    _codex_hook_trusted_hash,
    _codex_trust_roots,
    _reassert_codex_startup_keys,
)

# The single PreToolUse/Bash handler this repository ships in .codex/hooks.json.
GRAPHIFY_HOOKS_JSON = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": "/home/chao/.local/bin/graphify hook-check"}
                ],
            }
        ]
    }
}
KNOWN_GRAPHIFY_HOOK_HASH = "sha256:915a85c19cbfc6d6058d67addb2c495c0c770e805f571f794587d3960411b055"

# Keys CAO owns.  Anything outside these three families must survive a call
# byte-for-byte — the live file also carries the account's base_url and bearer
# token, and losing or rewriting those breaks the operator's codex outright.
FOREIGN_CONFIG = """\
model = "gpt-5.6-sol"
model_provider = "custom"

[model_providers.custom]
name = "custom"
wire_api = "responses"
base_url = "https://example.invalid/v1"
experimental_bearer_token = "sk-do-not-touch-me"

[notice]
hide_rate_limit_model_nudge = true
"""


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    return home


@pytest.fixture
def seat(tmp_path: Path) -> Path:
    """A worktree-shaped seat inside a repo, inside an outer repo.

    Mirrors production: the nested CAO fork is a repository of its own living
    inside the outer checkout, and the seat is a linked worktree below it.  Both
    repo roots are what the operator has to re-trust by hand today.
    """
    outer = tmp_path / "home" / "projects" / "outer-repo"
    (outer / ".git").mkdir(parents=True)
    inner = outer / "fork"
    (inner / ".git").mkdir(parents=True)
    path = inner / ".cao" / "worktrees" / "abcd1234"
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _home_at_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anchor Path.home() inside tmp_path so the root walk has a real boundary."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _write_hooks(directory: Path, payload: dict = GRAPHIFY_HOOKS_JSON) -> Path:
    hooks_file = directory / ".codex" / "hooks.json"
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return hooks_file


# --------------------------------------------------------------------------
# The hash algorithm, pinned against codex's own recorded output.
# --------------------------------------------------------------------------


def test_hook_hash_matches_codex_ground_truth() -> None:
    digest = _codex_hook_trusted_hash(
        "pre_tool_use",
        "Bash",
        {"type": "command", "command": "/home/chao/.local/bin/graphify hook-check"},
    )
    assert digest == KNOWN_GRAPHIFY_HOOK_HASH


def test_hook_hash_is_path_independent(tmp_path: Path) -> None:
    """Same hooks.json content at two paths -> same hash, different key.

    This is the property the whole spawn-time re-assert rests on: CAO can trust
    a brand-new worktree's hooks without ever having seen it.
    """
    first = _write_hooks(tmp_path / "repo-a")
    second = _write_hooks(tmp_path / "repo-b")
    ((key_a, hash_a),) = _codex_hook_trust_entries(first)
    ((key_b, hash_b),) = _codex_hook_trust_entries(second)
    assert hash_a == hash_b == KNOWN_GRAPHIFY_HOOK_HASH
    assert key_a != key_b
    assert key_a == f"{first}:pre_tool_use:0:0"


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("PreToolUse", "pre_tool_use"),
        ("PostToolUse", "post_tool_use"),
        ("SessionStart", "session_start"),
        ("UserPromptSubmit", "user_prompt_submit"),
        ("already_snake", "already_snake"),
    ],
)
def test_event_label_folding(event: str, expected: str) -> None:
    assert _codex_hook_event_label(event) == expected


def test_hash_declines_handlers_it_cannot_normalize() -> None:
    """A hash we are not sure of is worse than none: codex reads a wrong hash as
    "the hook changed" and shows the card anyway, and CAO would have written
    trust state for a hook shape it does not understand."""
    assert _codex_hook_trusted_hash("stop", "", {"type": "mcp_tool", "server": "x"}) is None
    assert _codex_hook_trusted_hash("stop", "", {"type": "command"}) is None
    assert _codex_hook_trusted_hash("stop", "", {"command": "x", "timeout": "600"}) is None


def test_positional_keys_for_multiple_groups_and_handlers(tmp_path: Path) -> None:
    hooks_file = _write_hooks(
        tmp_path / "repo",
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "a"}]},
                    {
                        "matcher": "Read",
                        "hooks": [
                            {"type": "command", "command": "b"},
                            {"type": "command", "command": "c"},
                        ],
                    },
                ]
            }
        },
    )
    keys = [key for key, _ in _codex_hook_trust_entries(hooks_file)]
    assert keys == [
        f"{hooks_file}:pre_tool_use:0:0",
        f"{hooks_file}:pre_tool_use:1:0",
        f"{hooks_file}:pre_tool_use:1:1",
    ]


def test_unreadable_hooks_json_is_not_a_launch_blocker(tmp_path: Path) -> None:
    hooks_file = tmp_path / "repo" / ".codex" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text("{not json", encoding="utf-8")
    assert _codex_hook_trust_entries(hooks_file) == []


# --------------------------------------------------------------------------
# Which directories get trusted.
# --------------------------------------------------------------------------


def test_trust_roots_cover_cwd_and_both_repo_roots(seat: Path) -> None:
    roots = _codex_trust_roots(seat)
    assert roots[0] == seat
    assert seat.parent.parent.parent in roots  # the nested fork checkout
    assert seat.parent.parent.parent.parent in roots  # the outer checkout


def test_trust_roots_walk_seats_outside_home(tmp_path: Path) -> None:
    """CAO provisions seats on a scratch mount outside $HOME.

    An earlier cut stopped the walk at the home boundary, which left every
    production seat with no repo root at all — the exact directories the
    operator has to re-trust by hand.  The boundary is on what may be trusted,
    not on where the walk may start.
    """
    scratch_repo = tmp_path / "scratch" / "worktrees" / "repo"
    (scratch_repo / ".git").mkdir(parents=True)
    seat = scratch_repo / "sub" / "seat"
    seat.mkdir(parents=True)
    assert _codex_trust_roots(seat) == [seat, scratch_repo]


def test_trust_roots_never_reach_home_or_above(seat: Path, tmp_path: Path) -> None:
    """Trusting $HOME would trust every directory the operator ever creates.

    $HOME is made a repository here on purpose: a dotfiles checkout at $HOME is
    common, and without that the ``.git`` test below would pass on its own and
    the boundary would not be load-bearing. Every ancestor of $HOME is given
    one too, so nothing above it can be trusted either.
    """
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    roots = [Path(root).resolve() for root in _codex_trust_roots(seat)]
    assert home.resolve() not in roots
    assert tmp_path.resolve() not in roots
    assert Path("/") not in roots
    # The seat and the two repo roots BELOW home are still trusted.
    assert seat in roots


# --------------------------------------------------------------------------
# The writer.
# --------------------------------------------------------------------------


def test_writes_all_three_owned_key_families(seat: Path, codex_home: Path) -> None:
    hooks_file = _write_hooks(seat)
    assert _reassert_codex_startup_keys(str(seat), codex_home) is True
    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert parsed["projects"][str(seat)]["trust_level"] == "trusted"
    assert (
        parsed["hooks"]["state"][f"{hooks_file}:pre_tool_use:0:0"]["trusted_hash"]
        == KNOWN_GRAPHIFY_HOOK_HASH
    )
    assert parsed["tui"]["resume_cwd"] == CODEX_RESUME_CWD_MODE


def test_idempotent_no_duplicate_tables(seat: Path, codex_home: Path) -> None:
    _write_hooks(seat)
    assert _reassert_codex_startup_keys(str(seat), codex_home) is True
    first = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert _reassert_codex_startup_keys(str(seat), codex_home) is True
    second = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert first == second
    assert second.count("resume_cwd") == 1
    assert second.count(f'[projects."{seat}"]') == 1
    assert second.count("trusted_hash") == 1


def test_resume_cwd_inserted_into_an_existing_tui_table(seat: Path, codex_home: Path) -> None:
    """A second [tui] header would make the file invalid TOML, and codex would
    then fall back to defaults for every key — worse than the card we suppress."""
    config = codex_home / "config.toml"
    config.write_text('[tui]\nstatus_line = ["current-dir"]\n', encoding="utf-8")
    assert _reassert_codex_startup_keys(str(seat), codex_home) is True
    text = config.read_text(encoding="utf-8")
    assert text.count("[tui]") == 1
    parsed = tomllib.loads(text)
    assert parsed["tui"]["resume_cwd"] == CODEX_RESUME_CWD_MODE
    assert parsed["tui"]["status_line"] == ["current-dir"]


def test_operator_resume_cwd_choice_is_left_alone(seat: Path, codex_home: Path) -> None:
    config = codex_home / "config.toml"
    config.write_text('[tui]\nresume_cwd = "session"\n', encoding="utf-8")
    _reassert_codex_startup_keys(str(seat), codex_home)
    assert tomllib.loads(config.read_text(encoding="utf-8"))["tui"]["resume_cwd"] == "session"


def test_writes_only_owned_keys_and_preserves_everything_else(seat: Path, codex_home: Path) -> None:
    """Diff the file before and after: every added line must belong to one of the
    three owned families, and no pre-existing line may change."""
    hooks_file = _write_hooks(seat)
    config = codex_home / "config.toml"
    config.write_text(FOREIGN_CONFIG, encoding="utf-8")
    before = tomllib.loads(FOREIGN_CONFIG)

    assert _reassert_codex_startup_keys(str(seat), codex_home) is True
    after_text = config.read_text(encoding="utf-8")
    after = tomllib.loads(after_text)

    # Nothing pre-existing was dropped or rewritten.
    for key, value in before.items():
        assert after[key] == value
    # The provider block, base_url and bearer token are untouched, verbatim.
    assert FOREIGN_CONFIG in after_text
    assert after["model_providers"]["custom"]["experimental_bearer_token"] == ("sk-do-not-touch-me")
    # Every new top-level key belongs to an owned family.
    assert set(after) - set(before) <= {"projects", "hooks", "tui"}
    assert set(after["hooks"]) == {"state"}
    assert set(after["tui"]) == {"resume_cwd"}
    assert all(set(v) == {"trust_level"} for v in after["projects"].values())
    assert all(set(v) == {"trusted_hash"} for v in after["hooks"]["state"].values())
    assert list(after["hooks"]["state"]) == [f"{hooks_file}:pre_tool_use:0:0"]


def test_never_touches_auth_json(seat: Path, codex_home: Path) -> None:
    auth = codex_home / "auth.json"
    auth.write_text('{"tokens": "untouched"}', encoding="utf-8")
    stat_before = auth.stat()
    _write_hooks(seat)
    assert _reassert_codex_startup_keys(str(seat), codex_home) is True
    assert auth.read_text(encoding="utf-8") == '{"tokens": "untouched"}'
    assert auth.stat().st_mtime_ns == stat_before.st_mtime_ns


def test_seat_without_hooks_still_gets_trust_and_resume(seat: Path, codex_home: Path) -> None:
    assert _reassert_codex_startup_keys(str(seat), codex_home) is True
    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert parsed["projects"][str(seat)]["trust_level"] == "trusted"
    assert parsed["tui"]["resume_cwd"] == CODEX_RESUME_CWD_MODE
    assert "hooks" not in parsed


def test_works_on_an_empty_or_absent_config(seat: Path, codex_home: Path) -> None:
    assert not (codex_home / "config.toml").exists()
    assert _reassert_codex_startup_keys(str(seat), codex_home) is True
    assert (
        tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))["tui"]["resume_cwd"]
        == CODEX_RESUME_CWD_MODE
    )


def test_creates_a_missing_codex_home(seat: Path, tmp_path: Path) -> None:
    home = tmp_path / "persona" / "codex-home"
    assert _reassert_codex_startup_keys(str(seat), home) is True
    assert (home / "config.toml").is_file()


def test_invalid_toml_is_left_untouched(seat: Path, codex_home: Path) -> None:
    config = codex_home / "config.toml"
    config.write_text("this is [not valid toml\n", encoding="utf-8")
    assert _reassert_codex_startup_keys(str(seat), codex_home) is False
    assert config.read_text(encoding="utf-8") == "this is [not valid toml\n"


# --------------------------------------------------------------------------
# The F703 (#558) cwd guard, kept verbatim.
# --------------------------------------------------------------------------


def test_rejects_a_non_path_cwd(codex_home: Path) -> None:
    """A mocked backend's MagicMock cwd is not a path; letting one through is
    how 52 junk trust tables landed in the operator's live config 2026-09-01."""
    assert _reassert_codex_startup_keys(object(), codex_home) is False  # type: ignore[arg-type]
    assert not (codex_home / "config.toml").exists()


def test_rejects_a_relative_cwd(codex_home: Path) -> None:
    assert _reassert_codex_startup_keys("relative/seat", codex_home) is False
    assert not (codex_home / "config.toml").exists()


def test_rejects_a_cwd_that_is_not_a_directory(tmp_path: Path, codex_home: Path) -> None:
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("", encoding="utf-8")
    assert _reassert_codex_startup_keys(str(not_a_dir), codex_home) is False
    assert not (codex_home / "config.toml").exists()
