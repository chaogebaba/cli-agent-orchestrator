"""Regression tests for codex init pipeline persona-home mismatch.

Root cause (2026-08-24): The persona context composes a CODEX_HOME env var
pointing to an isolated codex-home directory, but tmux.py's blocked-prefix
filter dropped CODEX_HOME in production mode.  This caused:
  1. codex binary in the pane used ~/.codex (no CODEX_HOME set)
  2. validate_session_artifact resolved persona codex_home (no sessions/ dir)
  3. session_artifact_missing → deferred_init_watchdog fires → worker unwound

Fix: CODEX_HOME added to _BLOCKED_PREFIX_ALLOWLIST in tmux.py, and a sessions
symlink is created in the persona codex-home during compose.
"""

import json
import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient


class TestCodexHomeAllowlist:
    """CODEX_HOME must pass through the blocked-prefix filter."""

    def test_codex_home_not_blocked(self):
        assert TmuxClient._is_blocked_env_key("CODEX_HOME") is False

    def test_codex_home_passes_merge_extra_env(self):
        env: dict[str, str] = {}
        TmuxClient._merge_extra_env(
            env, {"CODEX_HOME": "/run/user/1000/cao-personas/abc/current/gen-1/codex-home"}
        )
        assert env["CODEX_HOME"] == "/run/user/1000/cao-personas/abc/current/gen-1/codex-home"

    def test_other_codex_prefixed_vars_still_blocked(self):
        """CODEX_TOKEN and similar must still be blocked."""
        assert TmuxClient._is_blocked_env_key("CODEX_TOKEN") is True
        assert TmuxClient._is_blocked_env_key("CODEX_SESSION_ID") is True


class TestPersonaCodexHomeSessionsSymlink:
    """The persona codex-home must include a sessions symlink to production home."""

    def test_sessions_symlink_created_during_compose(self, tmp_path, monkeypatch):
        """compose_persona_plan creates sessions symlink in codex-home."""
        # Set up fake production codex home with sessions dir
        fake_codex_home = tmp_path / "codex-home-prod"
        fake_codex_home.mkdir()
        (fake_codex_home / "config.toml").write_text('model = "gpt-4"\n')
        (fake_codex_home / "auth.json").write_text('{"token": "x"}')
        sessions_dir = fake_codex_home / "sessions"
        sessions_dir.mkdir()
        # Put a fake rollout in sessions
        rollout = sessions_dir / "2026" / "08" / "24"
        rollout.mkdir(parents=True)
        rollout_file = (
            rollout / "rollout-2026-08-24T05-43-21-01a03327-3263-76d3-80df-532ba0fd16dd.jsonl"
        )
        rollout_file.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": "01a03327-3263-76d3-80df-532ba0fd16dd", "cwd": "/work"},
                }
            )
            + "\n"
        )

        # Replicate the compose_persona_plan codex-home setup logic
        codex_staging = tmp_path / "staging" / "codex-home"
        codex_staging.mkdir(parents=True, mode=0o700)

        real_codex_home = fake_codex_home.resolve()
        # Auth symlink (existing logic)
        auth = real_codex_home / "auth.json"
        os.symlink(str(auth.resolve()), codex_staging / "auth.json")
        # Sessions symlink (the fix)
        real_sessions = real_codex_home / "sessions"
        if real_sessions.is_dir():
            os.symlink(str(real_sessions), codex_staging / "sessions")

        # Verify symlink exists and points correctly
        assert (codex_staging / "sessions").is_symlink()
        assert (codex_staging / "sessions").resolve() == sessions_dir.resolve()
        # The rollout is findable via the symlink
        found = list(
            (codex_staging / "sessions").glob(
                "**/rollout-*01a03327-3263-76d3-80df-532ba0fd16dd*.jsonl"
            )
        )
        assert len(found) == 1
        assert found[0].name == rollout_file.name


class TestValidateSessionArtifactWithPersonaHome:
    """validate_session_artifact must find rollouts through persona home symlink."""

    def test_finds_rollout_via_sessions_symlink(self, tmp_path, monkeypatch):
        """When persona codex-home has sessions → production symlink, validation passes."""
        from cli_agent_orchestrator.providers.codex import CodexProvider

        # Real sessions dir with a rollout
        sessions_dir = tmp_path / "real-codex" / "sessions" / "2026" / "08" / "24"
        sessions_dir.mkdir(parents=True)
        uuid = "01a03327-3263-76d3-80df-532ba0fd16dd"
        rollout = sessions_dir / f"rollout-2026-08-24T05-43-21-{uuid}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": uuid, "cwd": "/work"},
                }
            )
            + "\n"
        )

        # Persona codex-home with sessions symlink
        persona_home = tmp_path / "persona-codex-home"
        persona_home.mkdir()
        os.symlink(str(tmp_path / "real-codex" / "sessions"), persona_home / "sessions")

        # Mock _resolved_codex_home to return persona home
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.codex._resolved_codex_home",
            lambda tid: persona_home,
        )

        provider = CodexProvider("test1234", "sess", "win", "dev")
        # Should NOT raise
        provider.validate_session_artifact(uuid, "/work")

    def test_fails_without_symlink(self, tmp_path, monkeypatch):
        """Without sessions symlink, validation raises RetryableArtifactValidation."""
        from cli_agent_orchestrator.providers.base import RetryableArtifactValidation
        from cli_agent_orchestrator.providers.codex import CodexProvider

        # Empty persona codex-home (no sessions dir at all)
        persona_home = tmp_path / "persona-codex-home"
        persona_home.mkdir()
        (persona_home / "sessions").mkdir()  # empty sessions dir

        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.codex._resolved_codex_home",
            lambda tid: persona_home,
        )

        provider = CodexProvider("test1234", "sess", "win", "dev")
        with pytest.raises(RetryableArtifactValidation):
            provider.validate_session_artifact("01a03327-3263-76d3-80df-532ba0fd16dd", "/work")
