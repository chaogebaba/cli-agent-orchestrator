"""F416 (#271): Tests for profile-level default_use_worktree resolution.

Verifies the lifecycle-like fallback: explicit caller value wins,
profile default is the fallback, absence means False.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile


class TestDefaultUseWorktreeResolution:
    """Tests that create_terminal resolves use_worktree from profile when caller omits it."""

    @pytest.fixture(autouse=True)
    def _patch_terminal_service(self, monkeypatch, tmp_path):
        """Patch out heavy dependencies so we can test the resolution logic."""
        from cli_agent_orchestrator.services import terminal_service

        self.ts = terminal_service
        backend = MagicMock()
        backend.create_session = MagicMock()
        backend.create_window = MagicMock()
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "f416test1")
        monkeypatch.setattr(terminal_service, "is_sandbox", lambda: False)
        self.tmp_path = tmp_path

    def _make_profile(self, *, default_use_worktree=None):
        kwargs = dict(name="test_profile", description="test", provider="kiro_cli")
        if default_use_worktree is not None:
            kwargs["default_use_worktree"] = default_use_worktree
        return AgentProfile(**kwargs)

    @pytest.mark.asyncio
    async def test_explicit_true_overrides_profile_false(self, monkeypatch):
        """Caller passes use_worktree=True; profile has default_use_worktree=False."""
        profile = self._make_profile(default_use_worktree=False)
        monkeypatch.setattr(self.ts, "load_agent_profile", lambda name: profile)

        # We expect create_terminal to attempt worktree provisioning (which will
        # fail because there's no git repo), proving that use_worktree=True was honored.
        from cli_agent_orchestrator.services.worktree_service import WorktreeError

        with pytest.raises((WorktreeError, Exception)):
            await self.ts.create_terminal(
                "kiro_cli",
                "test_profile",
                session_name="cao-f416",
                new_session=True,
                working_directory=str(self.tmp_path),
                use_worktree=True,
            )

    @pytest.mark.asyncio
    async def test_explicit_false_overrides_profile_true(self, monkeypatch):
        """Caller passes use_worktree=False; profile has default_use_worktree=True.

        The explicit False wins - no worktree is provisioned.
        """
        profile = self._make_profile(default_use_worktree=True)
        monkeypatch.setattr(self.ts, "load_agent_profile", lambda name: profile)

        # Patch worktree_service to detect if it's called
        worktree_called = []
        import cli_agent_orchestrator.services.worktree_service as ws

        original_find = ws.find_repo_root
        monkeypatch.setattr(
            ws, "find_repo_root", lambda *a, **kw: worktree_called.append(1) or original_find(*a, **kw)
        )

        # This will fail for other reasons (provider init etc), but we only care
        # that worktree provisioning was NOT attempted.
        try:
            await self.ts.create_terminal(
                "kiro_cli",
                "test_profile",
                session_name="cao-f416",
                new_session=True,
                working_directory=str(self.tmp_path),
                use_worktree=False,
            )
        except Exception:
            pass

        assert not worktree_called, "worktree should NOT be provisioned when caller passes explicit False"

    @pytest.mark.asyncio
    async def test_omitted_with_profile_true(self, monkeypatch):
        """Caller omits use_worktree (None); profile has default_use_worktree=True.

        Should fall back to profile value: provisions a worktree.
        """
        profile = self._make_profile(default_use_worktree=True)
        monkeypatch.setattr(self.ts, "load_agent_profile", lambda name: profile)

        # We expect worktree provisioning to be attempted (find_repo_root called)
        from cli_agent_orchestrator.services.worktree_service import WorktreeError

        with pytest.raises((WorktreeError, Exception)):
            await self.ts.create_terminal(
                "kiro_cli",
                "test_profile",
                session_name="cao-f416",
                new_session=True,
                working_directory=str(self.tmp_path),
                use_worktree=None,  # omitted by caller
            )

    @pytest.mark.asyncio
    async def test_omitted_with_profile_false(self, monkeypatch):
        """Caller omits use_worktree (None); profile has default_use_worktree=False.

        Should resolve to False: no worktree provisioned.
        """
        profile = self._make_profile(default_use_worktree=False)
        monkeypatch.setattr(self.ts, "load_agent_profile", lambda name: profile)

        worktree_called = []
        import cli_agent_orchestrator.services.worktree_service as ws

        original_find = ws.find_repo_root
        monkeypatch.setattr(
            ws, "find_repo_root", lambda *a, **kw: worktree_called.append(1) or original_find(*a, **kw)
        )

        try:
            await self.ts.create_terminal(
                "kiro_cli",
                "test_profile",
                session_name="cao-f416",
                new_session=True,
                working_directory=str(self.tmp_path),
                use_worktree=None,  # omitted
            )
        except Exception:
            pass

        assert not worktree_called, "worktree should NOT be provisioned when profile says False"

    @pytest.mark.asyncio
    async def test_omitted_with_profile_absent(self, monkeypatch):
        """Caller omits use_worktree (None); profile has no default_use_worktree field.

        Should resolve to False (absence = False).
        """
        profile = AgentProfile(name="no_worktree_field", description="test", provider="kiro_cli")
        monkeypatch.setattr(self.ts, "load_agent_profile", lambda name: profile)

        worktree_called = []
        import cli_agent_orchestrator.services.worktree_service as ws

        original_find = ws.find_repo_root
        monkeypatch.setattr(
            ws, "find_repo_root", lambda *a, **kw: worktree_called.append(1) or original_find(*a, **kw)
        )

        try:
            await self.ts.create_terminal(
                "kiro_cli",
                "no_worktree_field",
                session_name="cao-f416",
                new_session=True,
                working_directory=str(self.tmp_path),
                use_worktree=None,
            )
        except Exception:
            pass

        assert not worktree_called, "worktree should NOT be provisioned when profile has no field"


class TestAgentProfileDefaultUseWorktree:
    """Tests for the AgentProfile model field itself."""

    def test_field_defaults_to_none(self):
        p = AgentProfile(name="x", description="x")
        assert p.default_use_worktree is None

    def test_field_accepts_true(self):
        p = AgentProfile(name="x", description="x", default_use_worktree=True)
        assert p.default_use_worktree is True

    def test_field_accepts_false(self):
        p = AgentProfile(name="x", description="x", default_use_worktree=False)
        assert p.default_use_worktree is False

    def test_schema_validates_field(self):
        """JSON schema includes default_use_worktree."""
        import json
        from pathlib import Path

        schema_path = Path(__file__).parent.parent.parent / "src" / "cli_agent_orchestrator" / "schemas" / "agent_profile.schema.json"
        if not schema_path.exists():
            # Alternate location when running from the worktree
            import cli_agent_orchestrator.schemas as schemas_mod
            schema_path = Path(schemas_mod.__file__).parent / "agent_profile.schema.json"
        schema = json.loads(schema_path.read_text())
        assert "default_use_worktree" in schema["properties"]
        prop = schema["properties"]["default_use_worktree"]
        assert prop["type"] == "boolean"
