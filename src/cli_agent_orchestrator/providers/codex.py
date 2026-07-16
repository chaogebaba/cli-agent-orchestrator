"""Codex CLI provider implementation."""

import asyncio
import logging
import re
import shlex
import time
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.providers.base import (
    BaseProvider, RetryableArtifactValidation, TerminalArtifactValidation,
)
from cli_agent_orchestrator.providers.screen_classification import (
    ScreenClassification,
    ScreenSignal,
    classify_screen_signals,
)
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_server_settings,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# Regex patterns for Codex output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
IDLE_PROMPT_PATTERN = r"(?:❯|›|codex>)"
# Number of lines from the bottom of capture to check for the idle prompt.
# With --no-alt-screen, codex output is inline (scrollback contains history),
# so we can't anchor to \Z. Instead, check the last few lines where the prompt
# and status bar appear.
IDLE_PROMPT_TAIL_LINES = 5
# The idle prompt character ❯ (U+276F) is rendered on-screen by capture-pane
# but is NOT written to the raw output stream captured by pipe-pane.  Instead,
# the TUI footer text "? for shortcuts" is reliably present whenever the TUI
# is active.  This is intentionally permissive — _has_idle_pattern() is a
# lightweight pre-check; the real status decision is made by get_status()
# which uses capture-pane (rendered screen).
# Match assistant response start: "assistant:/codex:/agent:" (label style from synthetic
# test fixtures) or "•" bullet point (real Codex interactive output format).
# [^\S\n]* matches horizontal whitespace only (not newlines) so the match anchors
# on the actual bullet line — using \s* would let the match start on a blank
# line above the bullet, breaking per-line tool-call filtering downstream.
ASSISTANT_PREFIX_PATTERN = r"^(?:(?:assistant|codex|agent)\s*:|[^\S\n]*•)"
# MCP tool call marker emitted by Codex when invoking a tool, e.g.
# "• Called cao-mcp-server.load_skill({...})". The body that follows
# (└ ... lines) is the tool's return value, not the model's reply.
# Used to skip these markers when locating the actual response start.
# The "<server>.<tool>(" shape (identifier.identifier followed by an open
# paren) is required so legitimate model bullets like "• Called attention
# to the bug" don't get filtered as tool calls.
MCP_TOOL_CALL_PATTERN = r"^[^\S\n]*•\s+Called\s+[\w-]+\.[\w-]+\("
# Codex startup/system notice bullets that are NOT model replies, e.g.
# "• You have 3 usage limit resets available. Run /usage to use one."
# These render with the same "•" prefix as assistant messages; without this
# filter a fresh terminal showing only the banner is classified COMPLETED and
# the banner text gets extracted as the model's reply (false handoff success).
SYSTEM_NOTICE_PATTERN = r"^[^\S\n]*•\s+You have \d+ usage limit reset"
# Match user input: "You ..." (label style) or "› text" (Codex interactive prompt).
# The "›[^\S\n]*\S" alternative requires a non-whitespace character on the same line
# to distinguish user input ("› what is your role?") from the empty idle prompt ("› ").
# [^\S\n] matches horizontal whitespace only (spaces/tabs), preventing the pattern
# from crossing newline boundaries into subsequent lines.
USER_PREFIX_PATTERN = r"^(?:You\b|›[^\S\n]*\S)"
# Strict idle prompt pattern for extraction: matches empty prompt lines only.
# Distinguishes "› " (idle) from "› user message" (user input with text).
IDLE_PROMPT_STRICT_PATTERN = r"^\s*(?:❯|›|codex>)\s*$"
IDLE_PROMPT_SCREEN_PATTERN = rf"^\s*{IDLE_PROMPT_PATTERN}"

PROCESSING_PATTERN = r"\b(thinking|working|running|executing|processing|analyzing)\b"
WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"

# Codex TUI footer indicators (status bar below the idle prompt).
# Used to detect when the bottom lines contain TUI chrome rather than user input.
# v0.110 and earlier: "? for shortcuts" and "N% context left"
# v0.111+: "model · N% left · path" (PR #13202 restored draft footer hints)
# v0.136+: "model · path" (the "N% left" segment was removed)
# The "·\s+[~/]" alternative anchors on the path component of the footer,
# which is shared across v0.111 and v0.136 status bars.
TUI_FOOTER_PATTERN = r"(?:\?\s+for shortcuts|context left|\d+%\s+left|·\s+[~/])"
# Codex TUI progress spinner: "• Working (0s • esc to interrupt)",
# "• Thinking (3m 39s ...)",
# "• Starting script creation (1h 2m 3s • esc to interrupt)".
# The prefix text and elapsed-time format vary, but the interrupt hint is stable.
# Appears inline with --no-alt-screen when the agent is actively processing.
# Must be checked before COMPLETED to avoid false positives (the • matches
# ASSISTANT_PREFIX_PATTERN and the TUI footer › matches idle prompt).
TUI_PROGRESS_PATTERN = r"•.*\([^)]*\besc to interrupt\)"
SCREEN_FALLBACK_PROCESSING_PATTERN = re.compile(r"\A[\s\S]*\Z")

