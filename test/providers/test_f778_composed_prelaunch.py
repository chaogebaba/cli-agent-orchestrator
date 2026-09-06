"""F778 (#635): kiro prelaunch materialises the agent JSON for a composed spawn.

``_assert_kiro_identity_guard`` used to hard-fail when
``~/.kiro/agents/<spawn>.json`` was missing. For a POSITION-COMPOSED spawn name
(``kiro_cli_dev`` from ``assign(agent_profile="dev", provider="kiro_cli")``) the
JSON was never written because the composed profile has no installed source
file, so every dev+kiro_cli assign died at prelaunch. The guard now materialises
the JSON on demand from the composition; a genuine uninstalled-legacy name still
raises.
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider


def _write_composed_store(home: Path) -> None:
    """Seed a positions/ + overlays/ store with a ``dev`` position for kiro_cli."""
    store = home / "agent-store"
    positions = store / "positions"
    overlays = store / "overlays"
    positions.mkdir(parents=True, exist_ok=True)
    overlays.mkdir(parents=True, exist_ok=True)
    (positions / "dev.md").write_text(
        "---\n"
        "description: Dev position\n"
        "role: developer\n"
        "engine: kas\n"
        "providers:\n"
        "  - kiro_cli\n"
        "mcpServers:\n"
        "  cao-mcp-server:\n"
        "    type: stdio\n"
        "    command: cao-mcp-server\n"
        "    args: []\n"
        "---\n"
        "You are a dev worker.\n",
        encoding="utf-8",
    )
    (overlays / "kiro_cli.md").write_text(
        "---\nprovider: kiro_cli\n---\nKiro-specific guidance.\n",
        encoding="utf-8",
    )


@pytest.fixture
def composed_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "cao-home"
    kiro_dir = tmp_path / "kiro-agents"
    context_dir = home / "agent-context"
    store_dir = home / "agent-store"
    for path in (home, kiro_dir, context_dir, store_dir):
        path.mkdir(parents=True, exist_ok=True)
    _write_composed_store(home)

    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_AGENTS_DIR", str(kiro_dir))
    # Guard re-checks the MODULE-LEVEL KIRO_AGENTS_DIR; writer resolves
    # kiro_agents_dir() at call time. Point both at the same tmp dir.
    monkeypatch.setattr("cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("cli_agent_orchestrator.constants.kiro_agents_dir", lambda: kiro_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", store_dir
    )
    return {"home": home, "kiro_dir": kiro_dir, "context_dir": context_dir}


class TestComposedMaterialisesOnDemand:
    def test_composed_missing_json_is_written_and_guard_passes(self, composed_env):
        """kiro_cli_dev: missing JSON -> materialised from the dev position -> passes."""
        kiro_dir = composed_env["kiro_dir"]
        assert not (kiro_dir / "kiro_cli_dev.json").exists()

        provider = KiroCliProvider("ab12cd34", "sess", "win-0", "kiro_cli_dev")
        # Should NOT raise — the guard materialises the composed JSON.
        provider._assert_kiro_identity_guard()

        written = kiro_dir / "kiro_cli_dev.json"
        assert written.exists()
        data = json.loads(written.read_text(encoding="utf-8"))
        assert data["name"] == "kiro_cli_dev"
        # The dev position's cao-mcp-server survives composition into the JSON.
        assert "cao-mcp-server" in data["mcpServers"]

    def test_name_flattening_slash_to_double_underscore(self, composed_env):
        """A '/'-bearing profile name flattens to '__' in the on-disk JSON filename.

        Mirrors the install path: the context-file write REJECTS a separator-
        bearing name up front (security guard), so a '/'-bearing name never
        reaches the JSON sink in the normal flow. The flatten stays as
        defence-in-depth on the JSON sink itself, exercised here via
        write_kiro_agent_file with an already-flattened safe_filename.
        """
        from cli_agent_orchestrator.models.agent_profile import AgentProfile
        from cli_agent_orchestrator.models.kiro_engine import KiroEngine
        from cli_agent_orchestrator.services.install_service import write_kiro_agent_file
        from cli_agent_orchestrator.utils.path_validation import flatten_path_separators

        profile = AgentProfile(
            name="kiro_cli/dev",
            description="composed",
            provider="kiro_cli",
            role="developer",
            engine=KiroEngine.KAS,
            prompt="body",
        )
        context_file = composed_env["context_dir"] / "ctx.md"
        context_file.write_text("persona", encoding="utf-8")
        safe = flatten_path_separators(profile.name)
        assert safe == "kiro_cli__dev"
        path = write_kiro_agent_file(
            profile,
            context_file=context_file,
            allowed_tools=[],
            safe_filename=safe,
        )
        assert path.name == "kiro_cli__dev.json"


class TestLegacyStillRaises:
    def test_uninstalled_legacy_missing_json_raises(self, composed_env):
        """A genuine legacy name (not a <provider>_<position>) still fails loud."""
        provider = KiroCliProvider("ab12cd34", "sess", "win-0", "some_legacy_agent")
        with pytest.raises(RuntimeError, match="kiro base agent JSON missing"):
            provider._assert_kiro_identity_guard()

    def test_provider_prefix_but_unknown_position_raises(self, composed_env):
        """kiro_cli_<unknown>: prefix matches but no such position -> legacy fail."""
        provider = KiroCliProvider("ab12cd34", "sess", "win-0", "kiro_cli_nosuchpos")
        with pytest.raises(RuntimeError, match="kiro base agent JSON missing"):
            provider._assert_kiro_identity_guard()


class TestExistingJsonUntouched:
    def test_existing_json_not_modified(self, composed_env):
        """A pre-existing base JSON is left byte- and mtime-identical."""
        import os
        import time

        kiro_dir = composed_env["kiro_dir"]
        existing = kiro_dir / "kiro_cli_dev.json"
        original = json.dumps({"name": "kiro_cli_dev", "hand": "written"}, indent=2)
        existing.write_text(original, encoding="utf-8")
        # Backdate mtime so an accidental rewrite is detectable.
        old = time.time() - 1000
        os.utime(existing, (old, old))
        mtime_before = existing.stat().st_mtime_ns
        bytes_before = existing.read_bytes()

        provider = KiroCliProvider("ab12cd34", "sess", "win-0", "kiro_cli_dev")
        provider._assert_kiro_identity_guard()

        assert existing.read_bytes() == bytes_before
        assert existing.stat().st_mtime_ns == mtime_before
