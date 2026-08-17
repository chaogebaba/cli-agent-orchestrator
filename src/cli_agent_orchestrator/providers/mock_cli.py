"""Mock CLI provider — deterministic stand-in for credential-free CI tests.

This provider exists to exercise CAO's orchestration logic (handoff,
assign, send_message, inbox watchdog, multi-provider sessions) in
CI without requiring any real coding-CLI binary, network call, or
credentials.

It wraps a tiny ``mock_cli`` shell binary shipped at
``test/providers/fixtures/bin/mock_cli``. The binary is a deterministic
REPL: it prints a prompt, reads stdin, sleeps a configurable delay, and
echoes the input prefixed with ``> MOCK:``.

Production code paths never see this provider — the binary is not on
PATH outside pytest. The conftest-level PATH-prepend in
``test/conftest.py`` makes it discoverable for the duration of the test
session.

F139: In sandbox mode with a manifest fixture_providers row, the provider
derives all behavior from the manifest-pinned capability. No PATH lookup,
arbitrary command, or profile field controls the variant.

See ``docs/mock-cli-provider.md`` for the design and motivation.
"""

import asyncio
import logging
import os
import re
import shlex
from pathlib import Path
from typing import List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# Idle prompt emitted by the mock_cli binary at the end of every turn.
IDLE_PROMPT_PATTERN = r"❯\s*$"
IDLE_PROMPT_PATTERN_LOG = r"❯\s"
# Response indicator emitted by the binary before each reply line.
RESPONSE_INDICATOR_PATTERN = r"^>\s*MOCK:"
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
ERROR_INDICATOR = "ERROR: mock failure injected"

# Scripted-prompt mode: when CAO_MOCK_CLI_SCRIPTED_PROMPTS=1, the presence of
# this marker in the buffer causes get_status to return WAITING_USER_ANSWER.
# When an answer is delivered (text appears after the marker on subsequent lines),
# the status clears back to normal (IDLE/COMPLETED).
SCRIPTED_PROMPT_MARKER = "APPROVAL_REQUIRED:"

