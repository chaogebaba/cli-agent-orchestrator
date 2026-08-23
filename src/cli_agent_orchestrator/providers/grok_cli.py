"""Grok CLI provider implementation.

This provider drives the interactive Grok Build TUI in tmux. Probe P1 showed
that ``--minimal`` is the most scrapeable rendering mode: completed turns are
printed into normal scrollback and the pinned prompt/footer remains compact.

Profile prompt delivery uses ``--system-prompt-override`` rather than
``--agent`` because a live headless probe did not confirm that ad-hoc agent
definition bodies are applied consistently. The launch still passes ``-m`` when
the CAO profile pins a model.
"""

import hashlib
import logging
import os
import re
import shlex
import shutil
import signal
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import psutil

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider, RetryableArtifactValidation
from cli_agent_orchestrator.providers.screen_classification import (
    ScreenClassificationResult,
    ScreenSignal,
    screen_classification_result,
)
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_provider_profile_defaults,
    get_server_settings,
    resolve_provider_string_option,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.grok_config import ensure_grok_mcp_servers
from cli_agent_orchestrator.utils.provider_plane import provider_home
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

GROK_BINARY = str(Path.home() / ".grok" / "bin" / "grok")

IDLE_PROMPT_PATTERN = r"^\s*❯\s*$|^\s*❯\s+\S.*$"
USER_PROMPT_PATTERN = r"^\s*❯\s+(.+)$"
PROCESSING_PATTERN = (
    r"Waiting for response…"
    r"|Waiting for response\.\.\."
    # Mid-row spinner (◆ or braille) optionally behind ┃ separator + Thinking/Responding
    r"|[^\S\r\n]*(?:┃[^\S\r\n]*)?(?:◆|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏)"
    r"[^\S\r\n]*(?:Thinking|Responding)\b"
    # Bare spinner with any trailing text (mid-row or row-start) — fold r1 B1
    r"|(?:┃[^\S\r\n]*)?(?:⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏)[^\S\r\n]+\S"
    r"| - (?:Waiting for response|Thinking|Responding) - "
)
COMPLETION_PATTERN = r"^\s*(?:Turn completed in [\d.]+s\.|Worked for [\d.]+s\.)\s*$"
RUNNING_PATTERN = r"^\s*Worked for [\d.]+s\.\s+\d+ commands? still running\.\s*$"
WAITING_USER_ANSWER_PATTERN = (
    r"Run Grok Build in a project directory\?"
    r"|↑/↓ navigate"
    r"|Enter:submit"
    # F264: first-run trust-directory dialog footer (bottom row)
    r"|Enter or y to trust\b"
)
# F354: geometry-derived — grok trust dialog footer renders at row 41 in a 49-row
# pane (8 rows from bottom); must reach it (≥8) while excluding F264's quoted-footer
# text at row 1 in a 21-row negative (safe up to 19).  10 gives comfortable margin.
WAITING_VIEWPORT_ROWS = 10
ERROR_PATTERN = (
    r"^\s*(?:"
    r"Error:\s+.+"
    r"|ERROR:\s+.+"
    r"|Grok(?: Build)? (?:error|failed):\s+.+"
    r"|Authentication required\b.*"
    r"|Rate limit(?:ed| exceeded)?\b.*"
    r"|Failed to (?:authenticate|load|connect|initialize|start)\b.*"
    r")$"
)

FOOTER_HINT_PATTERN = r"(?:\balways-approve\b|ctrl\+o transcript|Shift\+Tab:mode|Ctrl\+x:shortcuts)"
IDLE_FOOTER_PATTERN = FOOTER_HINT_PATTERN
COMPOSER_PROMPT_PATTERN = r"^\s*(?:│\s*)?❯(?:\s|$)"
EMPTY_DRAFT_PLACEHOLDERS = {
    "",
}


class ProviderError(Exception):
    """Exception raised for Grok provider-specific errors."""


