"""End-to-end inbox delivery tests (send_message API simulation).

Tests the inbox delivery plumbing via the CAO API:
1. Create two terminals (sender and receiver) in the same session
2. Wait for both to reach IDLE
3. Send message from sender to receiver's inbox via API
4. Verify message appears in receiver's inbox
5. Verify receiver processes the message (status transitions)
6. Cleanup

NOTE: These tests send messages via the CAO API, not via an agent calling
the send_message() MCP tool. For real agent-to-agent communication via
MCP tools, see test_supervisor_orchestration.py.

Requires: running CAO server, authenticated CLI tools (codex, claude, kiro-cli, copilot), tmux.

Run:
    uv run pytest -m e2e test/e2e/test_send_message.py -v
    uv run pytest -m e2e test/e2e/test_send_message.py -v -k codex
    uv run pytest -m e2e test/e2e/test_send_message.py -v -k claude_code
    uv run pytest -m e2e test/e2e/test_send_message.py -v -k kiro_cli
    uv run pytest -m e2e test/e2e/test_send_message.py -v -k copilot
"""

import subprocess
import time
import uuid
from test.e2e.conftest import (
    cleanup_terminal,
    create_terminal,
    get_terminal_status,
    wait_for_status,
)

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL


def _create_terminal_in_session(session_name: str, provider: str, agent_profile: str):
    """Create a terminal in an existing session.

    Returns (terminal_id, window_name).
    """
    resp = requests.post(
        f"{API_BASE_URL}/sessions/{session_name}/terminals",
        params={
            "provider": provider,
            "agent_profile": agent_profile,
        },
    )
    assert resp.status_code in (
        200,
        201,
    ), f"Terminal creation in session failed: {resp.status_code} {resp.text}"
    data = resp.json()
    return data["id"]


def _get_terminal_token(terminal_id: str) -> str:
    """Fetch a terminal's CAO_TERMINAL_TOKEN from its tmux pane environment.

    Reads the token that was injected by the backend at spawn time:
    1. GET /terminals/{id} to obtain session_name and window name.
    2. tmux display-message to resolve the pane PID.
    3. Read /proc/{pid}/environ to extract CAO_TERMINAL_TOKEN.

    Returns the token string, or raises AssertionError if not found.
    """
    resp = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
    assert resp.status_code == 200, (
        f"GET terminal {terminal_id} failed: {resp.status_code} {resp.text}"
    )
    info = resp.json()
    session_name = info["session_name"]
    window_name = info["name"]

    target = f"{session_name}:{window_name}"
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target, "#{pane_pid}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"tmux display-message failed for {target}: {result.stderr}"
    pane_pid = result.stdout.strip()
    assert pane_pid.isdigit(), f"Invalid pane PID for {target}: {pane_pid!r}"

    environ_path = f"/proc/{pane_pid}/environ"
    with open(environ_path, "rb") as f:
        environ_data = f.read()
    for entry in environ_data.split(b"\x00"):
        if entry.startswith(b"CAO_TERMINAL_TOKEN="):
            return entry.decode().split("=", 1)[1]

    raise AssertionError(
        f"CAO_TERMINAL_TOKEN not found in pane environment for terminal {terminal_id} "
        f"(pid={pane_pid}, target={target})"
    )


def _send_inbox_message(sender_id: str, receiver_id: str, message: str):
    """Send a message to a terminal's inbox via the API.

    Authenticates using the sender's real CAO_TERMINAL_TOKEN (F332 enforcement).
    """
    token = _get_terminal_token(sender_id)
    resp = requests.post(
        f"{API_BASE_URL}/terminals/{receiver_id}/inbox/messages",
        params={"sender_id": sender_id, "message": message},
        headers={"X-CAO-Terminal-Token": token},
    )
    assert resp.status_code == 200, f"Inbox message send failed: {resp.status_code} {resp.text}"
    return resp.json()


def _get_inbox_messages(terminal_id: str, status_filter: str = None):
    """Get inbox messages for a terminal."""
    params = {"limit": 50}
    if status_filter:
        params["status"] = status_filter
    resp = requests.get(
        f"{API_BASE_URL}/terminals/{terminal_id}/inbox/messages",
        params=params,
    )
    assert resp.status_code == 200, f"Get inbox messages failed: {resp.status_code} {resp.text}"
    return resp.json()


