from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.cli.commands.profile import _validate_frontmatter
from cli_agent_orchestrator.models.agent_profile import AgentProfile, ContextPolicy
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.utils import persona_context
from cli_agent_orchestrator.utils.persona_context import (
    PersonaContextError,
    cleanup_persona,
    compose_persona_plan,
    cwd_key,
    filter_native_memory,
    load_persona_plan,
    persona_wrapper_prefix,
    reap_persona_generations,
    resolve_codex_home,
    wrap_claude_persona,
)
from cli_agent_orchestrator.utils.provider_plane import ProviderHome
from cli_agent_orchestrator.utils.sandbox_guard import bind_pane_identity


@pytest.fixture
def persona_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    home = tmp_path / "home"
    claude_home = home / ".claude"
    codex_home = home / ".codex"
    claude_home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    (claude_home / ".credentials.json").write_text(
        '{"claudeAiOauth":{"accessToken":"test-token","expiresAt":9999999999999}}\n',
        encoding="utf-8",
    )
    (claude_home / "CLAUDE.md").write_text("# global context\n", encoding="utf-8")
    (codex_home / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'model = "gpt-test"\n\n[features]\nmulti_agent = true\n', encoding="utf-8"
    )
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def fake_provider_home(provider: str) -> ProviderHome:
        native = claude_home if provider == "claude_code" else codex_home
        return ProviderHome(provider, "production", native)

    monkeypatch.setattr(persona_context, "provider_home", fake_provider_home)
    monkeypatch.setattr(persona_context, "_preflight", lambda plan: None)
    persona_context._PREFLIGHTED_BWRAP.clear()
    return {
        "runtime": runtime,
        "home": home,
        "claude": claude_home,
        "codex": codex_home,
        "cwd": tmp_path / "work.repo_name",
    }


def _memory(path: Path, name: str, memory_type: str, body: str) -> None:
    path.write_text(
        "---\n" f"name: {name}\n" "metadata:\n" f"  type: {memory_type}\n" "---\n" f"{body}\n",
        encoding="utf-8",
    )


def _seed_memory(env: dict[str, Path]) -> Path:
    env["cwd"].mkdir()
    corpus = env["claude"] / "projects" / cwd_key(env["cwd"].resolve()) / "memory"
    corpus.mkdir(parents=True)
    _memory(corpus / "feedback.md", "feedback-one", "feedback", "feedback body")
    _memory(corpus / "project.md", "project-one", "project", "project body")
    _memory(corpus / "user.md", "user-one", "user", "user body")
    _memory(corpus / "reference.md", "reference-one", "reference", "reference body")
    (corpus / "MEMORY.md").write_text("source index must not be copied\n", encoding="utf-8")
    return corpus


def test_cwd_key_pinned_encodings() -> None:
    assert cwd_key("/home/chao/VScode_projects/cli-subagents") == (
        "-home-chao-VScode-projects-cli-subagents"
    )
    assert cwd_key("/home/chao/.claude_test") == "-home-chao--claude-test"