class GrokCliProvider(BaseProvider):
    supports_fork_context = True
    supports_reauth_rebind = True
    """Provider for Grok Build's interactive CLI."""

    supports_screen_detection = True
    supports_draft_preservation = True
    composer_clear_keys = ["C-a", "C-k"]
    clear_immune_ghosts = False

    # F295 Half 2 D8: spinner animation stops counting as progress.
    liveness_exclude_patterns = [PROCESSING_PATTERN]

    @classmethod
    async def preflight_launch(cls, *, agent_profile: str | None, model: str | None) -> None:
        """F295 Half 2 D1: prove the relay route before resource allocation."""
        from cli_agent_orchestrator.utils.grok_preflight import run_preflight

        run_preflight(agent_profile=agent_profile, model=model)

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
        fork_context: Optional[ForkContext] = None,
        model: Optional[str] = None,
    ):
        super().__init__(
            terminal_id, session_name, window_name, allowed_tools, skill_prompt, fork_context
        )
        self.allocated_session_uuid = (
            None
            if fork_context and fork_context.mode == "resume"
            else self._allocate_session_uuid()
        )
        self._initialized = False
        self._input_received = False
        self._agent_profile = agent_profile
        self._model = model
        self._buffer_epoch: int = 0
        self._prepare_grok_home()

    @property
    def resolved_model(self) -> Optional[str]:
        """Return the effective model resolved during command build."""
        return getattr(self, "_resolved_model", None)

    @property
    def paste_enter_count(self) -> int:
        """Grok submits bracketed-pasted input with one Enter."""
        return 1

    @property
    def paste_submit_delay(self) -> float:
        """P1 verified 0.3s is enough for Grok's simple prompt editor."""
        return 0.3

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        return True

    @property
    def extraction_tail_lines(self) -> int:
        return 2000

    def _after_dispatch_commit_locked(self) -> None:
        self._input_received = True

    def _load_profile(self):
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except FileNotFoundError:
            logger.debug(
                "Grok profile '%s' not found; launching without profile",
                self._agent_profile,
            )
            return None
        except Exception as exc:
            raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {exc}")

    def _build_grok_command(self) -> str:
        profile = self._load_profile()
        command_parts = [
            GROK_BINARY,
            "--always-approve",
            "--permission-mode",
            "bypassPermissions",
            "--minimal",
        ]

        # F295 AC0: rebuild private config from canonical on every launch.
        # This ensures routing changes (e.g. cc-switch) propagate without
        # manual terminal respawn.  MCP sections survive because
        # ensure_grok_mcp_servers upserts them AFTER this rebuild.
        self._rebuild_private_config()

        if profile and profile.mcpServers:
            ensure_grok_mcp_servers(
                profile.mcpServers,
                terminal_id=self.terminal_id,
                config_path=self._home_path() / "config.toml",
            )

        provider_defaults = get_provider_defaults("grok_cli")
        profile_name = getattr(profile, "name", None) or self._agent_profile
        profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
        if self._model:
            model = self._model
        else:
            model = resolve_provider_string_option(
                profile_defaults,
                provider_defaults,
                profile,
                "model",
                "model",
            )
        self._resolved_model = model if (isinstance(model, str) and model) else None
        if isinstance(model, str) and model:
            command_parts.extend(["-m", model])

        reasoning_effort = resolve_provider_string_option(
            profile_defaults,
            provider_defaults,
            profile,
            "reasoning_effort",
            "reasoningEffort",
        )
        if isinstance(reasoning_effort, str) and reasoning_effort:
            command_parts.extend(["--reasoning-effort", reasoning_effort])

        system_prompt = profile.system_prompt if profile and profile.system_prompt else ""
        system_prompt = self._apply_skill_prompt(system_prompt)
        if system_prompt:
            command_parts.extend(["--system-prompt-override", system_prompt])

        if self._fork_context:
            if self._fork_context.mode == "resume":
                command_parts.extend(["--resume", self._fork_context.session_uuid])
            else:
                command_parts.extend(
                    [
                        "--resume",
                        self._fork_context.session_uuid,
                        "--fork-session",
                        "--session-id",
                        self.allocated_session_uuid,
                    ]
                )
        else:
            command_parts.extend(["--session-id", self.allocated_session_uuid])

        return (
            f"env GROK_HOME={shlex.quote(str(self._home_path()))}"
            f" GROK_CLAUDE_HOOKS_ENABLED=0"
            f" {shlex.join(command_parts)}"
        )

    def _allocate_session_uuid(self) -> str:
        try:
            cwd = (
                get_backend().get_pane_working_directory(self.session_name, self.window_name)
                or os.getcwd()
            )
        except Exception:
            cwd = os.getcwd()
        root = provider_home("grok_cli").home / "sessions" / quote(cwd, safe="")
        for _ in range(2):
            value = str(uuid.uuid4())
            if not (root / value).exists():
                return value
        raise ProviderError("session_uuid_collision")

    def build_fork_command(self, session_uuid: str, new_session_uuid: Optional[str]) -> list[str]:
        old_context, old_uuid = self._fork_context, self.allocated_session_uuid
        self._fork_context = ForkContext(
            mode="fork",
            session_uuid=session_uuid,
            base_name="base",
            provider="grok_cli",
            initial_preamble="",
        )
        self.allocated_session_uuid = new_session_uuid or self._allocate_session_uuid()
        try:
            return shlex.split(self._build_grok_command())
        finally:
            self._fork_context, self.allocated_session_uuid = old_context, old_uuid

    def build_resume_command(self, session_uuid: str) -> list[str]:
        old_context = self._fork_context
        self._fork_context = ForkContext(
            mode="resume",
            session_uuid=session_uuid,
            base_name="base",
            provider="grok_cli",
            initial_preamble="",
        )
        try:
            return shlex.split(self._build_grok_command())
        finally:
            self._fork_context = old_context

    def capture_session_uuid(self, pane_pid: int, launch_time: float, cwd: str) -> str:
        if not self.allocated_session_uuid:
            raise ProviderError("base_session_unset")
        return self.allocated_session_uuid

    def resume_session_uuid(self) -> str | None:
        if self._fork_context and self._fork_context.mode == "resume":
            return self._fork_context.session_uuid
        return None

    def validate_session_artifact(self, session_uuid: str, cwd: str) -> None:
        path = (
            Path.home()
            / ".grok"
            / "sessions"
            / quote(cwd, safe="")
            / session_uuid
            / "chat_history.jsonl"
        )
        if not path.is_file() or path.stat().st_size == 0:
            raise RetryableArtifactValidation("session_artifact_missing_or_inert")

    def provider_process_started_at(self, pane_pid: int) -> float | None:
        from cli_agent_orchestrator.services.fork_context_service import _descendants

        matches = []
        for pid in _descendants(pane_pid):
            try:
                if b"grok" in Path(f"/proc/{pid}/cmdline").read_bytes():
                    matches.append(pid)
            except OSError:
                pass
        if len(matches) != 1:
            return None
        stat = Path(f"/proc/{matches[0]}/stat").read_text().split()
        btime = next(
            float(x.split()[1])
            for x in Path("/proc/stat").read_text().splitlines()
            if x.startswith("btime ")
        )
        return btime + float(stat[21]) / os.sysconf(os.sysconf_names["SC_CLK_TCK"])

    async def initialize(
        self,
        *,
        coordinates: tuple[str, str] | None = None,
        provider_override=None,
        raw_status: bool = False,
    ) -> bool:
        """Start Grok and wait for the prompt/footer to become interactive."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        shell_kwargs = {"timeout": init_timeout}
        if coordinates is not None:
            shell_kwargs["coordinates"] = coordinates
        if not await wait_for_shell(self.terminal_id, **shell_kwargs):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        command = self._build_grok_command()
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        status_kwargs = dict(
            timeout=float(get_server_settings()["provider_init_timeout"]),
            polling_interval=1.0,
        )
        if provider_override is not None or raw_status:
            status_kwargs.update(provider_override=provider_override, raw_status=raw_status)
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            **status_kwargs,
        ):
            raise TimeoutError(f"Grok CLI initialization timed out after {init_timeout}s")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        """Detect Grok status from the raw tmux pipe-pane byte stream."""
        native = self._resolve_native_status()
        if native is not None:
            return native

        if not output:
            return TerminalStatus.UNKNOWN

        clean_output = strip_terminal_escapes(output)
        if not clean_output.strip():
            return TerminalStatus.UNKNOWN

        if re.search(WAITING_USER_ANSWER_PATTERN, clean_output, re.MULTILINE):
            return TerminalStatus.WAITING_USER_ANSWER

        last_processing = self._last_match(PROCESSING_PATTERN, clean_output)
        last_running = self._last_match(RUNNING_PATTERN, clean_output)
        last_completed = self._last_match(COMPLETION_PATTERN, clean_output)
        last_idle = self._last_idle_match(clean_output)
        tail = "\n".join(clean_output.splitlines()[-12:])

        if self._has_error_after_last_completion(clean_output, last_completed):
            return TerminalStatus.ERROR

        if last_processing and (
            last_completed is None or last_completed.start() < last_processing.start()
        ):
            return TerminalStatus.PROCESSING

        if last_running and (
            last_completed is None or last_completed.start() < last_running.start()
        ):
            return TerminalStatus.PROCESSING

        if last_completed and last_idle and last_idle.start() > last_completed.end():
            return TerminalStatus.COMPLETED

        if self._input_received and last_completed:
            return TerminalStatus.COMPLETED

        if last_idle:
            return TerminalStatus.IDLE
        if re.search(IDLE_FOOTER_PATTERN, tail):
            # Arm 2: footer visible — require composer prompt in last 8 lines
            visible_lines = tail.splitlines()
            if any(re.match(COMPOSER_PROMPT_PATTERN, line) for line in visible_lines[-8:]):
                return TerminalStatus.IDLE
            # Footer without composer — stay PROCESSING (re-checks next tick)

        if self._initialized and self.shell_baseline:
            current_cmd = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            if current_cmd == self.shell_baseline:
                return TerminalStatus.IDLE

        return TerminalStatus.PROCESSING

    signal_kinds = frozenset({"waiting", "error", "progress", "completion", "chrome"})

    def emit_screen_signals(self, screen_lines: List[str]) -> tuple[ScreenSignal, ...]:
        rows = [line.rstrip() for line in screen_lines]
        while rows and not rows[-1].strip():
            rows.pop()
        signals: List[ScreenSignal] = []
        completion_rows = [
            index for index, row in enumerate(rows) if re.search(COMPLETION_PATTERN, row)
        ]
        newest_completion = max(completion_rows, default=-1)
        for index, row in enumerate(rows):
            if re.search(WAITING_USER_ANSWER_PATTERN, row):
                if index >= len(rows) - WAITING_VIEWPORT_ROWS:
                    signals.append(ScreenSignal("waiting", "WAITING_USER_ANSWER_PATTERN", index))
            if re.search(PROCESSING_PATTERN, row):
                if "Waiting for response" in row:
                    signals.append(
                        ScreenSignal("progress", "PROCESSING_PATTERN", index, row, "exempt")
                    )
                else:
                    signals.append(
                        ScreenSignal("progress", "PROCESSING_PATTERN", index, row, "corroborable")
                    )
            if re.search(RUNNING_PATTERN, row):
                signals.append(ScreenSignal("progress", "RUNNING_PATTERN", index, row, "exempt"))
            if re.search(COMPLETION_PATTERN, row):
                signals.append(ScreenSignal("completion", "COMPLETION_PATTERN", index))
            # Grok errors are effective only after the newest completion.
            if index > newest_completion and re.search(ERROR_PATTERN, row):
                signals.append(ScreenSignal("error", "ERROR_PATTERN", index))
            if re.search(IDLE_PROMPT_PATTERN, row):
                signals.append(ScreenSignal("chrome", "IDLE_PROMPT_PATTERN", index))
            if re.search(IDLE_FOOTER_PATTERN, row):
                signals.append(ScreenSignal("chrome", "IDLE_FOOTER_PATTERN", index))
            if re.search(COMPOSER_PROMPT_PATTERN, row):
                signals.append(ScreenSignal("chrome", "COMPOSER_PROMPT_PATTERN", index))
        # Same-row mutual exclusion: progress on a row suppresses waiting on that row (§4.3)
        progress_rows = {s.row_index for s in signals if s.signal_class == "progress"}
        signals = [
            s for s in signals if not (s.signal_class == "waiting" and s.row_index in progress_rows)
        ]
        return tuple(signals)

    def classify_injection_hazard(self, rows: List[str]) -> str | None:
        return (
            "interactive_dialog"
            if self.get_status_from_screen(rows) == TerminalStatus.WAITING_USER_ANSWER
            else None
        )

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        return self.classify_screen(screen_lines).status

    def read_composer_draft(self, screen_lines: List[str]) -> Optional[str]:
        """Read Grok's visible bottom composer draft from a rendered screen."""
        visible = [line.rstrip() for line in screen_lines]
        if not visible:
            return None

        footer_idx = len(visible)
        for idx in range(len(visible) - 1, -1, -1):
            if re.search(FOOTER_HINT_PATTERN, visible[idx]):
                footer_idx = idx
                break
        if footer_idx == len(visible):
            return None

        lower_bound = max(0, footer_idx - 8)
        prompt_idx = None
        for idx in range(footer_idx - 1, lower_bound - 1, -1):
            if re.match(COMPOSER_PROMPT_PATTERN, visible[idx]):
                prompt_idx = idx
                break
        if prompt_idx is None:
            return None

        line = visible[prompt_idx]
        prompt_pos = line.rfind("❯")
        draft = line[prompt_pos + 1 :].strip()
        if draft in EMPTY_DRAFT_PLACEHOLDERS:
            return ""
        return draft

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last completed Grok response from captured scrollback."""
        clean_output = strip_terminal_escapes(script_output)
        lines = clean_output.splitlines()

        completion_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            if re.search(COMPLETION_PATTERN, lines[idx]):
                completion_idx = idx
                break
        if completion_idx is None:
            raise ValueError("No Grok response found - no completion marker detected")

        prompt_idx = None
        for idx in range(completion_idx - 1, -1, -1):
            if re.match(USER_PROMPT_PATTERN, lines[idx]):
                prompt_idx = idx
                break
        if prompt_idx is None:
            raise ValueError("No Grok response found - no user prompt before completion")

        content = lines[prompt_idx + 1 : completion_idx]
        filtered = []
        for line in content:
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r"^[◆⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]?\s*(Thought|Thinking|Responding)\b", stripped):
                continue
            if re.search(r"\b\d+(?:\.\d+)?s\s+[⇣↑]\S+", stripped):
                continue
            filtered.append(stripped)

        final_answer = "\n".join(filtered).strip()
        if not final_answer:
            raise ValueError("Empty Grok response - no content found")
        return final_answer

    @staticmethod
    def _last_match(pattern: str, text: str) -> Optional[re.Match[str]]:
        last = None
        for match in re.finditer(pattern, text, re.MULTILINE):
            last = match
        return last

    @staticmethod
    def _last_idle_match(text: str) -> Optional[re.Match[str]]:
        last = None
        for match in re.finditer(IDLE_PROMPT_PATTERN, text, re.MULTILINE):
            suffix = text[match.end() : match.end() + 500]
            if re.search(r"^\s*❯\s*$", match.group(0)) or re.search(FOOTER_HINT_PATTERN, suffix):
                last = match
        return last

    @staticmethod
    def _has_error_after_last_completion(
        text: str,
        last_completed: Optional[re.Match[str]],
    ) -> bool:
        scan_text = text[last_completed.end() :] if last_completed else text
        return re.search(ERROR_PATTERN, scan_text, re.IGNORECASE | re.MULTILINE) is not None

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> bool:
        """Retryable cleanup: remove private GROK_HOME after stopping processes.

        Returns True if cleanup completed, False if deferred (processes still using home).
        """
        self._initialized = False
        home = self._home_path()
        if not self._is_managed_home(home):
            logger.warning("Refusing cleanup of non-managed path: %s", home)
            return True
        if not home.exists():
            return True
        if home.is_symlink():
            home.unlink()
            return True
        stopped = self._stop_home_processes(home)
        if not stopped:
            logger.warning("Deferred cleanup for %s: processes still active", self.terminal_id)
            return False
        try:
            shutil.rmtree(home)
        except FileNotFoundError:
            pass
        except PermissionError:
            logger.warning("PermissionError removing %s", home)
            return False
        return True

    def notify_status_buffer_reset(self, epoch: int) -> None:
        """Epoch-aware reset: discard stale fingerprints from prior epochs."""
        self._buffer_epoch = epoch

    # ── Private GROK_HOME lifecycle ──────────────────────────────────────────

    def _home_path(self) -> Path:
        """Deterministic private GROK_HOME for this terminal."""
        slug = re.sub(r"[^A-Za-z0-9_-]", "", self.terminal_id)[:48]
        sha12 = hashlib.sha256(self.terminal_id.encode()).hexdigest()[:12]
        return self._managed_home_root() / f"{slug}-{sha12}"

    @staticmethod
    def _managed_home_root() -> Path:
        """Root directory for all managed private GROK_HOME dirs."""
        return CAO_HOME_DIR / "grok" / "terminals"

    def _is_managed_home(self, home: Path) -> bool:
        """Validate that home is a legitimate managed private home path."""
        try:
            root = self._managed_home_root()
            # Must match deterministic path for this terminal
            if home != self._home_path():
                return False
            # Parent must be managed root
            if home.parent != root:
                return False
            # No symlinks in ancestor chain
            base = CAO_HOME_DIR
            for part in [base, base / "grok", root]:
                if part.is_symlink():
                    return False
            # Platform normalization
            if home.parent.resolve(strict=False) != root.resolve(strict=False):
                return False
            return True
        except (OSError, ValueError):
            return False

    def _prepare_grok_home(self) -> None:
        """Create per-terminal private GROK_HOME with auth symlink, sessions symlink, and config."""
        plane = provider_home("grok_cli")
        home = self._home_path()
        root = self._managed_home_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.mkdir(exist_ok=True, mode=0o700)
        # Enforce permissions even if dir already existed
        home.chmod(0o700)

        # Auth symlink
        auth_target = getattr(plane, "credential_path", None) or (plane.home / "auth.json")
        auth_link = home / "auth.json"
        if not auth_link.exists() and not auth_link.is_symlink():
            auth_link.symlink_to(auth_target)

        # Sessions symlink
        sessions_target = plane.sessions
        sessions_target.mkdir(parents=True, exist_ok=True)
        sessions_link = home / "sessions"
        if not sessions_link.exists() and not sessions_link.is_symlink():
            sessions_link.symlink_to(sessions_target)

        # Config: seed from canonical, upsert terminal-bound MCP sections
        canonical_config = plane.home / "config.toml"
        private_config = home / "config.toml"
        if not private_config.exists():
            seed = canonical_config.read_text(encoding="utf-8") if canonical_config.exists() else ""
            self._atomic_write_private(private_config, seed)

    def _rebuild_private_config(self) -> None:
        """F295 AC0: rebuild private config from canonical on every launch.

        Reads the canonical ``~/.grok/config.toml``, sanity-parses with tomllib,
        and on success atomically overwrites the private copy.  On canonical
        missing or parse failure, keeps the existing private config (fail-safe
        to stale-but-working) and logs a warning.

        Also stamps the canonical sha256 into terminal metadata (AC2).
        """
        plane = provider_home("grok_cli")
        canonical_path = plane.home / "config.toml"
        private_config = self._home_path() / "config.toml"

        if not canonical_path.exists():
            logger.warning(
                "F295: canonical config %s missing; keeping existing private config "
                "for terminal %s",
                canonical_path,
                self.terminal_id,
            )
            return

        try:
            canonical_text = canonical_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "F295: cannot read canonical config %s: %s; keeping existing private config",
                canonical_path,
                exc,
            )
            return

        # Sanity-parse gate: reject corrupted/malformed TOML
        try:
            tomllib.loads(canonical_text)
        except tomllib.TOMLDecodeError as exc:
            logger.warning(
                "F295: canonical config %s failed TOML parse: %s; "
                "keeping existing private config for terminal %s",
                canonical_path,
                exc,
                self.terminal_id,
            )
            return

        # Atomic overwrite of private config with fresh canonical content
        self._atomic_write_private(private_config, canonical_text)

        # AC2: stamp sha256 of the canonical text into terminal system metadata (D12).
        # Uses the reserved 'cao' namespace so worker full-replace cannot erase it.
        canonical_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
        try:
            from cli_agent_orchestrator.clients.database import merge_terminal_system_metadata

            merge_terminal_system_metadata(self.terminal_id, {"config_sha256": canonical_hash})
        except Exception as exc:
            logger.warning(
                "F295: failed to stamp config hash for terminal %s: %s",
                self.terminal_id,
                exc,
            )

    def _atomic_write_private(self, path: Path, content: str) -> None:
        """Atomic write with restrictive permissions."""
        fd = None
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            os.fchmod(fd, 0o600)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temp_path, path)
            path.chmod(0o600)
            temp_path = None
        finally:
            if fd is not None:
                os.close(fd)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def _pids_using_home(self, home: Path) -> list[int] | None:
        """Find PIDs with GROK_HOME set to this path.

        F312: Returns None ONLY when a process that is plausibly ours (descendant
        of terminal pane or has cwd/open-files touching this home) cannot be
        inspected.  Uninspectable processes unrelated to this terminal are treated
        as not-ours and never block cleanup.
        """
        home_str = str(home)
        uid = os.getuid()
        result: list[int] = []
        try:
            for pid in psutil.pids():
                inspection = self._inspect_home_process(pid, home_str, uid)
                if inspection is None:
                    # AccessDenied — check if this pid is plausibly related to us.
                    if self._pid_plausibly_related(pid, home):
                        return None  # genuine uncertainty on a related process
                    # Unrelated uninspectable process — not ours, continue.
                    continue
                if inspection:
                    result.append(pid)
        except psutil.Error:
            return None
        return result

    def _pid_plausibly_related(self, pid: int, home: Path) -> bool:
        """F312: Heuristic — is this uninspectable pid plausibly a child of our terminal?

        Checks: (1) is it a descendant of the terminal's pane process, or
        (2) does its cwd or any open file reference the GROK_HOME path.
        Returns False (not related) if none of these can be confirmed.

        Residual risk: if a genuine grok child denies parent(), cwd(), AND
        open_files() inspection simultaneously (triple-deny), this heuristic
        returns False and cleanup proceeds — potentially removing a home that
        is still in use.  In practice this requires a same-uid process with
        PR_SET_DUMPABLE=0 AND restricted /proc/pid access, which grok children
        never configure.  The risk is accepted as preferable to the prior
        behavior of deferring cleanup forever on any AccessDenied.
        """
        home_str = str(home)
        try:
            proc = psutil.Process(pid)
            # Check if it's a descendant of our pane
            try:
                from cli_agent_orchestrator.services.fork_context_service import pane_pid

                our_pane = pane_pid(self.session_name, self.window_name)
                if our_pane:
                    parent = proc.parent()
                    # Walk ancestry up to 10 levels
                    visited: set[int] = set()
                    ancestor = parent
                    for _ in range(10):
                        if ancestor is None:
                            break
                        if ancestor.pid in visited:
                            break
                        visited.add(ancestor.pid)
                        if ancestor.pid == our_pane:
                            return True
                        try:
                            ancestor = ancestor.parent()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            break
            except Exception:
                pass

            # Check cwd
            try:
                cwd = proc.cwd()
                if cwd and cwd.startswith(home_str):
                    return True
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

            # Check open files
            try:
                for f in proc.open_files():
                    if f.path.startswith(home_str):
                        return True
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
        except psutil.AccessDenied:
            pass

        # Cannot confirm any relationship — treat as unrelated
        logger.debug(
            "F312: pid %d triple-deny (parent/cwd/open_files all inaccessible); "
            "treating as unrelated to GROK_HOME %s",
            pid,
            home,
        )
        return False

    def _inspect_home_process(self, pid: int, home_str: str, uid: int) -> bool | None:
        """Check if a single PID uses the given GROK_HOME. Returns None on uncertainty."""
        try:
            proc = psutil.Process(pid)
            if proc.uids().real != uid:
                return False
            environ = proc.environ()
            return environ.get("GROK_HOME") == home_str
        except psutil.AccessDenied:
            # Can't inspect: might be using our home → uncertain
            return None
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False

    def _pid_uses_home(self, pid: int, home_str: str) -> bool | None:
        """Verify a PID still uses the home at signal time (PID-reuse safety)."""
        return self._inspect_home_process(pid, home_str, os.getuid())

    def _stop_home_processes(self, home: Path) -> bool:
        """SIGTERM then SIGKILL processes using this home. Returns False if any survive."""
        pids = self._pids_using_home(home)
        if pids is None:
            return False
        if not pids:
            return True

        home_str = str(home)
        # SIGTERM phase
        for pid in pids:
            if self._pid_uses_home(pid, home_str):
                try:
                    proc = psutil.Process(pid)
                    proc.send_signal(signal.SIGTERM)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        # Wait for SIGTERM
        for pid in pids:
            try:
                proc = psutil.Process(pid)
                proc.wait(timeout=1.0)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
                pass

        # Check survivors and SIGKILL
        survivors = self._pids_using_home(home)
        if survivors is None:
            return False
        if not survivors:
            return True

        for pid in survivors:
            if self._pid_uses_home(pid, home_str):
                try:
                    proc = psutil.Process(pid)
                    proc.send_signal(signal.SIGKILL)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        # Final wait
        for pid in survivors:
            try:
                proc = psutil.Process(pid)
                proc.wait(timeout=1.0)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
                pass

        # Final check
        final = self._pids_using_home(home)
        if final is None:
            return False
        return len(final) == 0