def _run_send_message_test(provider: str, agent_profile: str):
    """Core send_message test: create two terminals, send message via inbox.

    Tests:
    - Message is created in receiver's inbox
    - Message has correct sender_id
    - Message content is preserved
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-sendmsg-{provider}-{session_suffix}"
    sender_id = None
    receiver_id = None
    actual_session = None

    try:
        # Step 1: Create first terminal (acts as sender / supervisor)
        sender_id, actual_session = create_terminal(provider, agent_profile, session_name)
        assert sender_id, "Sender terminal ID should not be empty"

        # Step 2: Wait for sender to be ready (idle or completed).
        start = time.time()
        while time.time() - start < 90.0:
            s = get_terminal_status(sender_id)
            if s in ("idle", "completed"):
                break
            if s == "error":
                break
            time.sleep(3)
        assert s in (
            "idle",
            "completed",
        ), f"Sender terminal did not become ready within 90s (provider={provider})"

        # Step 3: Create second terminal in the same session (acts as receiver)
        receiver_id = _create_terminal_in_session(actual_session, provider, agent_profile)
        assert receiver_id, "Receiver terminal ID should not be empty"

        # Step 4: Wait for receiver to be ready (idle or completed).
        start = time.time()
        while time.time() - start < 90.0:
            s = get_terminal_status(receiver_id)
            if s in ("idle", "completed"):
                break
            if s == "error":
                break
            time.sleep(3)
        assert s in (
            "idle",
            "completed",
        ), f"Receiver terminal did not become ready within 90s (provider={provider})"

        # Step 5: Send message from sender to receiver's inbox
        test_message = f"E2E test message from {sender_id} at {time.time()}"
        result = _send_inbox_message(sender_id, receiver_id, test_message)
        assert result.get("message_id"), "Message should have an ID"
        assert result.get("sender_id") == sender_id, "Sender ID should match"
        assert result.get("receiver_id") == receiver_id, "Receiver ID should match"

        # Step 6: Verify message appears in receiver's inbox
        # Give the inbox service a moment to process
        time.sleep(3)
        messages = _get_inbox_messages(receiver_id)
        assert len(messages) > 0, "Receiver should have at least one inbox message"

        # Find our message
        found = False
        for msg in messages:
            if msg.get("sender_id") == sender_id and test_message in msg.get("message", ""):
                found = True
                break
        assert found, (
            f"Test message not found in receiver's inbox. "
            f"Messages: {[m.get('message', '')[:50] for m in messages]}"
        )

        # Step 7: Verify message was DELIVERED (not stuck as PENDING).
        # Poll inbox message status — the inbox service may take a few seconds
        # to detect IDLE and paste the message into the receiver's terminal.
        delivered = False
        for _ in range(24):  # up to 120s (TUI providers need time to go IDLE)
            time.sleep(5)
            messages = _get_inbox_messages(receiver_id, status_filter="delivered")
            if any(
                m.get("sender_id") == sender_id and test_message in m.get("message", "")
                for m in messages
            ):
                delivered = True
                break
        assert delivered, (
            f"Inbox message should have been delivered (status=delivered) within 120s. "
            f"All messages: {_get_inbox_messages(receiver_id)}"
        )

        # Step 8: Verify receiver processes the message (should transition from IDLE).
        # After inbox delivery, the receiver gets the message as input.
        # Acceptable states: processing (working), completed (done),
        # waiting_user_answer (provider showing approval prompt for the message).
        transitioned = False
        for _ in range(12):  # up to 60s
            time.sleep(5)
            receiver_status = get_terminal_status(receiver_id)
            if receiver_status in ("processing", "completed", "waiting_user_answer"):
                transitioned = True
                break
        assert transitioned, (
            f"Receiver should have transitioned from IDLE after inbox delivery "
            f"within 60s, got: {receiver_status}"
        )

    finally:
        if sender_id and actual_session:
            cleanup_terminal(sender_id, actual_session)
        if receiver_id and actual_session:
            # Receiver is in the same session, just exit it
            try:
                requests.post(f"{API_BASE_URL}/terminals/{receiver_id}/exit")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Codex provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCodexSendMessage:
    """E2E send_message tests for the Codex provider."""

    def test_send_message_to_inbox(self, require_codex):
        """Send a message to another Codex terminal's inbox and verify delivery."""
        _run_send_message_test(provider="codex", agent_profile="developer")