@pytest.mark.requires_bwrap
def test_filter_and_compose_claude_manifest_rehydration(persona_env: dict[str, Path]) -> None:
    corpus = _seed_memory(persona_env)
    (persona_env["claude"] / "agents.json").write_text("{}\n", encoding="utf-8")
    policy = ContextPolicy(
        scope="persona",
        memoryTypes=["feedback"],
        memoryNames=["project-one"],
        extraLeaves=["agents.json"],
    )
    assert [path.name for path in filter_native_memory(corpus, policy)] == [
        "feedback.md",
        "project.md",
    ]

    plan = compose_persona_plan("terminal-one", "claude_code", "maker", policy, persona_env["cwd"])
    generation = plan.generation_dir
    assert stat.S_IMODE(generation.stat().st_mode) == 0o700
    assert stat.S_IMODE((generation / "persona-manifest.json").stat().st_mode) == 0o600
    assert not any(
        path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        for path in generation.rglob(".credentials.json")
    ), "persona tree must not contain real credential data (non-empty .credentials.json)"
    copied = generation / "projects" / cwd_key(persona_env["cwd"].resolve()) / "memory"
    assert sorted(path.name for path in copied.glob("*.md")) == [
        "MEMORY.md",
        "feedback.md",
        "project.md",
    ]
    assert "source index must not be copied" not in (copied / "MEMORY.md").read_text()
    header = (generation / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    assert header[0] == "# CAO persona context"
    assert header[1].startswith("Profile: maker (policy sha256:")
    assert len(header[1].split("sha256:", 1)[1].rstrip(")")) == 12
    manifest = json.loads((generation / "persona-manifest.json").read_text())
    assert manifest["manifest_version"] == 1
    assert manifest["terminal_id"] == "terminal-one"
    assert manifest["provider"] == "claude_code"
    assert manifest["created_mount_points"] == [
        str(persona_env["cwd"] / ".claude"),
        str(persona_env["cwd"] / "CLAUDE.md"),
    ]
    assert persona_env["cwd"].joinpath(".claude").is_dir()
    assert persona_env["cwd"].joinpath("CLAUDE.md").read_text() == ""

    rehydrated = load_persona_plan("terminal-one")
    assert rehydrated is not None
    assert persona_wrapper_prefix(rehydrated) == persona_wrapper_prefix(plan)
    inner = ["claude", "--session-id", "abc"]
    assert wrap_claude_persona(rehydrated, inner)[-len(inner) :] == inner

    second = compose_persona_plan(
        "terminal-one", "claude_code", "maker", policy, persona_env["cwd"]
    )
    assert second.generation == "gen-2"
    assert generation.is_dir()
    assert os.readlink(second.generation_dir.parent / "current") == "gen-2"

    cleanup_persona("terminal-one")
    assert not second.generation_dir.parent.exists()
    assert persona_env["cwd"].joinpath(".claude").is_dir()
    assert persona_env["cwd"].joinpath("CLAUDE.md").is_file()


@pytest.mark.requires_bwrap
def test_reap_persona_generations_keeps_current_only(persona_env: dict[str, Path]) -> None:
    persona_env["cwd"].mkdir()
    policy = ContextPolicy(scope="persona")
    first = compose_persona_plan(
        "terminal-reap", "claude_code", "maker", policy, persona_env["cwd"]
    )
    second = compose_persona_plan(
        "terminal-reap", "claude_code", "maker", policy, persona_env["cwd"]
    )

    reap_persona_generations("terminal-reap")

    assert not first.generation_dir.exists()
    assert second.generation_dir.is_dir()
    cleanup_persona("terminal-reap")


@pytest.mark.requires_bwrap
def test_manifest_rehydration_fails_loud_on_invalid_runtime_root(
    persona_env: dict[str, Path],
) -> None:
    persona_env["cwd"].mkdir()
    policy = ContextPolicy(scope="persona")
    compose_persona_plan(
        "terminal-invalid-runtime", "claude_code", "maker", policy, persona_env["cwd"]
    )
    persona_env["runtime"].chmod(0o755)
    try:
        with pytest.raises(PersonaContextError, match="ownership_or_mode"):
            load_persona_plan("terminal-invalid-runtime")
    finally:
        persona_env["runtime"].chmod(0o700)


@pytest.mark.requires_bwrap
def test_manifest_rehydration_fails_loud_on_inaccessible_persona_root(
    persona_env: dict[str, Path],
) -> None:
    persona_env["cwd"].mkdir()
    plan = compose_persona_plan(
        "terminal-inaccessible-load",
        "claude_code",
        "maker",
        ContextPolicy(scope="persona"),
        persona_env["cwd"],
    )
    terminal_root = plan.generation_dir.parent
    terminal_root.chmod(0o000)
    try:
        with pytest.raises(PersonaContextError, match="manifest_probe_inaccessible"):
            load_persona_plan("terminal-inaccessible-load")
    finally:
        terminal_root.chmod(0o700)


@pytest.mark.requires_bwrap
def test_generation_reaper_fails_loud_on_inaccessible_persona_root(
    persona_env: dict[str, Path],
) -> None:
    persona_env["cwd"].mkdir()
    plan = compose_persona_plan(
        "terminal-inaccessible-reap",
        "claude_code",
        "maker",
        ContextPolicy(scope="persona"),
        persona_env["cwd"],
    )
    terminal_root = plan.generation_dir.parent
    terminal_root.chmod(0o000)
    try:
        with pytest.raises(PersonaContextError, match="manifest_probe_inaccessible"):
            reap_persona_generations("terminal-inaccessible-reap")
    finally:
        terminal_root.chmod(0o700)


@pytest.mark.parametrize(
    "leaf",
    [
        "/absolute",
        "../x",
        "a/b",
        "a\\b",
        "\x00",
        ".",
        "..",
        "CLAUDE.md",
        ".credentials.json",
        "settings.json",
        "persona-manifest.json",
    ],
)
def test_leaf_policy_rejects_invalid_and_reserved_destinations(leaf: str) -> None:
    with pytest.raises(ValidationError):
        ContextPolicy(scope="persona", extraLeaves=[leaf])


def test_symlink_leaf_fails_without_partial_generation(persona_env: dict[str, Path]) -> None:
    _seed_memory(persona_env)
    target = persona_env["claude"] / "real-leaf"
    target.write_text("content", encoding="utf-8")
    (persona_env["claude"] / "linked-leaf").symlink_to(target)
    policy = ContextPolicy(scope="persona", extraLeaves=["linked-leaf"])
    with pytest.raises(PersonaContextError, match="persona_leaf_source_invalid"):
        compose_persona_plan("terminal-leaf", "claude_code", "maker", policy, persona_env["cwd"])
    terminal_root = persona_env["runtime"] / "cao-personas" / "terminal-leaf"
    assert not list(terminal_root.glob("gen-*"))


def test_codex_home_auth_memory_and_manifest_resolution(persona_env: dict[str, Path]) -> None:
    _seed_memory(persona_env)
    policy = ContextPolicy(scope="persona", memoryNames=["reference-one"])
    plan = compose_persona_plan("codex-one", "codex", "reviewer", policy, persona_env["cwd"])
    assert plan.codex_home is not None
    assert (plan.codex_home / "auth.json").is_symlink()
    assert os.readlink(plan.codex_home / "auth.json") == str(
        (persona_env["codex"] / "auth.json").resolve()
    )
    config = (plan.codex_home / "config.toml").read_text(encoding="utf-8")
    assert config.startswith("project_doc_max_bytes = 0\n")
    assert "[features]\nmulti_agent = true" in config
    assert "reference body" in plan.memory_instructions
    assert "feedback body" not in plan.memory_instructions
    assert resolve_codex_home("codex-one") == plan.codex_home
    assert load_persona_plan("codex-one") == plan
    pane_env = bind_pane_identity({}, "codex-one", plan=plan)
    assert pane_env["CODEX_HOME"] == str(plan.codex_home)
    with pytest.raises(ValueError, match="override CODEX_HOME"):
        bind_pane_identity({"CODEX_HOME": "/operator/value"}, "codex-one", plan=plan)


def test_two_codex_personas_render_distinct_developer_instructions(
    persona_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_memory(persona_env)
    first = compose_persona_plan(
        "codex-feedback",
        "codex",
        "maker",
        ContextPolicy(scope="persona", memoryNames=["feedback-one"]),
        persona_env["cwd"],
    )
    second = compose_persona_plan(
        "codex-project",
        "codex",
        "maker",
        ContextPolicy(scope="persona", memoryNames=["project-one"]),
        persona_env["cwd"],
    )
    profile = AgentProfile(name="maker", description="maker", system_prompt="charter")
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.codex.load_agent_profile", lambda name: profile
    )
    command_one = CodexProvider(
        "codex-feedback", "session", "window-one", "maker", persona_plan=first
    )._build_codex_command()
    command_two = CodexProvider(
        "codex-project", "session", "window-two", "maker", persona_plan=second
    )._build_codex_command()
    # developer_instructions live in a temp file + $(cat) fragment (upstream 0e7b70bb)
    import re
    from pathlib import Path as _P

    def _read_dev_instr(command: str) -> str:
        m = re.search(r"\$\(cat (\S+)\)", command)
        assert m is not None, command
        return _P(m.group(1)).read_text(encoding="utf-8")

    instructions_one = _read_dev_instr(command_one)
    instructions_two = _read_dev_instr(command_two)
    assert "feedback body" in instructions_one and "project body" not in instructions_one
    assert "project body" in instructions_two and "feedback body" not in instructions_two
    assert instructions_one != instructions_two


@pytest.mark.requires_bwrap
def test_provider_manager_rehydrates_manifest_plan(persona_env: dict[str, Path]) -> None:
    _seed_memory(persona_env)
    expected = compose_persona_plan(
        "claude-rehydrate",
        "claude_code",
        "maker",
        ContextPolicy(scope="persona"),
        persona_env["cwd"],
    )
    provider = ProviderManager().construct_provider(
        "claude_code", "claude-rehydrate", "session", "window", "maker"
    )
    assert getattr(provider, "_persona_plan", None) == expected


def test_no_manifest_lookup_does_not_create_persona_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert load_persona_plan("ordinary-terminal") is None
    assert not (runtime / "cao-personas").exists()


def test_ordinary_rehydration_without_runtime_dir_uses_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    assert load_persona_plan("ordinary-terminal") is None
    provider = ProviderManager().construct_provider(
        "claude_code", "ordinary-terminal", "session", "window", "maker"
    )
    assert getattr(provider, "_persona_plan", None) is None
    reap_persona_generations("ordinary-terminal")


def test_unknown_codex_resolution_without_runtime_dir_uses_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    production = tmp_path / "production-codex"
    production.mkdir()
    monkeypatch.setattr(
        persona_context,
        "provider_home",
        lambda provider: ProviderHome(provider, "production", production),
    )

    assert resolve_codex_home("unknown-terminal") == production


def test_context_policy_schema_accepts_contract_and_rejects_unknown_key() -> None:
    valid = {
        "name": "persona",
        "description": "persona",
        "contextPolicy": {
            "scope": "persona",
            "memoryTypes": ["feedback", "project"],
            "memoryNames": ["known"],
            "globalClaudeMd": False,
            "extraLeaves": [],
        },
        "container": {"path_maps": [{"host": "/host", "guest": "/guest"}]},
        "protected": True,
        "provider_init_timeout": 120,
    }
    assert _validate_frontmatter(valid) == []
    invalid = json.loads(json.dumps(valid))
    invalid["contextPolicy"]["unknown"] = True
    assert any("unknown" in message for message in _validate_frontmatter(invalid))


def test_unknown_manifest_version_rejected(persona_env: dict[str, Path]) -> None:
    _seed_memory(persona_env)
    plan = compose_persona_plan(
        "codex-version",
        "codex",
        "reviewer",
        ContextPolicy(scope="persona"),
        persona_env["cwd"],
    )
    manifest_path = plan.generation_dir / "persona-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["manifest_version"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PersonaContextError, match="version_unsupported"):
        load_persona_plan("codex-version")


def test_sabotaged_runtime_root_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    with pytest.raises(PersonaContextError, match="ownership_or_mode_invalid"):
        compose_persona_plan(
            "terminal-bad-root",
            "codex",
            "maker",
            ContextPolicy(scope="persona"),
            cwd,
        )


# ---------------------------------------------------------------------------
# F431 (issue #286): config.toml path-valued keys must be materialized into the
# isolated persona codex-home, or codex dies at launch resolving a relative
# path against CODEX_HOME. See persona_context.CODEX_CONFIG_FILE_KEYS.
# ---------------------------------------------------------------------------


def _write_codex_config_with(env: dict[str, Path], body: str) -> None:
    if not env["cwd"].exists():
        env["cwd"].mkdir(parents=True)
    (env["codex"] / "config.toml").write_text(body, encoding="utf-8")


def test_codex_relative_model_instructions_file_materialized(
    persona_env: dict[str, Path],
) -> None:
    """A relative model_instructions_file is copied into the persona home so the
    unchanged config value resolves against CODEX_HOME at launch (F431)."""
    (persona_env["codex"] / "gpt-unrestricted.md").write_text(
        "# custom system prompt\n", encoding="utf-8"
    )
    _write_codex_config_with(
        persona_env,
        'model = "gpt-test"\nmodel_instructions_file = "./gpt-unrestricted.md"\n',
    )
    plan = compose_persona_plan(
        "codex-instr", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
    )
    assert plan.codex_home is not None
    materialized = plan.codex_home / "gpt-unrestricted.md"
    assert materialized.is_file() and not materialized.is_symlink()
    assert materialized.read_text(encoding="utf-8") == "# custom system prompt\n"
    assert stat.S_IMODE(materialized.stat().st_mode) == 0o600
    # The rewritten config keeps the original relative value verbatim.
    config = (plan.codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'model_instructions_file = "./gpt-unrestricted.md"' in config


def test_codex_bare_relative_instructions_file_materialized(
    persona_env: dict[str, Path],
) -> None:
    """A bare filename (no ./) is treated as relative to the codex home too."""
    (persona_env["codex"] / "prompt.md").write_text("bare\n", encoding="utf-8")
    _write_codex_config_with(
        persona_env, 'model = "gpt-test"\nmodel_instructions_file = "prompt.md"\n'
    )
    plan = compose_persona_plan(
        "codex-bare", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
    )
    assert (plan.codex_home / "prompt.md").read_text(encoding="utf-8") == "bare\n"


def test_codex_nested_relative_instructions_file_materialized(
    persona_env: dict[str, Path],
) -> None:
    """A relative path with subdirectories is mirrored at the same relative path."""
    nested = persona_env["codex"] / "prompts" / "sys.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("nested\n", encoding="utf-8")
    _write_codex_config_with(
        persona_env, 'model = "gpt-test"\nmodel_instructions_file = "prompts/sys.md"\n'
    )
    plan = compose_persona_plan(
        "codex-nested", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
    )
    copied = plan.codex_home / "prompts" / "sys.md"
    assert copied.is_file() and copied.read_text(encoding="utf-8") == "nested\n"


def test_codex_experimental_instructions_file_alias_materialized(
    persona_env: dict[str, Path],
) -> None:
    """The deprecated experimental_instructions_file alias is covered too."""
    (persona_env["codex"] / "legacy.md").write_text("legacy\n", encoding="utf-8")
    _write_codex_config_with(
        persona_env, 'model = "gpt-test"\nexperimental_instructions_file = "./legacy.md"\n'
    )
    plan = compose_persona_plan(
        "codex-legacy", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
    )
    assert (plan.codex_home / "legacy.md").read_text(encoding="utf-8") == "legacy\n"


def test_codex_compact_prompt_file_materialized(persona_env: dict[str, Path]) -> None:
    (persona_env["codex"] / "compact.md").write_text("compact\n", encoding="utf-8")
    _write_codex_config_with(
        persona_env,
        'model = "gpt-test"\nexperimental_compact_prompt_file = "./compact.md"\n',
    )
    plan = compose_persona_plan(
        "codex-compact", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
    )
    assert (plan.codex_home / "compact.md").read_text(encoding="utf-8") == "compact\n"


def test_codex_absolute_instructions_file_not_copied_when_present(
    persona_env: dict[str, Path],
) -> None:
    """Absolute values resolve identically everywhere; codex reads them directly,
    so nothing is materialized — but a missing absolute target still fails."""
    abs_file = persona_env["codex"] / "abs.md"
    abs_file.write_text("abs\n", encoding="utf-8")
    _write_codex_config_with(
        persona_env, f'model = "gpt-test"\nmodel_instructions_file = "{abs_file}"\n'
    )
    plan = compose_persona_plan(
        "codex-abs", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
    )
    # No relative mirror is created; the absolute target is used in place.
    assert not (plan.codex_home / "abs.md").exists()


def test_codex_missing_relative_instructions_file_fails_loudly(
    persona_env: dict[str, Path],
) -> None:
    """A referenced-but-missing file is a hard error naming the path, not a skip."""
    _write_codex_config_with(
        persona_env, 'model = "gpt-test"\nmodel_instructions_file = "./nope.md"\n'
    )
    with pytest.raises(PersonaContextError, match="persona_codex_config_file_missing.*nope.md"):
        compose_persona_plan(
            "codex-missing", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
        )
    # Composition failed atomically: no persona tree is left behind.
    assert resolve_codex_home("codex-missing") != (
        persona_env["runtime"] / "cao-personas" / "codex-missing"
    )


def test_codex_missing_absolute_instructions_file_fails_loudly(
    persona_env: dict[str, Path],
) -> None:
    missing = persona_env["home"] / "gone.md"
    _write_codex_config_with(
        persona_env, f'model = "gpt-test"\nmodel_instructions_file = "{missing}"\n'
    )
    with pytest.raises(PersonaContextError, match="persona_codex_config_file_missing"):
        compose_persona_plan(
            "codex-absmiss", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
        )


def test_codex_parent_escape_instructions_file_rejected(
    persona_env: dict[str, Path],
) -> None:
    """A relative value with .. must be rejected before any filesystem write."""
    _write_codex_config_with(
        persona_env, 'model = "gpt-test"\nmodel_instructions_file = "../escape.md"\n'
    )
    with pytest.raises(PersonaContextError, match="persona_codex_config_path_escape"):
        compose_persona_plan(
            "codex-escape", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
        )


def test_codex_config_without_path_keys_unaffected(persona_env: dict[str, Path]) -> None:
    """The default fixture config (no path keys) composes exactly as before."""
    persona_env["cwd"].mkdir(parents=True)
    plan = compose_persona_plan(
        "codex-plain", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
    )
    config = (plan.codex_home / "config.toml").read_text(encoding="utf-8")
    assert config.startswith("project_doc_max_bytes = 0\n")
    # No stray files materialized beyond the known codex-home contents.
    names = sorted(p.name for p in plan.codex_home.iterdir())
    assert names == ["auth.json", "config.toml"]


# ---------------------------------------------------------------------------
# F431 round 2 gate remediation:
#   BLOCKER-1  model_catalog_json is a fourth top-level startup-read file path.
#   SHOULD-1   nested relative input path keys -> one degraded warning.
#   SHOULD-2   symlink source rejection must test the UNresolved candidate.
# ---------------------------------------------------------------------------

# A minimal catalog with one model entry. Real codex validates content further
# (17-field ModelInfo); the portable assertions below only need a present file,
# and the optional codex smoke only asserts the F431 "os error 2" class is gone.
_CATALOG_JSON = '{"models": [{"id": "gpt-test"}]}\n'


def test_codex_model_catalog_json_materialized(persona_env: dict[str, Path]) -> None:
    """BLOCKER-1: a relative model_catalog_json is materialized into the persona
    home so codex's startup config load resolves it against CODEX_HOME (F431)."""
    (persona_env["codex"] / "catalog.json").write_text(_CATALOG_JSON, encoding="utf-8")
    _write_codex_config_with(
        persona_env, 'model = "gpt-test"\nmodel_catalog_json = "./catalog.json"\n'
    )
    plan = compose_persona_plan(
        "codex-catalog", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
    )
    assert plan.codex_home is not None
    materialized = plan.codex_home / "catalog.json"
    assert materialized.is_file() and not materialized.is_symlink()
    assert materialized.read_text(encoding="utf-8") == _CATALOG_JSON
    assert stat.S_IMODE(materialized.stat().st_mode) == 0o600
    # Config keeps the original relative value; codex resolves it under CODEX_HOME.
    config = (plan.codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'model_catalog_json = "./catalog.json"' in config


def test_codex_model_catalog_json_in_covered_keys() -> None:
    """Key-coverage guard: model_catalog_json must stay in the materialized set
    (BLOCKER-1 would regress silently if it were dropped from the tuple)."""
    assert "model_catalog_json" in persona_context.CODEX_CONFIG_FILE_KEYS


def test_codex_missing_model_catalog_json_fails_loudly(persona_env: dict[str, Path]) -> None:
    """A referenced-but-missing catalog is a hard error naming the path, matching
    the reviewer's reproduced 'failed to load configuration' launch class."""
    _write_codex_config_with(
        persona_env, 'model = "gpt-test"\nmodel_catalog_json = "./catalog.json"\n'
    )
    with pytest.raises(
        PersonaContextError, match="persona_codex_config_file_missing.*catalog.json"
    ):
        compose_persona_plan(
            "codex-catmiss",
            "codex",
            "reviewer",
            ContextPolicy(scope="persona"),
            persona_env["cwd"],
        )


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex binary not available")
def test_codex_persona_config_catalog_resolves_placeholder(persona_env: dict[str, Path]) -> None:
    """BLOCKER-1 production-shape check (no real IO — unit tier): after
    compose_persona_plan materializes the catalog, the persona config.toml keeps
    the relative value AND the referenced file exists at the exact relative path
    codex would resolve under CODEX_HOME. This is what eliminates the reviewer's
    reproduced 'No such file or directory (os error 2)' launch failure; a live
    `codex features list` smoke would be real IO and is barred from the unit tier
    (F254 G2), so we assert the resolvable on-disk state instead."""
    (persona_env["codex"] / "catalog.json").write_text(_CATALOG_JSON, encoding="utf-8")
    _write_codex_config_with(
        persona_env, 'model = "gpt-test"\nmodel_catalog_json = "./catalog.json"\n'
    )
    plan = compose_persona_plan(
        "codex-resolves", "codex", "reviewer", ContextPolicy(scope="persona"), persona_env["cwd"]
    )
    config = (plan.codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'model_catalog_json = "./catalog.json"' in config
    # The exact path codex resolves the relative value to under CODEX_HOME.
    resolved = plan.codex_home / "catalog.json"
    assert resolved.is_file() and resolved.read_text(encoding="utf-8") == _CATALOG_JSON


def test_codex_relative_symlink_source_rejected(persona_env: dict[str, Path]) -> None:
    """SHOULD-2: an in-home symlink SOURCE for a materialized key is rejected. The
    check must run on the UNresolved candidate (resolve() would dereference the
    link and hide it)."""
    real_target = persona_env["codex"] / "real-prompt.md"
    real_target.write_text("real\n", encoding="utf-8")
    link = persona_env["codex"] / "link-prompt.md"
    link.symlink_to(real_target)  # in-home symlink -> in-home regular file
    _write_codex_config_with(
        persona_env, 'model = "gpt-test"\nmodel_instructions_file = "./link-prompt.md"\n'
    )
    with pytest.raises(
        PersonaContextError, match="persona_codex_config_symlink_source.*link-prompt.md"
    ):
        compose_persona_plan(
            "codex-symlink",
            "codex",
            "reviewer",
            ContextPolicy(scope="persona"),
            persona_env["cwd"],
        )


def test_codex_nested_relative_agent_config_warns(
    persona_env: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """SHOULD-1: a nested relative agents.<name>.config_file emits ONE degraded
    warning naming key+value and is NOT fatal (composition still succeeds)."""
    _write_codex_config_with(
        persona_env,
        'model = "gpt-test"\n[agents.test]\nconfig_file = "./role.toml"\n',
    )
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.utils.persona_context"):
        plan = compose_persona_plan(
            "codex-nestedwarn",
            "codex",
            "reviewer",
            ContextPolicy(scope="persona"),
            persona_env["cwd"],
        )
    assert plan.codex_home is not None  # non-fatal: composition succeeded
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "persona_codex_config_nested_relative_unsupported" in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "agents.test.config_file" in warnings[0].getMessage()
    assert "./role.toml" in warnings[0].getMessage()
    # The nested file is NOT materialized (unsupported contract).
    assert not (plan.codex_home / "role.toml").exists()


def test_codex_nested_relative_skill_and_provider_warn(
    persona_env: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """SHOULD-1: skills.config.<n>.path and model_providers.<id>.auth.cwd nested
    relative values are detected in the single warning."""
    _write_codex_config_with(
        persona_env,
        'model = "gpt-test"\n'
        '[[skills.config]]\npath = "./skills/foo"\n'
        '[model_providers.acme.auth]\ncwd = "./auth-wd"\n',
    )
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.utils.persona_context"):
        compose_persona_plan(
            "codex-nested2",
            "codex",
            "reviewer",
            ContextPolicy(scope="persona"),
            persona_env["cwd"],
        )
    msgs = [
        r.getMessage()
        for r in caplog.records
        if "persona_codex_config_nested_relative_unsupported" in r.getMessage()
    ]
    assert len(msgs) == 1, [r.getMessage() for r in caplog.records]
    assert "skills.config.0.path" in msgs[0]
    assert "model_providers.acme.auth.cwd" in msgs[0]


def test_codex_nested_absolute_paths_do_not_warn(
    persona_env: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """SHOULD-1: absolute nested values relocate correctly and must NOT warn."""
    _write_codex_config_with(
        persona_env,
        'model = "gpt-test"\n[agents.test]\nconfig_file = "/etc/codex/role.toml"\n',
    )
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.utils.persona_context"):
        compose_persona_plan(
            "codex-absnested",
            "codex",
            "reviewer",
            ContextPolicy(scope="persona"),
            persona_env["cwd"],
        )
    assert not [
        r
        for r in caplog.records
        if "persona_codex_config_nested_relative_unsupported" in r.getMessage()
    ]
