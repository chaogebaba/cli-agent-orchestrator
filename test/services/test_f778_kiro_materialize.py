"""F778 (#635): on-demand kiro agent-JSON materialisation for composed spawns.

A position-composed assign (``agent_profile="dev", provider="kiro_cli"``)
synthesises the spawn name ``kiro_cli_dev`` in memory (D6/D7); ``cao install``
never wrote its agent JSON because the composed profile has no installed source
file. These tests cover the reusable writer factored out of ``install_agent``
and the byte-for-byte parity between the on-demand path and the install path.
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.services.install_service import (
    build_kiro_agent_config,
    materialize_kiro_agent_json,
    write_kiro_agent_file,
)


@pytest.fixture
def kiro_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Repoint the call-time + module-level dirs the kiro writer touches."""
    home = tmp_path / "cao-home"
    kiro_dir = tmp_path / "kiro-agents"
    context_dir = home / "agent-context"
    store_dir = home / "agent-store"
    for path in (home, kiro_dir, context_dir, store_dir):
        path.mkdir(parents=True, exist_ok=True)

    # Writer resolves kiro_agents_dir() at call time.
    monkeypatch.setenv("CAO_AGENTS_DIR", str(kiro_dir))
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    # _write_context_file reads the MODULE-LEVEL AGENT_CONTEXT_DIR.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
    )
    monkeypatch.setattr("cli_agent_orchestrator.constants.kiro_agents_dir", lambda: kiro_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", store_dir
    )
    return {
        "home": home,
        "kiro_dir": kiro_dir,
        "context_dir": context_dir,
        "store_dir": store_dir,
    }


def _kas_profile(name: str = "kiro_cli_dev") -> AgentProfile:
    return AgentProfile(
        name=name,
        description="Composed dev cell",
        provider="kiro_cli",
        role="developer",
        engine=KiroEngine.KAS,
        prompt="You are a dev worker.",
        mcpServers={
            "cao-mcp-server": {
                "type": "stdio",
                "command": "/usr/local/bin/cao-mcp-server",
                "args": [],
            }
        },
    )


class TestWriteKiroAgentFile:
    """The extracted writer produces a valid, atomic agent JSON."""

    def test_writes_json_named_by_safe_filename(self, kiro_paths):
        profile = _kas_profile()
        context_file = kiro_paths["context_dir"] / "kiro_cli_dev.md"
        context_file.write_text("persona", encoding="utf-8")

        path = write_kiro_agent_file(
            profile,
            context_file=context_file,
            allowed_tools=["fs_read"],
            safe_filename="kiro_cli_dev",
        )

        assert path == kiro_paths["kiro_dir"] / "kiro_cli_dev.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == "kiro_cli_dev"
        # KAS profile defaults the permissions block.
        assert data["permissions"]["rules"][0]["capability"] == "shell"
        # F118: identity env injected into every MCP entry.
        env = data["mcpServers"]["cao-mcp-server"]["env"]
        assert env["CAO_TERMINAL_ID"] == "${CAO_TERMINAL_ID}"

    def test_flattens_path_separator_in_name(self, kiro_paths):
        """A '/'-bearing name flattens to '__' in the on-disk filename."""
        profile = _kas_profile(name="team/dev")
        context_file = kiro_paths["context_dir"] / "ctx.md"
        context_file.write_text("persona", encoding="utf-8")

        # Caller flattens the filename; build_kiro_agent_config keeps profile.name.
        from cli_agent_orchestrator.utils.path_validation import flatten_path_separators

        safe = flatten_path_separators(profile.name)
        assert safe == "team__dev"
        path = write_kiro_agent_file(
            profile,
            context_file=context_file,
            allowed_tools=[],
            safe_filename=safe,
        )
        assert path.name == "team__dev.json"

    def test_kas_rejects_v2_only_fields(self, kiro_paths):
        profile = _kas_profile()
        profile.hooks = {"onStart": "echo hi"}
        context_file = kiro_paths["context_dir"] / "ctx.md"
        context_file.write_text("persona", encoding="utf-8")
        with pytest.raises(ValueError, match="toolsSettings or hooks"):
            build_kiro_agent_config(profile, context_file=context_file, allowed_tools=[])