# ---------------------------------------------------------------------------
# Claude Code provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestClaudeCodeSendMessage:
    """E2E send_message tests for the Claude Code provider."""

    def test_send_message_to_inbox(self, require_claude):
        """Send a message to another Claude Code terminal's inbox and verify delivery."""
        _run_send_message_test(provider="claude_code", agent_profile="developer")


# ---------------------------------------------------------------------------
# Kiro CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKiroCliSendMessage:
    """E2E send_message tests for the Kiro CLI provider."""

    def test_send_message_to_inbox(self, require_kiro):
        """Send a message to another Kiro CLI terminal's inbox and verify delivery."""
        _run_send_message_test(provider="kiro_cli", agent_profile="developer")


# ---------------------------------------------------------------------------
# Kimi CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKimiCliSendMessage:
    """E2E send_message tests for the Kimi CLI provider."""

    def test_send_message_to_inbox(self, require_kimi):
        """Send a message to another Kimi CLI terminal's inbox and verify delivery."""
        _run_send_message_test(provider="kimi_cli", agent_profile="developer")


# ---------------------------------------------------------------------------
# Copilot CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCopilotCliSendMessage:
    """E2E send_message tests for the Copilot CLI provider."""

    def test_send_message_to_inbox(self, require_copilot):
        """Send a message to another Copilot CLI terminal's inbox and verify delivery."""
        _run_send_message_test(provider="copilot_cli", agent_profile="developer")


# ---------------------------------------------------------------------------
# Cursor CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCursorCliSendMessage:
    """E2E send_message tests for the Cursor CLI provider.

    Requires the ``agent`` (or legacy ``cursor-agent``) binary on PATH.
    Skip otherwise via the ``require_cursor`` fixture.
    """

    def test_send_message_to_inbox(self, require_cursor):
        """Send a message to another Cursor CLI terminal's inbox and verify delivery."""
        _run_send_message_test(provider="cursor_cli", agent_profile="developer")


# ---------------------------------------------------------------------------
# Antigravity CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestAntigravityCliSendMessage:
    """E2E send_message tests for the Antigravity CLI provider."""

    def test_send_message_to_inbox(self, require_antigravity):
        """Send a message to another Antigravity CLI terminal's inbox and verify delivery."""
        _run_send_message_test(provider="antigravity_cli", agent_profile="developer")


@pytest.mark.e2e
class TestOmpSendMessage:
    def test_send_message_to_inbox(self, require_omp):
        _run_send_message_test(provider="omp", agent_profile="developer")


# ---------------------------------------------------------------------------
# Grok Build CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestGrokCliSendMessage:
    """E2E inbox delivery test for the official xAI Grok Build CLI."""

    def test_send_message_to_inbox(self, require_grok):
        """Deliver an inbox message to an idle Grok terminal and process it."""
        _run_send_message_test(provider="grok_cli", agent_profile="developer")


# ---------------------------------------------------------------------------
# Cline CLI provider — AC18 (wp-callbacks-f329-f332)
# ---------------------------------------------------------------------------