# Workspace trust/approval prompt shown when Codex opens a new directory
TRUST_PROMPT_PATTERN = (
    r"(?:allow Codex to work in this folder"
    r"|Do you trust the contents of this directory)"
)
TRUST_SELECTOR_PATTERN = re.compile(
    r"^\s*›\s*1\.\s*(?:Yes|Allow|Trust|Continue)\b",
    re.IGNORECASE | re.MULTILINE,
)
DIALOG_ACTION_FOOTER_PATTERN = re.compile(
    r"(?:Press enter to\s+(?:confirm|continue|view)|"
    r"Press space to select|"
    r"left/right\s+group\s+.*enter\s+edit shortcut.*esc\s+close)",
    re.IGNORECASE,
)
# Codex welcome banner indicating normal startup (no trust prompt)
CODEX_WELCOME_PATTERN = r"OpenAI Codex"
CODEX_EMPTY_COMPOSER_PLACEHOLDERS = {
    "Explain this codebase",
    "Ask Codex to do anything",
}
# CSI SGR sequences only (colour/intensity). Used to walk dim state on
# escape-preserving capture-pane (-e) lines without treating cursor CSI as text.
_SGR_CSI_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _apply_sgr_params_to_dim(params: str, dim: bool) -> bool:
    """Update dim/faint state from one SGR parameter list.

    Walks codes left-to-right. Extended colour payloads after 38/48 are
    consumed so their sub-parameters cannot be mistaken for intensity codes:
    ``38;5;N`` (256-colour) skips N; ``38;2;R;G;B`` (truecolour) skips R,G,B.
    Only a standalone intensity ``2`` sets dim; ``22`` clears it; ``0``/empty
    resets all attributes (dim off).
    """
    if params == "":
        return False
    parts = params.split(";")
    idx = 0
    while idx < len(parts):
        code = parts[idx]
        idx += 1
        if code == "" or code == "0":
            dim = False
            continue
        if code in ("38", "48"):
            # Select graphic rendition extended colour: next token is mode.
            if idx >= len(parts):
                break
            mode = parts[idx]
            idx += 1
            if mode == "5":
                # 256-colour: one colour index follows.
                idx += 1
            elif mode == "2":
                # Truecolour: three RGB components follow.
                idx += 3
            # Unknown mode: stop consuming; remaining tokens are not intensity.
            continue
        if code == "2":
            dim = True
            continue
        if code == "22":
            dim = False
            continue
        # Other SGR codes (bold, italic, plain colours, …) leave dim as-is.
    return dim


def _composer_body_is_dim_ghost(raw_body: str) -> bool:
    """Return True when composer body text is entirely dim/faint (SGR 2).

    Empirical (codex 0.143 capture-pane -e): ghost suggestions render as
    ``\\x1b[1m›\\x1b[0m \\x1b[2mHINT\\x1b[0m`` (sometimes with a bg SGR before
    dim). Typed drafts have no dim on the body text. pyte drops dim, so this
    only works on escape-preserving captures.

    Truecolour / 256-colour SGR (``38;2;…`` / ``38;5;…``) must not be read as
    dim: the ``2`` after ``38`` is a colour-space selector, not intensity.
    """
    dim = False
    saw_text = False
    saw_undimmed = False
    i = 0
    n = len(raw_body)
    while i < n:
        if raw_body[i] == "\x1b":
            m = _SGR_CSI_RE.match(raw_body, i)
            if m:
                dim = _apply_sgr_params_to_dim(m.group(1), dim)
                i = m.end()
                continue
            # Non-SGR CSI/OSC: skip to final byte or drop the ESC.
            if i + 1 < n and raw_body[i + 1] == "[":
                j = i + 2
                while j < n and not ("A" <= raw_body[j] <= "Z" or "a" <= raw_body[j] <= "z"):
                    j += 1
                i = j + 1 if j < n else n
                continue
            i += 1
            continue
        ch = raw_body[i]
        if not ch.isspace():
            saw_text = True
            if not dim:
                saw_undimmed = True
        i += 1
    return saw_text and not saw_undimmed


def _compute_tui_footer_cutoff(all_lines: list) -> int:
    """Compute the character position where the TUI footer area starts.

    Scans backward from the last line to find the TUI footer status bar
    (matches TUI_FOOTER_PATTERN), then continues upward to include any
    blank lines and the suggestion hint line (› with text) that appear
    above the status bar as part of the footer area.

    Returns the character position in the joined text (``'\\n'.join(all_lines)``)
    where the footer starts. Returns ``len('\\n'.join(all_lines))`` if no
    footer is found.
    """
    n = len(all_lines)
    footer_start_idx = n

    # Find the status bar line (last TUI_FOOTER_PATTERN match in the bottom area)
    for i in range(n - 1, max(n - IDLE_PROMPT_TAIL_LINES - 1, -1), -1):
        if re.search(TUI_FOOTER_PATTERN, all_lines[i]):
            footer_start_idx = i
            break

    if footer_start_idx == n:
        return len("\n".join(all_lines))

    # Scan upward from the status bar to include blank lines and the
    # suggestion hint (› with text) that are part of the TUI footer chrome.
    for j in range(footer_start_idx - 1, max(footer_start_idx - 4, -1), -1):
        line = all_lines[j]
        if not line.strip():
            footer_start_idx = j
        elif re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line):
            footer_start_idx = j
            break
        else:
            break

    return len("\n".join(all_lines[:footer_start_idx]))