class TestMaterializeIdempotent:
    """materialize_kiro_agent_json is idempotent (re-run = identical bytes)."""

    def test_rerun_produces_identical_bytes(self, kiro_paths):
        profile = _kas_profile()
        p1 = materialize_kiro_agent_json(
            profile, composed_source="---\nname: kiro_cli_dev\n---\nx\n"
        )
        first = p1.read_bytes()
        mtime1 = p1.stat().st_mtime_ns

        p2 = materialize_kiro_agent_json(
            profile, composed_source="---\nname: kiro_cli_dev\n---\nx\n"
        )
        assert p2 == p1
        assert p2.read_bytes() == first
        # Content is byte-identical across re-runs (idempotent).
        assert json.loads(p2.read_text()) == json.loads(p1.read_text())
        # mtime may advance (atomic replace), but content must not change.
        _ = mtime1


class TestInstallByteParity:
    """The extracted writer produces bytes identical to the install path.

    Installs a LEGACY kiro profile via install_agent (which now routes through
    write_kiro_agent_file) and then rebuilds the JSON independently via
    build_kiro_agent_config, asserting the two serialisations are byte-identical.
    This is the F778 parity guarantee: the on-demand path and the install path
    share one writer, so a composed spawn's JSON matches what install would write.
    """

    def _install_paths(self, monkeypatch, tmp_path):
        home = tmp_path / "cao-home"
        kiro_dir = tmp_path / "kiro-agents"
        context_dir = home / "agent-context"
        store_dir = home / "agent-store"
        provider_dir = tmp_path / "provider"
        for path in (home, kiro_dir, context_dir, store_dir, provider_dir):
            path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CAO_AGENTS_DIR", str(kiro_dir))
        monkeypatch.setenv("CAO_HOME_DIR", str(home))
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", kiro_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.constants.kiro_agents_dir", lambda: kiro_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.agent_context_dir", lambda: context_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", store_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", store_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            lambda: {"kiro_cli": str(provider_dir)},
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            lambda: [],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            lambda: [],
        )
        return {"kiro_dir": kiro_dir, "context_dir": context_dir, "provider_dir": provider_dir}

    def test_install_writer_matches_independent_build(self, monkeypatch, tmp_path):
        from cli_agent_orchestrator.services.install_service import install_agent
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
        from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

        paths = self._install_paths(monkeypatch, tmp_path)
        profile_md = paths["provider_dir"] / "legacy_kiro.md"
        profile_md.write_text(
            "---\n"
            "name: legacy_kiro\n"
            "description: Legacy kiro dev\n"
            "provider: kiro_cli\n"
            "role: developer\n"
            "engine: kas\n"
            "mcpServers:\n"
            "  cao-mcp-server:\n"
            "    type: stdio\n"
            "    command: /usr/local/bin/cao-mcp-server\n"
            "    args: []\n"
            "prompt: |\n  You are legacy.\n"
            "---\n"
            "Legacy body.\n",
            encoding="utf-8",
        )

        result = install_agent("legacy_kiro", "kiro_cli")
        assert result.success is True, result.message
        installed_bytes = (paths["kiro_dir"] / "legacy_kiro.json").read_bytes()

        # Rebuild the JSON independently through the same factored helpers,
        # pointing resources at the SAME context-file path the install wrote.
        profile = load_agent_profile("legacy_kiro")
        context_file = paths["context_dir"] / "legacy_kiro.md"
        mcp_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
        allowed = resolve_allowed_tools(profile.allowedTools, profile.role, mcp_names)
        config = build_kiro_agent_config(profile, context_file=context_file, allowed_tools=allowed)
        rebuilt = config.model_dump_json(indent=2, exclude_none=True).encode("utf-8")

        assert rebuilt == installed_bytes