def _run_cline_mcp_callback_test():
    """AC18: verify a cline_dev worker calls back via the send_message MCP tool.

    This test exercises the full F329'/F332 path:
    1. Create a supervisor terminal (any provider with MCP, e.g. codex).
    2. Create a cline_dev worker in the same session.
    3. Deliver a trivial task to the worker that instructs it to call
       send_message with a known payload back to the supervisor.
    4. Poll the supervisor's inbox for the callback message.
    5. Verify the message was delivered via MCP (not a hand-crafted POST).

    Red on the pre-change tree: the cline worker has no send_message MCP tool
    in its toolset before F329' materializes cline_mcp_settings.json.
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-cline-ac18-{session_suffix}"
    supervisor_id = None
    worker_id = None
    actual_session = None
    callback_marker = f"AC18-CALLBACK-{uuid.uuid4().hex[:8]}"

    try:
        # Step 1: Create supervisor terminal (uses codex as the cheapest MCP-capable provider)
        supervisor_id, actual_session = create_terminal(
            "cline_cli", "cline_dev", session_name
        )
        assert supervisor_id, "Supervisor terminal ID should not be empty"

        # Wait for supervisor to reach IDLE
        start = time.time()
        while time.time() - start < 90.0:
            s = get_terminal_status(supervisor_id)
            if s in ("idle", "completed"):
                break
            if s == "error":
                break
            time.sleep(3)
        assert s in ("idle", "completed"), (
            f"Supervisor did not become ready within 90s (status={s})"
        )

        # Step 2: Create cline_dev worker in the same session
        worker_id = _create_terminal_in_session(actual_session, "cline_cli", "cline_dev")
        assert worker_id, "Worker terminal ID should not be empty"

        # Wait for worker to reach IDLE
        start = time.time()
        while time.time() - start < 90.0:
            s = get_terminal_status(worker_id)
            if s in ("idle", "completed"):
                break
            if s == "error":
                break
            time.sleep(3)
        assert s in ("idle", "completed"), (
            f"Worker did not become ready within 90s (status={s})"
        )

        # Step 3: Send task to the worker instructing it to call send_message
        task_message = (
            f"[CAO Assigned task] Supervisor terminal: {supervisor_id}. "
            f"Your ONLY task: call the send_message MCP tool with "
            f'receiver_id="{supervisor_id}" and message="{callback_marker}". '
            f"Do nothing else. Do not explain. Just call send_message."
        )
        resp = requests.post(
            f"{API_BASE_URL}/terminals/{worker_id}/input",
            params={"message": task_message},
        )
        assert resp.status_code == 200, (
            f"Send task to worker failed: {resp.status_code} {resp.text}"
        )

        # Step 4: Wait for the callback to arrive in the supervisor's inbox.
        # The worker needs to process the task and invoke send_message via MCP.
        # Allow generous time for cline startup + MCP invocation.
        callback_received = False
        for _ in range(60):  # up to 300s (5 min)
            time.sleep(5)
            messages = _get_inbox_messages(supervisor_id)
            for msg in messages:
                if callback_marker in msg.get("message", ""):
                    callback_received = True
                    # Verify the sender is the worker
                    assert msg.get("sender_id") == worker_id, (
                        f"Callback sender should be worker {worker_id}, "
                        f"got {msg.get('sender_id')}"
                    )
                    break
            if callback_received:
                break

        assert callback_received, (
            f"AC18 FAILED: cline worker {worker_id} did not deliver callback "
            f"containing '{callback_marker}' to supervisor {supervisor_id} "
            f"within 300s. This means send_message MCP tool is not available "
            f"in the worker's toolset (F329' not applied)."
        )

    finally:
        if worker_id and actual_session:
            try:
                requests.post(f"{API_BASE_URL}/terminals/{worker_id}/exit")
            except Exception:
                pass
        if supervisor_id and actual_session:
            cleanup_terminal(supervisor_id, actual_session)


@pytest.mark.e2e
@pytest.mark.slow
class TestClineCliSendMessage:
    """AC18: E2E cline worker calls back via the send_message MCP tool.

    Verifies F329' (MCP materialization) + F332 (sender-token authentication)
    end-to-end: a cline_dev worker, assigned a trivial task, delivers its
    result via the send_message MCP tool — the inbox row's provenance is the
    MCP path, not a hand-crafted curl.

    Red on the pre-change tree: today the MCP tool does not exist in the
    worker's toolset (cline_cli.py does not materialize cline_mcp_settings.json).
    """

    def test_cline_worker_mcp_callback(self, require_cline):
        """A cline_dev worker delivers a callback via send_message MCP tool."""
        _run_cline_mcp_callback_test()

    def test_send_message_to_inbox(self, require_cline):
        """Standard inbox delivery test for cline_cli provider."""
        _run_send_message_test(provider="cline_cli", agent_profile="cline_dev")


@pytest.mark.e2e
class TestMiniMaxCodeSendMessage:
    """E2E inbox delivery test for MiniMax Code."""

    def test_send_message_to_inbox(self, require_minimax_code):
        _run_send_message_test(provider="mcode", agent_profile="developer")
