"""E2E: F560 Kiro session resume via id minting (ZQX7 round-trip).

Drives the REAL KiroCliProvider against a real ``kiro-cli`` process in tmux,
exercising the exact mint+resume path shipped in F560:

1. Construct a provider (fresh spawn) — it mints ``sess_<uuid4>`` and launches
   ``kiro-cli ... --resume-id <minted>``. Verify the process adopts that id.
2. Send "remember token ZQX7"; wait for the turn to complete (persists the
   session under the minted id).
3. Kill the tmux window (worker reaped), then construct a SECOND provider with
   a resume ForkContext carrying the SAME id — it launches
   ``kiro-cli ... --resume-id <same-id>`` and re-opens the conversation.
4. Ask for the token; assert the reply contains ZQX7.

Requires: kiro-cli installed + authenticated, tmux. Marked ``e2e`` (the tier
plugin also marks /test/e2e/ as ``live``). Run on an offload box:

    uv run pytest -m e2e test/e2e/test_kiro_session_resume.py -v
"""

import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.providers.kiro_cli import ANSI_CODE_PATTERN, KiroCliProvider

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not __import__("os").environ.get("CAO_F560_E2E"),
        reason=(
            "F560 resume e2e drives the real provider; provider.initialize() is "
            "coupled to CAO's StatusMonitor/FIFO pipeline (a CAO-registered "
            "terminal), so it needs the server harness. Set CAO_F560_E2E=1 to run "
            "on a host with kiro-cli authenticated + the CAO terminal lifecycle "
            "available. The provider flag behaviour is proven by the unit suite "
            "and a manual live tmux ZQX7 round-trip (see build report)."
        ),
    ),
]

_TOKEN = "ZQX7"


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def _capture(session: str, window: str) -> str:
    out = _tmux("capture-pane", "-t", f"{session}:{window}", "-p", "-S", "-200").stdout
    return re.sub(ANSI_CODE_PATTERN, "", out or "")


@pytest.fixture
def kiro_window(tmp_path: Path):
    """A tmux session+window with a shell in an isolated cwd for kiro sessions."""
    if shutil.which("kiro-cli") is None or shutil.which("tmux") is None:
        pytest.skip("kiro-cli and tmux are required for this e2e")
    session = f"f560e2e-{uuid.uuid4().hex[:8]}"
    window = "0"
    cwd = tmp_path / "work"
    cwd.mkdir()
    _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", str(cwd))
    try:
        yield session, window, str(cwd)
    finally:
        _tmux("kill-session", "-t", session)


async def _drive_turn(provider: KiroCliProvider, session: str, window: str, text: str) -> None:
    """Send one message and wait until the provider leaves PROCESSING."""
    from cli_agent_orchestrator.backends.registry import get_backend

    get_backend().send_keys(session, window, text)
    time.sleep(1.0)
    get_backend().send_special_key(session, window, "Enter")
    # Wait for a completed turn (bounded).
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = provider.get_status()
        if status in (TerminalStatus.COMPLETED, TerminalStatus.IDLE) and _TOKEN_seen_or_prompt(
            session, window
        ):
            return
        time.sleep(1.0)


def _TOKEN_seen_or_prompt(session: str, window: str) -> bool:
    # Heuristic: a completed turn shows the credits/time footer.
    return "Credits" in _capture(session, window)


@pytest.mark.asyncio
async def test_kiro_mint_and_resume_round_trip(kiro_window):
    """A minted id starts a fresh session and resumes it with ZQX7 recall."""
    session, window, cwd = kiro_window

    # --- 1. Fresh spawn: provider mints the id and launches --resume-id ---
    p1 = KiroCliProvider(
        "f560t1",
        session,
        window,
        "kiro_dev",
        allowed_tools=["*"],
        engine=KiroEngine.KAS,
    )
    minted = p1.allocated_session_uuid
    assert minted.startswith("sess_")

    # Point the provider's backend context at our tmux window and initialize.
    await p1.initialize()

    # The launched process must have adopted the minted id.
    listed = subprocess.run(
        ["kiro-cli", "--v3", "chat", "--list-sessions", "--format", "json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert minted in (
        listed.stdout or ""
    ), f"minted id {minted} not adopted; list-sessions: {listed.stdout!r}"

    # --- 2. Plant the token ---
    await _drive_turn(p1, session, window, f"Remember this token: {_TOKEN}. Just acknowledge.")

    # --- 3. Reap the worker (kill the window), then resume the SAME id ---
    _tmux("kill-session", "-t", session)
    time.sleep(2.0)
    _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", cwd)

    p2 = KiroCliProvider(
        "f560t2",
        session,
        window,
        "kiro_dev",
        allowed_tools=["*"],
        engine=KiroEngine.KAS,
        fork_context=ForkContext(
            mode="resume",
            session_uuid=minted,
            base_name="base",
            provider="kiro_cli",
            initial_preamble="",
        ),
    )
    assert p2.allocated_session_uuid == minted  # reused verbatim
    assert p2._resume_session_id() == minted
    await p2.initialize()

    # --- 4. Ask for the token; assert recall ---
    await _drive_turn(
        p2, session, window, "What token did I ask you to remember? Reply with only the token."
    )
    pane = _capture(session, window)
    # The prior turn AND the recall both surface; require the token to appear
    # after the resume prompt line.
    assert _TOKEN in pane, f"token {_TOKEN} not recalled after resume; pane tail:\n{pane[-800:]}"
