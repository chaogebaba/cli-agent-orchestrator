import asyncio
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.terminal_service import create_terminal


@pytest.fixture(autouse=True)
def _legacy_direct_create_is_not_a_seed_capability(monkeypatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_provider_class",
        lambda _name: type("Capability", (), {"supports_seed_resume_identity": False}),
    )


@pytest.mark.parametrize("path", ["relative/path", "/does/not/exist", "/tmp"])
def test_invalid_workdir_rejected_before_identifier_backend_or_db(path):
    with patch(
        "cli_agent_orchestrator.services.terminal_service.generate_terminal_id"
    ) as generate, patch(
        "cli_agent_orchestrator.services.terminal_service.get_backend"
    ) as backend:
        with patch(
            "cli_agent_orchestrator.services.terminal_service.db_create_terminal"
        ) as db_create:
            with pytest.raises(ValueError, match="invalid_working_directory"):
                asyncio.run(create_terminal("codex", "developer", working_directory=path))
    generate.assert_not_called()
    backend.assert_not_called()
    db_create.assert_not_called()


def test_regular_file_rejected_before_identifier_backend_or_db(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("x")
    with patch(
        "cli_agent_orchestrator.services.terminal_service.generate_terminal_id"
    ) as generate, patch(
        "cli_agent_orchestrator.services.terminal_service.get_backend"
    ) as backend, patch(
        "cli_agent_orchestrator.services.terminal_service.db_create_terminal"
    ) as db_create:
        with pytest.raises(ValueError, match="invalid_working_directory"):
            asyncio.run(
                create_terminal("codex", "developer", working_directory=str(file_path))
            )
    generate.assert_not_called()
    backend.assert_not_called()
    db_create.assert_not_called()


def test_explicit_workdir_is_canonicalized_before_backend(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    backend = __import__("unittest.mock").mock.MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.return_value = "window"
    with patch(
        "cli_agent_orchestrator.services.terminal_service.get_backend", return_value=backend
    ), patch(
        "cli_agent_orchestrator.services.terminal_service.load_agent_profile", side_effect=FileNotFoundError
    ), patch(
        "cli_agent_orchestrator.services.terminal_service.db_create_terminal"
    ), patch(
        "cli_agent_orchestrator.services.terminal_service.provider_manager.create_provider"
    ) as provider:
        provider.return_value.initialize = __import__("unittest.mock").mock.AsyncMock()
        asyncio.run(
            create_terminal("codex", "developer", session_name="cao-test", working_directory=str(alias))
        )
    assert backend.create_window.call_args.args[3] == str(target.resolve())
