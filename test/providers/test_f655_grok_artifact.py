"""F655 (#510): grok_cli session-artifact validation for grok CLI 1.0.13.

grok 1.0.13 does not write ``chat_history.jsonl`` at session-create time — it
seeds the per-``<quote(cwd)>/<uuid>`` session directory with ``summary.json`` /
``prompt_context.json`` / ``system_prompt.txt`` / ``events.jsonl`` ~1s after
launch and defers ``chat_history.jsonl`` until the first completed turn (or, on
some hosts, not before the artifact-sync deadline at all). The old validator
required ``chat_history.jsonl`` non-empty and therefore made every grok spawn
expire the bounded wait. These tests pin the new contract: validate on the
session directory plus ANY non-empty seed artifact.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest

from cli_agent_orchestrator.providers.base import RetryableArtifactValidation
from cli_agent_orchestrator.providers.grok_cli import (
    GROK_SESSION_LIVENESS_ARTIFACTS,
    GrokCliProvider,
)

SESSION_UUID = "5eb6a098-9667-46a4-8c85-9cd487905933"


@pytest.fixture
def provider(tmp_path):
    """A GrokCliProvider whose ``~/.grok`` is redirected under tmp_path.

    ``validate_session_artifact`` resolves ``Path.home() / ".grok" / ...``; patch
    ``Path.home`` for the grok_cli module so the check reads an isolated tree and
    never the real user home.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path / "cao"),
        patch("cli_agent_orchestrator.providers.grok_cli.provider_home") as mock_plane,
        patch("cli_agent_orchestrator.providers.grok_cli.get_backend") as mock_backend,
        patch("cli_agent_orchestrator.providers.grok_cli.Path.home", return_value=fake_home),
    ):
        plane = MagicMock()
        plane.home = tmp_path / "cao" / "grok_plane"
        plane.home.mkdir(parents=True, exist_ok=True)
        plane.sessions = plane.home / "sessions"
        plane.sessions.mkdir(parents=True, exist_ok=True)
        plane.credential_path = plane.home / "auth.json"
        plane.credential_path.write_text("{}")
        mock_plane.return_value = plane

        backend_inst = MagicMock()
        backend_inst.get_pane_working_directory.return_value = "/data/cao-scratch/f655"
        mock_backend.return_value = backend_inst

        p = GrokCliProvider(
            terminal_id="test-f655-01",
            session_name="s1",
            window_name="w1",
            agent_profile="grok_dev",
        )
        # Expose the fake home so tests can build the expected session path.
        p._test_home = fake_home  # type: ignore[attr-defined]
        yield p


def _session_dir(provider, cwd: str, session_uuid: str = SESSION_UUID) -> Path:
    return (
        provider._test_home
        / ".grok"
        / "sessions"
        / quote(cwd, safe="")
        / session_uuid
    )


class TestF655GrokArtifactValidation:
    CWD = "/data/cao-scratch/f655"

    def test_missing_session_dir_is_retryable(self, provider):
        """No session directory yet → RetryableArtifactValidation (keep waiting)."""
        with pytest.raises(RetryableArtifactValidation):
            provider.validate_session_artifact(SESSION_UUID, self.CWD)

    def test_empty_session_dir_is_retryable(self, provider):
        """Directory exists but holds no non-empty seed artifact → retryable."""
        _session_dir(provider, self.CWD).mkdir(parents=True)
        with pytest.raises(RetryableArtifactValidation):
            provider.validate_session_artifact(SESSION_UUID, self.CWD)

    def test_zero_byte_artifacts_are_retryable(self, provider):
        """Seed files present but all zero-length (inert) → retryable."""
        d = _session_dir(provider, self.CWD)
        d.mkdir(parents=True)
        for name in GROK_SESSION_LIVENESS_ARTIFACTS:
            (d / name).write_text("")
        with pytest.raises(RetryableArtifactValidation):
            provider.validate_session_artifact(SESSION_UUID, self.CWD)

    def test_summary_json_alone_validates(self, provider):
        """F655 core: summary.json seeded at create-time is sufficient liveness,
        even though chat_history.jsonl is absent (grok 1.0.13 defers it)."""
        d = _session_dir(provider, self.CWD)
        d.mkdir(parents=True)
        (d / "summary.json").write_text('{"title": "x"}')
        # No chat_history.jsonl on disk — must still pass.
        assert not (d / "chat_history.jsonl").exists()
        provider.validate_session_artifact(SESSION_UUID, self.CWD)  # no raise

    @pytest.mark.parametrize("artifact", list(GROK_SESSION_LIVENESS_ARTIFACTS))
    def test_any_single_nonempty_artifact_validates(self, provider, artifact):
        """Each declared liveness artifact, present and non-empty, validates alone."""
        d = _session_dir(provider, self.CWD)
        d.mkdir(parents=True)
        (d / artifact).write_text("seed")
        provider.validate_session_artifact(SESSION_UUID, self.CWD)  # no raise

    def test_chat_history_still_validates(self, provider):
        """Backwards compat: a host that DOES seed chat_history.jsonl early still
        validates on it (no regression for the pre-1.0.13 layout)."""
        d = _session_dir(provider, self.CWD)
        d.mkdir(parents=True)
        (d / "chat_history.jsonl").write_text('{"type":"system"}\n')
        provider.validate_session_artifact(SESSION_UUID, self.CWD)  # no raise

    def test_wrong_uuid_dir_is_retryable(self, provider):
        """A seeded artifact under a DIFFERENT uuid does not satisfy this uuid."""
        other = _session_dir(provider, self.CWD, session_uuid="00000000-0000-0000-0000-000000000000")
        other.mkdir(parents=True)
        (other / "summary.json").write_text("{}")
        with pytest.raises(RetryableArtifactValidation):
            provider.validate_session_artifact(SESSION_UUID, self.CWD)