# F139: terminal ID format required by fixture binary
_TERMINAL_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _scripted_prompts_enabled() -> bool:
    """Check if scripted-prompt mode is enabled via env var."""
    return os.environ.get("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "").strip() in ("1", "true", "yes")


class MockCliProvider(BaseProvider):
    """Deterministic mock provider for orchestration-layer CI tests.

    Not for production use. The companion binary lives at
    ``test/providers/fixtures/bin/mock_cli`` and must be on PATH (the
    repo's ``test/conftest.py`` prepends it for the pytest session).

    F139: In sandbox fixture mode, behavior is derived entirely from the
    manifest-pinned capability. The binary is invoked by absolute realpath
    with explicit --variant, --state-dir, --terminal-id argv.
    """

    BINARY_NAME = "mock_cli"
    # ARM4: enable rebind/generation-2 path for sandbox gate tests
    supports_reauth_rebind: bool = True
    # ARM7: configurable startup delay for crash-restart timing tests
    # Set CAO_MOCK_CLI_STARTUP_DELAY_MS to inject a delay before init completes
    _STARTUP_DELAY_ENV = "CAO_MOCK_CLI_STARTUP_DELAY_MS"
    # F254 D9: busy-hold for UX-3 injection_during_prompt scenario.
    # Set CAO_MOCK_CLI_BUSY_MS to hold the fake in PROCESSING for that many ms
    # after each send_input, enabling zero-sleep testing of busy-gate behaviour.
    _BUSY_MS_ENV = "CAO_MOCK_CLI_BUSY_MS"

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        allowed_tools: Optional[List[str]] = None,
        delay_ms: int = 50,
    ) -> None:
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._delay_ms = delay_ms
        self._fixture_capability = None
        self._fixture_never_set_event: Optional[asyncio.Event] = None
        # F254 D9: timestamp until which get_status returns PROCESSING
        self._busy_until: float = 0.0

    def _load_fixture_capability(self):
        """Load the fixture capability from the active manifest if in sandbox mode."""
        if not os.environ.get("CAO_INSTANCE_ID", "").strip():
            return None
        try:
            from cli_agent_orchestrator.utils.provider_plane import (
                load_active_fixture_provider,
            )

            return load_active_fixture_provider("mock_cli")
        except Exception:
            return None

    async def initialize(self) -> bool:
        """Launch the ``mock_cli`` binary inside the tmux window."""
        if not await wait_for_shell(self.terminal_id, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        self._fixture_capability = self._load_fixture_capability()
        cap = self._fixture_capability

        # F138 R10 / F139 D8: Capture shell_baseline from the pane BEFORE any
        # fixture binary launches. Required by _prepare_provider_runtime_identity
        # when supports_reauth_rebind=True (L309 of terminal_service.py).
        # - empty-shell: needs baseline for F124 child-gone detection
        # - healthy/startup-delay: needs baseline for runtime identity persist
        # - process-less: needs baseline (no child starts, pane stays at shell)
        # - spawn-then-fault: faults before identity persist; capture is harmless
        try:
            baseline = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
        except Exception:
            baseline = None
        if baseline:
            self.shell_baseline = baseline
            logger.info("MockCliProvider shell_baseline captured: %s", baseline)

        if cap is not None:
            # F139 D10: process-less variant starts no child
            if cap.variant == "process-less":
                self.has_process_child = False
                self._initialized = True
                return True
            # F139 D6: exact binary argv from manifest — no PATH lookup
            if not _TERMINAL_ID_RE.match(self.terminal_id):
                raise ValueError(
                    f"F139: terminal_id must be canonical 8-hex, got: {self.terminal_id!r}"
                )
            argv = [
                str(cap.binary_realpath),
                "--variant",
                cap.variant,
                "--state-dir",
                str(cap.state_dir),
                "--terminal-id",
                self.terminal_id,
            ]
            command = shlex.join(argv)
        else:
            # Legacy CI mode: binary on PATH
            command = shlex.join([self.BINARY_NAME, "--delay-ms", str(self._delay_ms)])

        get_backend().send_keys(self.session_name, self.window_name, command)

        # F139 D8: empty-shell variant skips wait_until_status entirely.
        # The binary exits before IDLE is ever observed by the status monitor.
        # D8 sequence: send keys → wait for fixture child gone → return True.
        if cap is not None and cap.variant == "empty-shell":
            await self._wait_for_fixture_child_gone()
            # F139 r5 D15: signal to _provider_child_alive that the fixture
            # confirmed child exit (baseline match). This makes the F124
            # _confirm_launch_health check return False deterministically
            # regardless of transient procfs/tmux pane state.
            if self.shell_baseline:
                self.launch_health_failure_confirmed = True
            self._initialized = True
            return True

        # ARM5: spawn-then-fault — child spawned (send_keys fired above),
        # but initialize() raises to simulate provider crash after spawn.
        # This exercises the D23 settlement trigger: incarnation is reserved
        # (child started) but initialize fails → settlement/reconcile path.
        if cap is not None and cap.variant == "spawn-then-fault":
            # Brief pause to allow process child to actually start
            await asyncio.sleep(0.05)
            raise RuntimeError(
                "F139: spawn-then-fault — provider child spawned but "
                "initialize() failed (simulated post-spawn crash)"
            )

        if not await wait_until_status(
            self.terminal_id, {TerminalStatus.IDLE, TerminalStatus.COMPLETED}, timeout=15.0
        ):
            raise TimeoutError("mock_cli initialization timed out after 15 seconds")


        # ARM7: configurable startup delay — creates timing window for
        # crash-restart-with-pending-job tests
        _startup_delay_raw = os.environ.get(self._STARTUP_DELAY_ENV, "")
        if _startup_delay_raw.strip().isdigit() and int(_startup_delay_raw) > 0:
            await asyncio.sleep(int(_startup_delay_raw) / 1000.0)
        self._initialized = True
        return True

    async def _wait_for_fixture_child_gone(self, timeout: float = 15.0) -> None:
        """F139 D8: wait until the fixture child is gone / pane returns to baseline.

        Returns normally (does not raise) — the false-positive initialize is the
        intended contract for the empty-shell variant. The wait is on pane state,
        not elapsed time, per blueprint D8.
        """
        if not self.shell_baseline:
            # No baseline captured — fall back to a bounded wait for the pane
            # command to differ from the fixture binary (indicates exit).
            return
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                current = get_backend().get_pane_current_command(
                    self.session_name, self.window_name
                )
            except Exception:
                current = None
            if current == self.shell_baseline:
                # Pane returned to pre-launch shell — fixture child is gone.
                return
            await asyncio.sleep(0.1)
        logger.warning(
            "F139 empty-shell: fixture child did not return to baseline within %.1fs; "
            "continuing with false-positive initialize",
            timeout,
        )

    async def send_input(self, text: str) -> None:
        """Send input — fixture variants may have special behavior."""
        cap = self._fixture_capability

        if cap is not None and cap.variant == "process-less":
            # F139 D10: record receipt in state_dir, no tmux I/O
            receipt_path = cap.state_dir / f"receipt-{self.terminal_id}"
            fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, text.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            # fsync containing directory
            dir_fd = os.open(cap.state_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return

        if cap is not None and cap.variant == "procfs-unavailable":
            # F139 D11: await an Event that is never set — cancellation-safe
            if self._fixture_never_set_event is None:
                self._fixture_never_set_event = asyncio.Event()
            try:
                await self._fixture_never_set_event.wait()
            except asyncio.CancelledError:
                raise
            return

        # Normal send path (healthy, empty-shell, post-send-death, legacy)
        # F139 D9: "uses the ordinary tmux send path" — force_bracketed_paste=True
        # matches production send_input delivery so the binary's `read -r line`
        # receives a complete terminated line via paste rather than character-by-
        # character key events. The binary already strips paste markers.
        get_backend().send_keys(
            self.session_name,
            self.window_name,
            text,
            force_bracketed_paste=True,
            enter_count=1,
        )

        # F254 D9: arm the busy-hold window after send, enabling zero-sleep
        # testing of the UX-3 injection gate (busy worker blocks paste).
        import time as _time

        _busy_raw = os.environ.get(self._BUSY_MS_ENV, "")
        if _busy_raw.strip().isdigit() and int(_busy_raw) > 0:
            self._busy_until = _time.monotonic() + int(_busy_raw) / 1000.0

        if cap is not None and cap.variant == "post-send-death":
            # F139 D9: wait for receipt marker, then raise
            receipt_path = cap.state_dir / f"receipt-{self.terminal_id}"
            deadline = asyncio.get_event_loop().time() + 30.0
            while asyncio.get_event_loop().time() < deadline:
                if receipt_path.exists():
                    break
                await asyncio.sleep(0.05)
            else:
                raise TimeoutError("F139: post-send-death receipt not observed within 30s")
            # Raise deferred-init failure after receipt evidence exists
            from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError

            raise DeliveryDeferredError(
                "provider_launch_failed",
                "F139: fixture post-send-death — delivery receipt observed, provider exited",
            )

    def get_status(self, buffer: str) -> TerminalStatus:
        """Pattern-match the binary's output buffer to determine current state."""
        if not buffer:
            return TerminalStatus.UNKNOWN

        # F254 D9: CAO_MOCK_CLI_BUSY_MS busy-hold window
        import time as _time

        if self._busy_until and _time.monotonic() < self._busy_until:
            return TerminalStatus.PROCESSING

        # F139 D10: process-less is always ready
        if self._fixture_capability and self._fixture_capability.variant == "process-less":
            return TerminalStatus.IDLE

        clean = re.sub(ANSI_CODE_PATTERN, "", buffer)

        if ERROR_INDICATOR in clean:
            return TerminalStatus.ERROR

        # Scripted-prompt mode: check for APPROVAL_REQUIRED marker
        if _scripted_prompts_enabled() and SCRIPTED_PROMPT_MARKER in clean:
            # Find the marker position
            marker_idx = clean.rfind(SCRIPTED_PROMPT_MARKER)
            after_marker = clean[marker_idx + len(SCRIPTED_PROMPT_MARKER) :]
            # The marker line may contain the prompt text (e.g. "APPROVAL_REQUIRED: Allow?")
            # The answer is delivered when there is a SUBSEQUENT line with non-whitespace
            # content after the marker line.
            lines_after = after_marker.split("\n")[1:]  # Skip remainder of marker line
            has_answer = any(line.strip() for line in lines_after)
            if not has_answer:
                return TerminalStatus.WAITING_USER_ANSWER

        has_idle = re.search(IDLE_PROMPT_PATTERN, clean, re.MULTILINE)
        if not has_idle:
            return TerminalStatus.PROCESSING

        responses = list(re.finditer(RESPONSE_INDICATOR_PATTERN, clean, re.MULTILINE))
        if responses:
            return TerminalStatus.COMPLETED
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Return the payload of the last ``> MOCK: ...`` line."""
        clean = re.sub(ANSI_CODE_PATTERN, "", script_output)
        matches = list(re.finditer(r"^>\s*MOCK:\s*(.*)$", clean, re.MULTILINE))
        if not matches:
            raise ValueError("No mock_cli response found in script output")
        return matches[-1].group(1).strip()

    # -- F138 ARM4: session identity methods for same-ID rebind path --

    def capture_session_uuid(self, pane_pid: int, launch_time: float, cwd: str) -> str:
        """Return a stable fixture session UUID derived from the terminal_id.

        MockCliProvider has no real session state, so we synthesize a
        deterministic identifier that satisfies the rebind path's contract:
        same terminal → same session UUID across rebind generations.
        """
        return f"mock-session-{self.terminal_id}"

    def resume_session_uuid(self) -> str | None:
        """Return the fixture session UUID for resume coordination."""
        return f"mock-session-{self.terminal_id}"

    def validate_session_artifact(self, session_uuid: str, cwd: str) -> None:
        """Validate the session artifact for rebind.

        In fixture mode, any session UUID matching our deterministic pattern
        is valid. This allows the rebind path to proceed without requiring
        filesystem artifacts that a real provider would maintain.
        """
        expected_prefix = f"mock-session-{self.terminal_id}"
        if session_uuid != expected_prefix:
            raise ValueError(
                f"mock_cli session artifact mismatch: expected {expected_prefix}, got {session_uuid}"
            )

    def get_idle_pattern_for_log(self) -> str:
        return IDLE_PROMPT_PATTERN_LOG

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        return None