def _toml_scalar(value: Any) -> str:
    """Serialize a Python scalar to a TOML literal for a ``-c key=<value>`` override.

    Strings become quoted TOML basic strings (backslash, quote, tab, CR, and newline escaped so
    tmux ``send_keys`` keeps the launch command on one line); bools become
    ``true``/``false``; ints and floats are emitted bare. Non-scalar values (dict/list/None) raise ``TypeError`` so a misconfigured profile fails fast. ``bool`` is checked
    before ``int`` because ``bool`` is a subclass of ``int`` in Python, so the
    order here is load-bearing — a flipped order would render ``True`` as ``1``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise TypeError(
            "codexConfig values must be scalars (str, bool, int, or float); "
            f"got {type(value).__name__}"
        )
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


_CODEX_CONFIG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _toml_override(key: str, value: Any) -> str:
    """Build one ``key=<toml-scalar>`` Codex ``-c`` override, validating the key.

    Keys must be non-empty dotted config paths over ``[A-Za-z0-9_.-]`` (e.g.
    ``features.fast_mode``); spaces, ``=``, quotes, or control characters are
    rejected so a misconfigured profile fails fast instead of silently emitting
    a malformed ``-c`` override. Value-serialization failures from
    :func:`_toml_scalar` are re-raised with the offending key for context.
    """
    if not isinstance(key, str) or not _CODEX_CONFIG_KEY_PATTERN.match(key):
        raise ValueError(
            f"Invalid codexConfig key {key!r}: must be a dotted config path over "
            "[A-Za-z0-9_.-] (e.g. 'features.fast_mode')"
        )
    try:
        return f"{key}={_toml_scalar(value)}"
    except TypeError as exc:
        raise TypeError(f"codexConfig key '{key}': {exc}") from exc


def _resolved_codex_profile_config(profile) -> tuple[str | None, dict[str, Any]]:
    """Single model/config resolver shared by interactive and seed launches."""
    defaults = get_provider_defaults("codex")
    default_model = defaults.get("model")
    if "model" in defaults and isinstance(default_model, str):
        model = default_model or None
    else:
        model = profile.model if profile and profile.model else None
    config = dict(getattr(profile, "codexConfig", None) or {})
    effort = defaults.get("reasoning_effort")
    if "reasoning_effort" in defaults and isinstance(effort, str):
        if effort:
            config["model_reasoning_effort"] = effort
        else:
            config.pop("model_reasoning_effort", None)
    return model, config


def _find_assistant_marker(text: str) -> Optional[re.Match[str]]:
    """Find the first ASSISTANT_PREFIX_PATTERN match in ``text`` whose line
    is not an MCP tool-call marker.

    Codex emits ``• Called <server>.<tool>(...)`` when invoking an MCP tool;
    that bullet matches ASSISTANT_PREFIX_PATTERN but is followed by tool
    output, not the model's reply. Anchoring on it would conflate tool
    output with the model response (status: false COMPLETED;
    extraction: skill-body leak).
    """
    for m in re.finditer(ASSISTANT_PREFIX_PATTERN, text, re.IGNORECASE | re.MULTILINE):
        line_end = text.find("\n", m.start())
        if line_end == -1:
            line_end = len(text)
        line = text[m.start() : line_end]
        if re.match(MCP_TOOL_CALL_PATTERN, line):
            continue
        if re.match(SYSTEM_NOTICE_PATTERN, line):
            continue
        return m
    return None


class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


class CodexProvider(BaseProvider):
    supports_fork_context = True
    supports_seed_resume_identity = True
    supports_reauth_rebind = True

    def capture_shell_baseline(self) -> str | None:
        """Capture through this module's backend seam before Codex starts."""
        return get_backend().get_pane_current_command(self.session_name, self.window_name)
    """Provider for Codex CLI tool integration."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
        fork_context: Optional[ForkContext] = None,
    ):
        """Initialize provider state."""
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt, fork_context)
        self._initialized = False
        self._agent_profile = agent_profile

    @classmethod
    def seed_resume_identity(cls, cwd: str, agent_profile: str) -> str:
        """Create and validate a native Codex rollout without CAO coordinates."""
        profile = load_agent_profile(agent_profile)
        argv = ["codex", "exec", "--skip-git-repo-check", "-C", cwd]
        model, config = _resolved_codex_profile_config(profile)
        if isinstance(model, str) and model:
            argv.extend(["--model", model])
        for key, value in config.items():
            argv.extend(["-c", _toml_override(key, value)])
        argv.append("Reply exactly: SEED_OK then stop.")
        try:
            completed = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=90, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("seed_timeout") from exc
        except OSError as exc:
            raise RuntimeError("seed_exec_failed") from exc
        if completed.returncode != 0:
            raise RuntimeError("seed_exec_failed")
        matches = set(re.findall(
            r"(?im)^\s*session id:\s*([0-9a-f]{8}-[0-9a-f-]{27,})\s*$",
            completed.stdout or "",
        ))
        if len(matches) != 1:
            raise RuntimeError("seed_uuid_unparseable")
        session_uuid = next(iter(matches))
        validator = cls("seed", "seed", "seed", agent_profile)
        try:
            validator.validate_session_artifact(session_uuid, cwd)
        except Exception as exc:
            raise RuntimeError("seed_artifact_invalid") from exc
        return session_uuid

    def _build_codex_command(self) -> str:
        """Build Codex command with agent profile if provided.

        Returns properly escaped shell command string that can be safely sent via tmux.
        Uses codex's -c developer_instructions flag to inject agent system prompts.
        """
        # --yolo (alias for --dangerously-bypass-approvals-and-sandbox)
        # is the default because CAO runs codex non-interactively in tmux
        # where approval prompts would block handoff/assign. Profiles can
        # opt out via `codexProfile` (names a [profiles.<name>] block in
        # ~/.codex/config.toml), unless unrestricted allowed tools are enabled.
        # In practice, allowed_tools containing "*" is treated as yolo mode
        # and overrides codexProfile in the same way as an explicit yolo launch.
        yolo = bool(self._allowed_tools and "*" in self._allowed_tools)

        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        if profile and profile.codexProfile and not yolo:
            command_parts = ["codex", "--profile", profile.codexProfile]
        else:
            command_parts = ["codex", "--yolo"]
        command_parts.extend(["--no-alt-screen", "--disable", "shell_snapshot"])

        model, codex_config = _resolved_codex_profile_config(profile)
        if isinstance(model, str) and model:
            command_parts.extend(["--model", model])

        if profile is not None:
            system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
            system_prompt = self._apply_skill_prompt(system_prompt)

            # Prepend security constraints for soft enforcement (Codex has no
            # native tool restriction mechanism). Only applied when tool
            # restrictions are active (not unrestricted "*").
            if self._allowed_tools and "*" not in self._allowed_tools:
                from cli_agent_orchestrator.constants import SECURITY_PROMPT

                tools_list = ", ".join(self._allowed_tools)
                tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
                system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt

            if system_prompt:
                # Codex accepts developer_instructions via -c config override.
                # This is injected as a developer role message before AGENTS.md content.
                # Escape backslashes, double quotes, and newlines for TOML basic string.
                # Newlines must become literal \n to prevent tmux send_keys from
                # splitting the command across multiple lines.
                command_parts.extend(
                    ["-c", f"developer_instructions={_toml_scalar(system_prompt)}"]
                )

            # Add MCP servers via -c config overrides (per-session, no global config changes).
            # Each server field is set via dotted path: mcp_servers.<name>.<field>=<value>
            if profile.mcpServers:
                for server_name, server_config in profile.mcpServers.items():
                    prefix = f"mcp_servers.{server_name}"
                    if isinstance(server_config, dict):
                        cfg = dict(server_config)
                    else:
                        cfg = server_config.model_dump(exclude_none=True)
                    # Resolve the bundled cao-mcp-server console script to a
                    # PATH-independent invocation.
                    cfg = resolve_mcp_server_config(cfg)
                    if "command" in cfg:
                        command_parts.extend(
                            ["-c", f"{prefix}.command={_toml_scalar(cfg['command'])}"]
                        )
                    if "args" in cfg:
                        args_toml = "[" + ", ".join(_toml_scalar(a) for a in cfg["args"]) + "]"
                        command_parts.extend(["-c", f"{prefix}.args={args_toml}"])
                    if "env" in cfg and cfg["env"]:
                        for env_key, env_val in cfg["env"].items():
                            command_parts.extend(
                                ["-c", f"{prefix}.env.{env_key}={_toml_scalar(str(env_val))}"]
                            )
                    # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server)
                    # can identify the current session for handoff/assign operations.
                    # Codex does not forward env vars to MCP subprocesses by default;
                    # env_vars lists names to inherit from the parent shell environment.
                    env_vars = cfg.get("env_vars", [])
                    if "CAO_TERMINAL_ID" not in env_vars:
                        env_vars = list(env_vars) + ["CAO_TERMINAL_ID"]
                    env_vars_toml = "[" + ", ".join(_toml_scalar(v) for v in env_vars) + "]"
                    command_parts.extend(["-c", f"{prefix}.env_vars={env_vars_toml}"])
                    # Set a generous tool timeout for MCP calls like handoff, which
                    # create a new terminal, initialize the provider, send a message,
                    # wait for the agent to complete, and extract the output.
                    # Codex defaults to 60s which is too short for multi-step operations.
                    # Value MUST be a TOML float (600.0, not 600) because Codex
                    # deserializes tool_timeout_sec via Option<f64>; a TOML integer
                    # is silently rejected and falls back to the 60s default.
                    if "tool_timeout_sec" not in cfg:
                        command_parts.extend(["-c", f"{prefix}.tool_timeout_sec=600.0"])

            # Inline Codex config overrides (-c key=value). Lets a profile set
            # per-agent Codex knobs — reasoning effort, service tier, fast mode,
            # etc. — without editing the global ~/.codex/config.toml or
            # maintaining named profile files. Keys may be dotted config paths
            # (e.g. "features.fast_mode"); values are serialized to TOML
            # scalars. Emitted before providers.toml defaults so per-key TOML
            # settings can take precedence while other profile keys remain.
        for key, value in codex_config.items():
            command_parts.extend(["-c", _toml_override(key, value)])

        command_parts.extend(["-c", "features.multi_agent=false"])

        if self._fork_context:
            mode = self._fork_context.mode
            prefix = ["codex", mode]
            rest = command_parts[1:]
            rest = ["--dangerously-bypass-approvals-and-sandbox" if x == "--yolo" else x for x in rest]
            command_parts = prefix + rest + [self._fork_context.session_uuid]
        return shlex.join(command_parts)

    def build_fork_command(self, session_uuid: str, new_session_uuid: Optional[str] = None) -> list[str]:
        old = self._fork_context
        self._fork_context = ForkContext(mode="fork", session_uuid=session_uuid, base_name="base",
                                         provider="codex", initial_preamble="")
        try:
            return shlex.split(self._build_codex_command())
        finally:
            self._fork_context = old

    def build_resume_command(self, session_uuid: str) -> list[str]:
        old = self._fork_context
        self._fork_context = ForkContext(mode="resume", session_uuid=session_uuid, base_name="base",
                                         provider="codex", initial_preamble="")
        try:
            return shlex.split(self._build_codex_command())
        finally:
            self._fork_context = old

    def capture_session_uuid(self, pane_pid: int, launch_time: float, cwd: str) -> str:
        from cli_agent_orchestrator.services.fork_context_service import capture_codex_uuid
        return capture_codex_uuid(pane_pid, launch_time, cwd)

    def resume_session_uuid(self) -> str | None:
        return None

    def validate_session_artifact(self, session_uuid: str, cwd: str) -> None:
        matches = list((Path.home() / ".codex" / "sessions").glob(
            f"**/rollout-*{session_uuid}*.jsonl"
        ))
        if not matches:
            raise RetryableArtifactValidation("session_artifact_missing")
        if len(matches) > 1:
            raise TerminalArtifactValidation("session_artifact_ambiguous")
        with matches[0].open(encoding="utf-8") as stream:
            first = json.loads(stream.readline())
        if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != session_uuid:
            raise TerminalArtifactValidation("session_artifact_identity_invalid")

    def auth_state_path(self) -> Path | None:
        return Path.home() / ".codex" / "auth.json"

    def provider_process_started_at(self, pane_pid: int) -> float | None:
        from cli_agent_orchestrator.services.fork_context_service import _descendants

        matches = []
        for pid in _descendants(pane_pid):
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
                if b"codex" in cmd:
                    matches.append(pid)
            except OSError:
                pass
        if len(matches) != 1:
            return None
        stat = Path(f"/proc/{matches[0]}/stat").read_text().split()
        btime = next(float(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines() if x.startswith("btime "))
        return btime + float(stat[21]) / os.sysconf(os.sysconf_names["SC_CLK_TCK"])

    async def _handle_trust_prompt(self, timeout: float = 20.0) -> None:
        """Auto-accept the workspace trust prompt if it appears.

        Codex shows a folder approval dialog when opening a new directory.
        This sends Enter to accept the default option (allow Codex to work).
        CAO assumes the user trusts the working directory since they confirmed
        workspace access during the launch command.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            output = get_backend().get_history(self.session_name, self.window_name)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Clean ANSI codes for reliable text matching
            clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

            if re.search(TRUST_PROMPT_PATTERN, clean_output):
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info("Codex workspace trust prompt detected, auto-accepting")
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                return

            # Check if Codex has fully started (welcome banner visible)
            if re.search(CODEX_WELCOME_PATTERN, clean_output):
                logger.info("Codex started without trust prompt")
                return

            await asyncio.sleep(1.0)
        logger.warning("Codex trust prompt handler timed out")

    async def initialize(
        self, *, coordinates: tuple[str, str] | None = None,
        provider_override=None, raw_status: bool = False,
    ) -> bool:
        """Initialize Codex provider by starting codex command."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        shell_kwargs = {"timeout": init_timeout}
        if coordinates is not None:
            shell_kwargs["coordinates"] = coordinates
        if not await wait_for_shell(self.terminal_id, **shell_kwargs):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")
        self.shell_baseline = self.capture_shell_baseline()
        if not self.shell_baseline:
            raise ProviderError("shell_baseline_unavailable")

        # Send a warm-up command before launching codex.
        # Codex exits immediately in freshly-created tmux sessions where the shell
        # has not yet processed a full interactive command cycle.
        # Arm the StatusMonitor stickiness gate: each send_keys here represents
        # external input that must be allowed to drive PROCESSING transitions
        # past any previously-latched ready state.
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, "echo ready")
        await asyncio.sleep(2.0)

        # Build command with flags and agent profile (developer_instructions).
        # --no-alt-screen: run in inline mode so output stays in normal scrollback,
        #   making tmux capture-pane reliable.
        # --disable shell_snapshot: avoid TTY input conflicts (SIGTTIN) in tmux
        #   caused by the shell_snapshot subprocess inheriting stdin.
        command = self._build_codex_command()
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Handle workspace trust prompt if it appears (new/untrusted directories)
        await self._handle_trust_prompt(timeout=20.0)

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
            raise TimeoutError("Codex initialization timed out after 60 seconds")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        # Native status (herdr): trust the backend's agent state when available;
        # on herdr the buffer is never fed, so buffer parsing can't leave UNKNOWN.
        native = self._resolve_native_status()
        if native is not None:
            return native

        return self._get_screen_local_status(output)

    @staticmethod
    def _get_screen_local_status(output: str) -> TerminalStatus:
        """Classify Codex text without consulting backend or mutable provider state."""

        if not output:
            return TerminalStatus.UNKNOWN

        # Strip the RAW pipe-pane escapes (cursor positioning, in-place redraws),
        # not just SGR colour codes — otherwise cursor sequences survive and the
        # idle ``›`` prompt / structural checks below misfire on the raw stream.
        clean_output = strip_terminal_escapes(output)
        tail_output = "\n".join(clean_output.splitlines()[-25:])

        # Search for user messages, excluding the Codex TUI footer when present.
        # The TUI footer (idle prompt hint like "› Summarize recent commits" +
        # status bar "? for shortcuts / context left") can contain › followed by
        # suggestion text, which USER_PREFIX_PATTERN would incorrectly match as
        # user input, preventing COMPLETED detection.
        # Only apply the cutoff when TUI footer indicators are actually present
        # to avoid over-excluding in short outputs or test fixtures.
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        if tui_footer_detected:
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)

        last_user = None
        for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            if match.start() < cutoff_pos:
                last_user = match

        output_after_last_user = clean_output[last_user.start() :] if last_user else clean_output
        # Skip MCP tool-call markers — those mark "model invoked a tool", not
        # "model has replied", and shouldn't gate WAITING/ERROR detection.
        assistant_after_last_user = bool(
            last_user and _find_assistant_marker(output_after_last_user) is not None
        )

        # Check trust prompt early — the trust menu uses › which matches the idle prompt
        # pattern, and PROCESSING_PATTERN matches "running" in "You are running Codex in..."
        trust = re.search(TRUST_PROMPT_PATTERN, clean_output)
        if trust:
            selector = TRUST_SELECTOR_PATTERN.search(clean_output, trust.end())
            if (
                selector is not None
                and clean_output[trust.end() : selector.start()].count("\n") <= 4
            ):
                return TerminalStatus.WAITING_USER_ANSWER

        # Check bottom of captured output for idle prompt.
        # With --no-alt-screen, scrollback contains history so we can't anchor
        # to end-of-string. Instead, check only the last few lines.
        bottom_lines = clean_output.strip().splitlines()[-IDLE_PROMPT_TAIL_LINES:]
        has_idle_prompt_at_end = any(
            re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line, re.IGNORECASE) for line in bottom_lines
        )

        # Only treat ERROR/WAITING prompts as actionable if they appear after the last user message
        # and are not part of an assistant response.
        if last_user is not None:
            if not assistant_after_last_user:
                if re.search(
                    WAITING_PROMPT_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.WAITING_USER_ANSWER
                if re.search(
                    ERROR_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.ERROR
        else:
            if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.WAITING_USER_ANSWER
            if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.ERROR
        if has_idle_prompt_at_end:
            # Check for TUI progress indicator ("• Working (0s • esc to interrupt)").
            # With --no-alt-screen, the TUI footer (› hint + status bar) is always
            # rendered at the bottom, even during processing. The • in the progress
            # spinner matches ASSISTANT_PREFIX_PATTERN, causing a false COMPLETED.
            # Detect the spinner and return PROCESSING before checking for COMPLETED.
            if re.search(TUI_PROGRESS_PATTERN, tail_output, re.MULTILINE):
                return TerminalStatus.PROCESSING

            # Consider COMPLETED only if we see an assistant marker (skipping
            # MCP tool-call markers) after the last user message. Without the
            # tool-call filter, "• Called <server>.<tool>(...)" emitted before
            # the model has actually replied would trip COMPLETED prematurely.
            if last_user is not None:
                if _find_assistant_marker(clean_output[last_user.start() :]) is not None:
                    return TerminalStatus.COMPLETED

                return TerminalStatus.IDLE

            # No user-message marker in the cleaned buffer. Two cases:
            # - Fresh init: no assistant content either → IDLE.
            # - Long-running response: the › user marker has been evicted from
            #   the 8KB rolling buffer by the time the response settles, but an
            #   assistant bullet is still visible. Without this branch we'd
            #   return IDLE forever and ``wait_for_status(completed)`` in the
            #   e2e tests would time out.
            # Search above the TUI footer cutoff so the › suggestion-hint and
            # status-bar lines aren't confused with a model reply.
            if _find_assistant_marker(clean_output[:cutoff_pos]) is not None:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # If we're not at an idle prompt and we don't see explicit errors/permission prompts,
        # assume the CLI is still producing output.
        return TerminalStatus.PROCESSING

    # Opt in to pyte rendered-screen detection (gated by CAO_PYTE_STATUS). The
    # existing get_status() regex logic above already works correctly against a
    # composited screen (verified against a live capture-pane snapshot) — the
    # bug is specific to the raw pipe-pane rolling buffer, where an unsent TUI
    # composer draft can evict the user/assistant anchors from the 8KB window.
    # The base class's default get_status_from_screen() (join + delegate to
    # get_status) is sufficient here, so no override is needed — see base.py's
    # ClaudeCodeProvider reference implementation for a provider that DOES need
    # a purpose-built override.
    supports_screen_detection = True

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        """Block orchestrated input while Codex is showing an interactive dialog."""
        return True

    supports_draft_preservation = True
    composer_clear_keys = ["C-a", "C-k"]
    clear_immune_ghosts = True
    # Dim-SGR ghost detection needs escape-preserving capture-pane (-e).
    composer_parse_accepts_escapes = True
    liveness_exclude_patterns = [
        rf"^\s*{IDLE_PROMPT_PATTERN}",
        TUI_FOOTER_PATTERN,
        r"\btab\s+to\s+queue\s+message\b",
    ]

    def classify_screen(self, screen_lines: list[str]) -> ScreenClassification:
        """Produce Codex signals while preserving the existing fixture corpus."""
        joined = "\n".join(screen_lines)
        clean = strip_terminal_escapes(joined)
        rows = clean.splitlines()
        legacy_status = self._get_screen_local_status(joined)
        chrome_rows = [
            index for index, row in enumerate(rows)
            if re.search(IDLE_PROMPT_SCREEN_PATTERN, row, re.IGNORECASE)
        ]
        progress_rows = [
            index for index, row in enumerate(rows)
            if re.search(TUI_PROGRESS_PATTERN, row) is not None
        ]
        terminal_index = next(
            (index for index in range(len(rows) - 1, -1, -1) if rows[index].strip()),
            -1,
        )
        signals: list[ScreenSignal] = []
        for index, row in enumerate(rows):
            progress = re.search(TUI_PROGRESS_PATTERN, row) is not None
            if progress:
                signals.append(ScreenSignal("progress", "TUI_PROGRESS_PATTERN", index))
            if TRUST_SELECTOR_PATTERN.search(row):
                signals.append(ScreenSignal("waiting", "TRUST_SELECTOR_PATTERN", index))
            if (
                not progress_rows
                and index == terminal_index
                and DIALOG_ACTION_FOOTER_PATTERN.search(row)
            ):
                signals.append(
                    ScreenSignal("waiting", "DIALOG_ACTION_FOOTER_PATTERN", index)
                )
            if legacy_status == TerminalStatus.WAITING_USER_ANSWER and re.search(
                WAITING_PROMPT_PATTERN, row, re.IGNORECASE
            ):
                signals.append(ScreenSignal("waiting", "WAITING_PROMPT_PATTERN", index))
            if legacy_status == TerminalStatus.ERROR and re.search(
                ERROR_PATTERN, row, re.IGNORECASE
            ):
                signals.append(ScreenSignal("error", "ERROR_PATTERN", index))
            assistant = re.search(ASSISTANT_PREFIX_PATTERN, row, re.IGNORECASE) is not None
            excluded_assistant = bool(
                re.search(MCP_TOOL_CALL_PATTERN, row, re.IGNORECASE)
                or re.search(SYSTEM_NOTICE_PATTERN, row, re.IGNORECASE)
            )
            if assistant and not excluded_assistant and (
                legacy_status == TerminalStatus.COMPLETED
                or progress
                or (bool(progress_rows) and index > max(progress_rows) and bool(chrome_rows))
            ):
                signals.append(
                    ScreenSignal("completion", "ASSISTANT_PREFIX_PATTERN", index)
                )
            if index in chrome_rows:
                signals.append(ScreenSignal("chrome", "IDLE_PROMPT_SCREEN_PATTERN", index))

        # Codex historically treats a signal-free screen as PROCESSING. Keep
        # that output byte-identical for the existing corpus with an explicit,
        # named producer fallback; it participates only when no law signal exists.
        if not signals and legacy_status == TerminalStatus.PROCESSING:
            assert SCREEN_FALLBACK_PROCESSING_PATTERN.search(clean)
            signals.append(
                ScreenSignal(
                    "progress", "SCREEN_FALLBACK_PROCESSING_PATTERN", max(len(rows) - 1, 0)
                )
            )
        return classify_screen_signals(signals)

    def get_status_from_screen(self, screen_lines: list[str]) -> TerminalStatus:
        return self.classify_screen(screen_lines).status

    def read_composer_draft(self, screen_lines: list[str]) -> str | None:
        """Read the visible Codex composer draft from rendered screen lines.

        Codex renders the editable composer at the bottom with a leading ``›``.
        The status footer sits below it. The parser intentionally uses only the
        provider's rendered screen shape; the shared draft guard stays generic.

        When lines retain SGR escapes (``capture-pane -e`` / strip_escapes=False),
        dim-wrapped composer body text (SGR 2) is treated as a ghost suggestion
        and returns ``""`` so it is not stashed/restored as a real draft. Plain
        (escape-stripped) lines still work; placeholder strings remain a fallback.
        """
        if not screen_lines:
            return None

        raw_lines = [line.rstrip("\r") for line in screen_lines]
        # Structural matching uses escape-stripped text; segment join keeps raw
        # widths where useful, then we strip SGR from the final draft.
        plain_lines = [strip_terminal_escapes(line).rstrip() for line in raw_lines]

        footer_idx = len(plain_lines)
        for i in range(len(plain_lines) - 1, -1, -1):
            if re.search(TUI_FOOTER_PATTERN, plain_lines[i]):
                footer_idx = i
                break

        search_end = footer_idx
        while search_end > 0 and not plain_lines[search_end - 1].strip():
            search_end -= 1

        prompt_idx: int | None = None
        lower_bound = max(0, search_end - 12)
        for i in range(search_end - 1, lower_bound - 1, -1):
            if "›" in plain_lines[i]:
                prompt_idx = i
                break
        if prompt_idx is None:
            return None

        # Ghost detection needs the raw (possibly dim) body after › on the
        # prompt line plus continuation rows before the footer.
        raw_prompt = raw_lines[prompt_idx]
        # Locate › in raw by walking with CSI skipped, or plain rfind on stripped.
        plain_prompt = plain_lines[prompt_idx]
        prompt_pos = plain_prompt.rfind("›")
        first_plain = plain_prompt[prompt_pos + 1 :]
        if first_plain.startswith(" "):
            first_plain = first_plain[1:]

        raw_body_parts: list[str] = []
        # Extract raw suffix after › (CSI may wrap the glyph).
        raw_after = self._raw_after_prompt_glyph(raw_prompt)
        raw_body_parts.append(raw_after)
        for line in raw_lines[prompt_idx + 1 : search_end]:
            raw_body_parts.append(line)
        raw_body = "\n".join(raw_body_parts)
        if _composer_body_is_dim_ghost(raw_body):
            return ""

        segments = [first_plain]
        for line in plain_lines[prompt_idx + 1 : search_end]:
            text = line.strip()
            if not text:
                segments.append("")
                continue
            if text.startswith(("╭", "╰", "│")) and text.endswith(("╮", "╯", "│")):
                continue
            segments.append(text)

        while segments and segments[-1] == "":
            segments.pop()

        # Join using plain line widths (escape-stripped); matches previous
        # behavior for wrap detection on capture-pane plain or pyte screens.
        draft = self._join_composer_segments(plain_lines, prompt_idx, prompt_pos, segments)
        if draft.strip() in CODEX_EMPTY_COMPOSER_PLACEHOLDERS:
            return ""
        return draft

    @staticmethod
    def _raw_after_prompt_glyph(raw_line: str) -> str:
        """Return the raw substring after the composer ``›`` glyph (CSI-aware)."""
        plain_chars: list[str] = []
        raw_map: list[int] = []
        j = 0
        while j < len(raw_line):
            if raw_line[j] == "\x1b" and j + 1 < len(raw_line) and raw_line[j + 1] == "[":
                k = j + 2
                while k < len(raw_line) and not (
                    "A" <= raw_line[k] <= "Z" or "a" <= raw_line[k] <= "z"
                ):
                    k += 1
                j = k + 1 if k < len(raw_line) else len(raw_line)
                continue
            plain_chars.append(raw_line[j])
            raw_map.append(j)
            j += 1
        plain = "".join(plain_chars)
        p = plain.rfind("›")
        if p < 0 or p + 1 >= len(raw_map):
            # Glyph at end or missing: body empty / whole line after last char.
            if p >= 0 and p + 1 == len(raw_map):
                return ""
            idx = raw_line.rfind("›")
            return raw_line[idx + 1 :] if idx >= 0 else raw_line
        start = raw_map[p + 1]
        return raw_line[start:]

    @staticmethod
    def _join_composer_segments(
        raw_lines: list[str],
        prompt_idx: int,
        prompt_pos: int,
        segments: list[str],
    ) -> str:
        if not segments:
            return ""

        joined = segments[0]
        for offset, segment in enumerate(segments[1:], start=1):
            prev_raw = raw_lines[prompt_idx + offset - 1]
            width = len(prev_raw)
            prev_visible = prev_raw.rstrip()
            if offset == 1:
                available = max(width - prompt_pos - 2, 0)
                prev_len = len(prev_visible[prompt_pos + 2 :])
            else:
                available = width
                prev_len = len(prev_visible)
            if available >= 20 and prev_len >= available:
                joined += segment
            else:
                joined += "\n" + segment
        return joined

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Codex's final response from terminal output.

        Supports two output formats:
        - Label style: "You ...\\nassistant: response\\n❯" (synthetic/test format)
        - Bullet style: "› user message\\n• response\\n›" (real Codex interactive mode)

        Primary approach: find the last user message and extract everything between
        the end of that line and the next empty idle prompt.
        Fallback: use assistant marker based extraction when no user message is found.
        """
        # Strip ALL terminal escape sequences, not just SGR colour codes. The
        # narrow ANSI_CODE_PATTERN (``\x1b[...m``) leaves cursor-movement (H),
        # erase (K), and scroll CSI sequences in place; codex's TUI emits those
        # heavily, so an SGR-only strip returned raw escape garbage
        # (``[49;2H[K[38;2;...m``) as the "response", failing extraction. Use
        # the shared strip which also normalises \r and column-1 cursor moves to
        # newlines — this is fed a tmux capture-pane render (already laid out),
        # so the line-based extraction below still anchors correctly.
        clean_output = strip_terminal_escapes(script_output)

        # Primary: find last user message, extract response between it and idle prompt.
        # Exclude the Codex TUI footer from user-message matching when detected.
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        if tui_footer_detected:
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)

        user_matches = [
            m
            for m in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
            if m.start() < cutoff_pos
        ]

        if user_matches:
            last_user = user_matches[-1]

            # Find the first assistant response marker (• or assistant:) after
            # the user message, skipping "• Called <server>.<tool>(...)" MCP
            # tool call markers — those are followed by tool output, not the
            # model's reply. Anchoring on a tool call marker would pull tool
            # output (e.g. skill body text) into the extracted response.
            asst_after_user = _find_assistant_marker(clean_output[last_user.start() :])

            if asst_after_user:
                response_start = last_user.start() + asst_after_user.start()
            else:
                # No assistant marker found; fall back to skipping one line
                user_line_end = clean_output.find("\n", last_user.start())
                if user_line_end == -1:
                    user_line_end = len(clean_output)
                response_start = user_line_end + 1

            # Find extraction boundary: empty idle prompt or TUI footer area.
            # With --no-alt-screen, the TUI footer (› hint + status bar) has no
            # empty idle prompt. Use cutoff_pos as the boundary when TUI is present.
            idle_after = re.search(
                IDLE_PROMPT_STRICT_PATTERN,
                clean_output[response_start:],
                re.MULTILINE,
            )
            if idle_after:
                end_pos = response_start + idle_after.start()
            elif tui_footer_detected:
                end_pos = cutoff_pos
            else:
                end_pos = len(clean_output)

            response_text = clean_output[response_start:end_pos].strip()

            if response_text:
                # Strip "assistant:" prefix if present (label format)
                response_text = re.sub(
                    r"^(?:assistant|codex|agent)\s*:\s*",
                    "",
                    response_text,
                    count=1,
                    flags=re.IGNORECASE,
                )
                return response_text.strip()

        # Fallback: assistant marker based extraction (no user message found).
        # Filter out "• Called <tool>(...)" MCP tool call markers so we anchor
        # on the model's actual reply, not tool output.
        all_matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )
        matches = []
        for m in all_matches:
            line_end = clean_output.find("\n", m.start())
            if line_end == -1:
                line_end = len(clean_output)
            line = clean_output[m.start() : line_end]
            if re.match(MCP_TOOL_CALL_PATTERN, line):
                continue
            if re.match(SYSTEM_NOTICE_PATTERN, line):
                continue
            matches.append(m)

        if not matches:
            raise ValueError("No Codex response found - no assistant marker detected")

        last_match = matches[-1]
        start_pos = last_match.end()

        idle_after = re.search(
            IDLE_PROMPT_STRICT_PATTERN,
            clean_output[start_pos:],
            re.MULTILINE,
        )
        end_pos = start_pos + idle_after.start() if idle_after else len(clean_output)

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Codex response - no content found")

        return final_answer

    def exit_cli(self) -> str:
        """Get the command to exit Codex CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Codex CLI provider."""
        self._initialized = False
