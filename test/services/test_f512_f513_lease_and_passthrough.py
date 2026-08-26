"""F512 (#367) + F513 (#368) regression tests.

F512 — MCP delete_terminal must pass the API's 409 `detail` through verbatim
for every recognized cause (lease contention, rebind, cascade conflicts,
protection) instead of collapsing them all into the generic
"cleanup is pending" message. The generic message is reserved for a genuine
cleanup-deferral (empty / unrecognized detail).

F513 — the session-lifecycle exclusive lease is session-scoped, so a delete of
terminal X must not be permanently blocked by an unrelated terminal Y's
in-flight deferred init holding a shared lease. A would-be exclusive holder
now waits a bounded interval (delete.lifecycle_lease_wait_s) before surfacing
the 409, and succeeds if the contending shared holder releases within it.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

# ===========================================================================
# F512: MCP delete_terminal 409 detail passthrough
# ===========================================================================


def _mock_409(detail: str, *, raise_path: bool) -> MagicMock:
    """Build a mock response for a 409 with the given detail.

    raise_path=True routes through response.raise_for_status() -> HTTPError
    (the `except requests.HTTPError` branch); raise_path=False leaves
    raise_for_status a no-op so the in-band `status_code == 409` branch fires.
    """
    resp = MagicMock()
    resp.status_code = 409
    resp.json.return_value = {"detail": detail}
    resp.text = '{"detail": "%s"}' % detail
    if raise_path:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status = MagicMock()
    return resp


LEASE_AND_CONFLICT_CODES = [
    "resume_in_progress",
    "rebind_in_progress",
    "cascade_quiesce_unstable",
    "cascade_outside_caller_subtree",
]

PROTECTION_DETAILS = [
    "ready_base",
    "protected",
    "cascade_something",
    "subtree conflict",
]


@pytest.mark.parametrize("raise_path", [False, True])
@pytest.mark.parametrize("code", LEASE_AND_CONFLICT_CODES)
def test_mcp_delete_passes_lease_contention_detail_verbatim(code: str, raise_path: bool) -> None:
    """F512: lease/rebind/cascade codes reach the caller verbatim, NOT the
    generic 'cleanup is pending' message (either 409 branch)."""
    from cli_agent_orchestrator.mcp_server.server import delete_terminal as mcp_delete_terminal

    resp = _mock_409(code, raise_path=raise_path)

    with (
        patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http,
        patch("cli_agent_orchestrator.mcp_server.server._current_terminal_id", return_value=None),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"provider": "grok_cli"},
        ),
    ):
        mock_http.delete.return_value = resp
        result = mcp_delete_terminal(terminal_id="abcd1234", force=False, orphan=False)

    assert result["success"] is False
    # The real cause is surfaced verbatim.
    assert code in result["message"]
    # And it is NOT collapsed into the generic cleanup wording.
    assert "cleanup is pending" not in result["message"]


@pytest.mark.parametrize("raise_path", [False, True])
@pytest.mark.parametrize("detail", PROTECTION_DETAILS)
def test_mcp_delete_passes_protection_detail_verbatim(detail: str, raise_path: bool) -> None:
    """F512: protection/cascade conflicts remain verbatim (unchanged behavior)."""
    from cli_agent_orchestrator.mcp_server.server import delete_terminal as mcp_delete_terminal

    resp = _mock_409(detail, raise_path=raise_path)

    with (
        patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http,
        patch("cli_agent_orchestrator.mcp_server.server._current_terminal_id", return_value=None),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"provider": "grok_cli"},
        ),
    ):
        mock_http.delete.return_value = resp
        result = mcp_delete_terminal(terminal_id="abcd1234", force=False, orphan=False)

    assert result["success"] is False
    assert detail in result["message"]
    assert "cleanup is pending" not in result["message"]


@pytest.mark.parametrize("raise_path", [False, True])
def test_mcp_delete_genuine_cleanup_deferral_uses_generic_message(raise_path: bool) -> None:
    """F512: an actual cleanup-deferral (detail mentions 'cleanup deferred',
    not a passthrough code) still yields the provider-aware generic message."""
    from cli_agent_orchestrator.mcp_server.server import delete_terminal as mcp_delete_terminal

    resp = _mock_409("cleanup deferred for terminal 'abcd1234'", raise_path=raise_path)

    with (
        patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http,
        patch("cli_agent_orchestrator.mcp_server.server._current_terminal_id", return_value=None),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"provider": "kiro_cli"},
        ),
    ):
        mock_http.delete.return_value = resp
        result = mcp_delete_terminal(terminal_id="abcd1234", force=False, orphan=False)

    assert result["success"] is False
    assert "cleanup is pending" in result["message"]
    assert "kiro_cli" in result["message"]


@pytest.mark.parametrize("raise_path", [False, True])
def test_mcp_delete_empty_detail_falls_back_to_generic(raise_path: bool) -> None:
    """F512: an empty/unrecognized detail falls back to the generic message
    (never crashes, never fabricates a code)."""
    from cli_agent_orchestrator.mcp_server.server import delete_terminal as mcp_delete_terminal

    resp = _mock_409("", raise_path=raise_path)

    with (
        patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http,
        patch("cli_agent_orchestrator.mcp_server.server._current_terminal_id", return_value=None),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"provider": "kiro_cli"},
        ),
    ):
        mock_http.delete.return_value = resp
        result = mcp_delete_terminal(terminal_id="abcd1234", force=False, orphan=False)

    assert result["success"] is False
    assert "cleanup is pending" in result["message"]


def test_classify_delete_409_unit() -> None:
    """Direct unit test of the shared classifier."""
    from cli_agent_orchestrator.mcp_server.server import _classify_delete_409

    with patch(
        "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
        return_value={"provider": "codex"},
    ):
        # Passthrough code -> verbatim
        r = _classify_delete_409("resume_in_progress", "t1")
        assert r["success"] is False
        assert "resume_in_progress" in r["message"]
        assert "cleanup is pending" not in r["message"]

        # Genuine deferral -> generic provider-aware
        r = _classify_delete_409("cleanup deferred for terminal 't1'", "t1")
        assert "cleanup is pending" in r["message"]
        assert "codex" in r["message"]

        # Empty -> generic
        r = _classify_delete_409("", "t1")
        assert "cleanup is pending" in r["message"]


def test_mcp_delete_forwards_force_to_api() -> None:
    """F512: force is forwarded verbatim as ?force=true on the DELETE request."""
    from cli_agent_orchestrator.mcp_server.server import delete_terminal as mcp_delete_terminal

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"success": True}
    resp.raise_for_status = MagicMock()

    with (
        patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http,
        patch("cli_agent_orchestrator.mcp_server.server._current_terminal_id", return_value=None),
        patch("cli_agent_orchestrator.services.fleet_labels.remove_label"),
    ):
        mock_http.delete.return_value = resp
        mcp_delete_terminal(terminal_id="abcd1234", force=True, orphan=False)

    # The force flag reached the API cleanup-force path via the query param.
    _, kwargs = mock_http.delete.call_args
    assert kwargs["params"]["force"] is True


# ===========================================================================
# F513: bounded-wait exclusive lifecycle-lease acquire
# ===========================================================================


def _fresh_lease_module():
    """Import the lease module and clear its process-local state."""
    import cli_agent_orchestrator.services.session_lifecycle_lease as m

    with m._guard:
        m._shared.clear()
        m._exclusive.clear()
    return m


def test_blocking_acquire_succeeds_immediately_when_free() -> None:
    m = _fresh_lease_module()
    tok = m.acquire_session_lifecycle_exclusive_blocking("sess-A", timeout_s=1.0)
    assert tok is not None
    assert tok.mode == "exclusive"
    m.release_session_lifecycle_lease(tok)


def test_blocking_acquire_times_out_when_shared_holder_persists() -> None:
    """F513: if a shared holder never releases within the window, the
    bounded acquire returns None (mapped to 409 by the caller)."""
    m = _fresh_lease_module()
    shared = m.acquire_session_lifecycle_shared("sess-B")
    assert shared is not None

    t0 = time.monotonic()
    tok = m.acquire_session_lifecycle_exclusive_blocking(
        "sess-B", timeout_s=0.4, poll_interval_s=0.05
    )
    elapsed = time.monotonic() - t0

    assert tok is None
    # It actually waited ~the timeout rather than failing instantly.
    assert elapsed >= 0.35
    m.release_session_lifecycle_lease(shared)


def test_blocking_acquire_succeeds_after_shared_holder_releases() -> None:
    """F513 core: a delete's exclusive acquire waits out a transient sibling
    shared holder (an unrelated deferred init) and then succeeds — instead of
    the old instant resume_in_progress 409."""
    m = _fresh_lease_module()
    shared = m.acquire_session_lifecycle_shared("sess-C")
    assert shared is not None

    def _release_soon() -> None:
        time.sleep(0.2)
        m.release_session_lifecycle_lease(shared)

    releaser = threading.Thread(target=_release_soon)
    releaser.start()
    try:
        tok = m.acquire_session_lifecycle_exclusive_blocking(
            "sess-C", timeout_s=2.0, poll_interval_s=0.05
        )
        assert tok is not None
        assert tok.mode == "exclusive"
        m.release_session_lifecycle_lease(tok)
    finally:
        releaser.join()


def test_blocking_acquire_does_not_hold_guard_while_waiting() -> None:
    """The bounded wait must not pin the module _guard lock — other lease
    operations on a DIFFERENT session must proceed concurrently while one
    session's acquire is polling."""
    m = _fresh_lease_module()
    shared = m.acquire_session_lifecycle_shared("sess-D")
    assert shared is not None

    other_ok: list[bool] = []

    def _touch_other_session() -> None:
        # If the guard were held across the sleep, this would block.
        tok = m.acquire_session_lifecycle_exclusive("sess-OTHER")
        other_ok.append(tok is not None)
        if tok is not None:
            m.release_session_lifecycle_lease(tok)

    waiter = threading.Thread(
        target=lambda: m.acquire_session_lifecycle_exclusive_blocking(
            "sess-D", timeout_s=0.5, poll_interval_s=0.05
        )
    )
    waiter.start()
    time.sleep(0.1)  # let the waiter enter its poll loop
    _touch_other_session()
    waiter.join()

    assert other_ok == [True]
    m.release_session_lifecycle_lease(shared)


def test_delete_inner_waits_then_raises_on_persistent_contention() -> None:
    """F513 integration-shape: _delete_terminal_inner surfaces
    resume_in_progress only AFTER the bounded wait, and passes the configured
    timeout into the blocking acquire."""
    import cli_agent_orchestrator.services.terminal_service as ts

    captured: dict[str, float] = {}

    def _fake_blocking(session_name: str, *, timeout_s: float, poll_interval_s: float = 0.25):
        captured["timeout_s"] = timeout_s
        return None  # simulate persistent contention

    with (
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease."
            "acquire_session_lifecycle_exclusive_blocking",
            side_effect=_fake_blocking,
        ),
        patch.object(ts, "_quiesce_cascade_subtree_pre_plan"),
        patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            return_value=3.0,
        ),
    ):
        with pytest.raises(RuntimeError, match="resume_in_progress"):
            ts._delete_terminal_inner(
                terminal_id="abcd1234",
                session_name="cao-sess",
                root={"tmux_session": "cao-sess"},
                registry=None,
                force=False,
                orphan=False,
                caller_id=None,
            )

    assert captured["timeout_s"] == 3.0
