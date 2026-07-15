"""Load-bearing WPM4-E fork ergonomics acceptance tests."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.services.fork_context_service import ForkContextError, mark_ready


ROW = {
    "name": "base",
    "kind": "base",
    "provider": "codex",
    "session_uuid": "11111111-1111-4111-8111-111111111111",
    "cwd": "/repo",
    "agent_profile": "developer",
    "git_sha": "a" * 40,
    "dirty_hashes": "{}",
}


def _assign_with_default(
    monkeypatch, default, *, resolution=ROW, fork_from=None, changed=(),
):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with ExitStack() as stack:
        configured = stack.enter_context(
            patch.object(server, "_configured_default_fork_base", return_value=default)
        )
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.fork_context_service.resolve_base",
                side_effect=resolution if isinstance(resolution, Exception) else None,
                return_value=None if isinstance(resolution, Exception) else resolution,
            )
        )
        stack.enter_context(patch.object(server, "resolve_provider", return_value="codex"))
        stack.enter_context(
            patch(
                "pathlib.Path.glob",
                return_value=[SimpleNamespace(name=f"rollout-{ROW['session_uuid']}.jsonl")],
            )
        )
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.fork_context_service.staleness",
                return_value=(list(changed), "[STALE]" if changed else "[FRESH]"),
            )
        )
        stack.enter_context(patch.object(server, "strict_supervisor_cwd", return_value="/cold"))
        create = stack.enter_context(
            patch.object(server, "_create_terminal", return_value=("feed1234", "codex"))
        )
        result = server._assign_impl("developer", "task", fork_from=fork_from)
    return result, create, configured


def test_e2_default_base_creates_fork_context(monkeypatch):
    result, create, _ = _assign_with_default(monkeypatch, "base")

    assert result["success"] is True
    context = create.call_args.kwargs["fork_context"]
    assert context is not None
    assert (context.mode, context.base_name) == ("fork", "base")


def test_e1_stale_fork_is_deferred_with_refresh_base(monkeypatch):
    result, create, _ = _assign_with_default(
        monkeypatch, "base", changed=("token.txt",)
    )

    assert result["success"] is True
    assert create.call_args.kwargs["refresh_base_name"] == "base"
    assert create.call_args.kwargs["fork_context"].initial_preamble == "[STALE]"


def test_e2_explicit_cold_and_absent_key_remain_cold(monkeypatch):
    result, create, configured = _assign_with_default(
        monkeypatch, "base", fork_from="cold"
    )
    assert result["success"] is True
    configured.assert_not_called()
    assert create.call_args.kwargs["fork_context"] is None

    result, create, _ = _assign_with_default(monkeypatch, None)
    assert result["success"] is True
    assert create.call_args.kwargs["fork_context"] is None


def test_e2_retired_default_falls_back_cold_with_warning(monkeypatch):
    result, create, _ = _assign_with_default(
        monkeypatch, "retired", resolution=ForkContextError("base_name_unknown")
    )

    assert result["success"] is True
    assert create.call_args.kwargs["fork_context"] is None
    assert create.call_args.kwargs["initial_message"].startswith(
        "[COLD-FALLBACK] configured default fork base 'retired' is unavailable"
    )


def test_e2_anchor_default_falls_back_without_refresh_target(monkeypatch):
    result, create, _ = _assign_with_default(
        monkeypatch, "root", resolution=ForkContextError("anchor_not_forkable:root")
    )

    assert result["success"] is True
    assert create.call_args.kwargs["fork_context"] is None
    assert "anchor_not_forkable:root" in create.call_args.kwargs["initial_message"]


def test_e2_mark_ready_rejects_reserved_cold_before_terminal_lookup():
    try:
        mark_ready("missing", "cold", None)
    except ForkContextError as exc:
        assert exc.code == "base_name_reserved:cold"
    else:  # pragma: no cover - assertion spelling keeps the typed code visible
        raise AssertionError("reserved cold name was accepted")
