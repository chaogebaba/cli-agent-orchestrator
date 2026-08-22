"""Smoke-tier test tagging (fx147, D2).

Applies the ``smoke`` marker to one representative test per major subsystem.
Selection criterion: runs in < 200 ms solo, exercises the module's core
happy path, no I/O or subprocess.

Registered via the ``pytest_plugins`` tuple in ``test/conftest.py``.
"""

import pytest

# Node IDs of the smoke-tier representatives. Each must:
# - Run in < 200 ms solo
# - Exercise the module's core happy path
# - No real I/O or subprocess
_SMOKE_TESTS: set[str] = {
    # services — session_service
    "test/services/test_session_service.py::test_canonical_session_env_uses_working_directory",
    # services — terminal_service
    "test/services/test_terminal_service.py::TestPurgeStaleTerminalRecords::test_deletes_records_when_backend_window_is_missing",
    # services — fifo_reader
    "test/services/test_fifo_reader.py::TestStopReader::test_unlinks_stale_fifo_without_in_memory_reader",
    # services — draft_guard
    "test/services/test_draft_guard.py::test_preserve_logs_clears_and_restores",
    # services — inbox_service
    "test/services/test_inbox_service.py::TestDeliverPending::test_draft_guard_deferral_counts_attempted_only_and_notifies_caller_once",
    # api — agui emit_ui
    "test/api/test_agui_emit_ui.py::test_emit_ui_publishes_generative_ui_intent",
    # api — agui default_on_smoke
    "test/api/test_agui_default_on_smoke.py::test_single_flag_drives_lifecycle_event_onto_the_stream",
    # providers — codex (unit)
    "test/providers/test_base_provider.py::TestBaseProvider::test_init",
    # providers — base_provider
    "test/providers/test_base_provider.py::TestBaseProvider::test_apply_skill_prompt_appends",
    # mcp_server — assign
    "test/mcp_server/test_assign.py::TestCreateTerminalProviderResolution::test_existing_session_respects_child_profile_provider",
    # mcp_server — send_message
    "test/mcp_server/test_assign.py::TestCreateTerminalProviderResolution::test_deferred_assign_threads_barrier_in_create_body",
    # models — kiro_agent
    "test/models/test_kiro_agent.py::TestKiroAgentConfigPermissions::test_permissions_serialized_when_set",
    # models — kiro_engine
    "test/models/test_kiro_engine.py::test_resolve_kiro_engine_uses_creation_precedence[None-None-v2]",
    # clients — database
    "test/clients/test_database.py::TestTerminalOperations::test_create_terminal",
    # helpers — fake_clock
    "test/helpers/test_fake_clock.py::TestFakeClockBasics::test_initial_value",
    # utils
    "test/utils/test_workflow_events.py::test_run_event_terminal_regardless_of_state_field",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    smoke_mark = pytest.mark.smoke
    for item in items:
        if item.nodeid in _SMOKE_TESTS:
            item.add_marker(smoke_mark)
